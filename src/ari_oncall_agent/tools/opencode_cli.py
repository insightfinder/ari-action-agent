"""oncall agent's own opencode wiring: which capability maps to which
scoped opencode agent, each agent's least-privilege permission block, and
the per-request opencode.json (LLM key, GitHub MCP server + PAT). The
actual subprocess/event-parsing/tracing mechanics are agent-kind-agnostic
and live in common/opencode_runtime.py — this module supplies only what's
specific to fixing code.

`publish_pr`'s *primary* path no longer calls opencode at all: title,
body, branch name and file list are already fully deterministic by the
time it runs, so routing it through an LLM-driven agent bought nothing but
latency (see capabilities/publish_pr.py, tools/git_ops.py,
tools/github_rest.py). `ari-publish` below still exists and is still
wired with the GitHub MCP server's tools — publish_pr falls back to it
(a real opencode run, same as before) if the deterministic path fails, so
the more flexible/comprehensive MCP-driven route is still available as a
safety net rather than removed outright.
"""

import copy
import json
import os
from pathlib import Path
from typing import Any

from common import skills
from common.opencode_runtime import merge_traces, opencode_config_home, run_opencode as _run_opencode  # noqa: F401  (re-exported)
from common.settings import get_settings
from ..protected_branches import protected_branches
from .build_cache import reserve_build_cache_home

# Mirrors configs/capabilities.yaml's capability→agent mapping. Kept here
# (not just in the YAML) because it's opencode-specific wiring, not a fact
# about the capability itself.
CAPABILITY_AGENTS = {
    "generate_change": "ari-change",
    "verify": "ari-verify",
    "publish_pr": "ari-publish",
    "run_skill": "ari-skill",
}

SKILL_RUN_DIR = "skill-run"
SKILL_CONFIG_FILE = "ari-skill.json"
SERVICE_SECRET_ENV = (
    "AGENT_API_KEYS",
    "TEMPORAL_API_KEY",
    "TEMPORAL_PAYLOAD_ENCRYPTION_KEY",
    "GITHUB_PAT",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)
SKILL_AGENTS_FOR_REPO_SKILLS = ("ari-change", "ari-verify")

# Read-only git inspection only — `ari-change` has no business committing
# or pushing anything (that's publish_pr.py's job — deterministically via
# tools/git_ops.py on the primary path, or `ari-publish` below on the
# fallback path). Confirmed live this mattered, not just in principle: the
# old blanket `"git *": "allow"` let a reproduction of `ari-change` create
# a branch, commit, push, and open a real PR itself, entirely bypassing
# the dedicated publish step (see docs/MEMORY.md for the full incident) —
# `git *` also matches destructive commands like `git reset --hard`/`git
# clean -fdx`, not just the read-only inspection this agent actually needs
# (it still reads history — `git log`/`git diff`/etc — while investigating
# before it edits; see generate_change.py).
_READ_ONLY_GIT_BASH = {
    "git diff*": "allow",
    "git status*": "allow",
    "git log*": "allow",
    "git show*": "allow",
    "git blame*": "allow",
    "*": "deny",
}


def _no_delegation_tools() -> dict[str, bool]:
    """opencode's built-in `task` tool lets an agent delegate a sub-goal to
    a fresh subagent — confirmed live that this subagent runs with its own
    permissions, not the delegating agent's `permission` block: a
    reproduction of `ari-change` used it to hand off "commit and open a
    PR" to a general-purpose subagent that then ran `gh pr create`, a
    command `ari-change`'s own bash permission (`git *` only) would have
    denied outright. It also dominated wall-clock time in that same run —
    4 delegated calls accounted for ~68% of a ~400s run, each one a full
    separate multi-turn reasoning loop instead of the direct, fast
    read/grep/edit calls the parent agent could have made itself. Returns
    a fresh dict each call so callers can safely spread it into their own
    `tools` override (e.g. `ari-publish`'s `github_*: True`) without two
    agents sharing one mutable dict."""
    return {"task": False}


_AGENT_DEFS = {
    # Investigates as needed (root cause for a bug, or scope for a feature/
    # edit) and edits, both in one session — no separate read-only
    # investigation agent any more (see generate_change.py's docstring for
    # why that split was removed). Can inspect git history, cannot commit/
    # push (ari-publish's job), cannot run arbitrary build/test commands
    # (that's verify's job), and cannot touch GitHub.
    "ari-change": {
        "description": "Investigates the repo as needed, then makes the requested code change — a bug fix, feature, refactor, or edit.",
        "mode": "primary",
        "permission": {
            "edit": "allow",
            "bash": _READ_ONLY_GIT_BASH,
            "webfetch": "deny",
            "skill": "deny",
        },
        "tools": _no_delegation_tools(),
    },
    # Needs to actually run the project's build/lint/test tooling, which is
    # unpredictable per-repo — broad bash allow is required here. Isolation
    # comes from the ephemeral per-request sandbox, not command whitelisting.
    # Never edits code — a failure just reports failure, generate_change
    # retries. `git push*` is denied unconditionally (not just for
    # protected branches — see protected_branches.py): verify has no
    # legitimate reason to push anything, ever, regardless of which
    # branch, and its otherwise-broad bash access would silently permit it
    # without this.
    "ari-verify": {
        "description": "Runs the project's build/lint/test suite and reports pass/fail.",
        "mode": "primary",
        "permission": {
            "edit": "deny",
            "bash": {"git push*": "deny", "*": "allow"},
            "webfetch": "deny",
            "skill": "deny",
        },
        "tools": _no_delegation_tools(),
    },
    # The only agent with GitHub MCP tools enabled. No build tooling. Keeps
    # its own broad `git *` — pushing/committing/branching is exactly its
    # job — but still no delegation: it should do its own git/MCP calls
    # directly, not hand them to an unrestricted subagent. Only reached as
    # publish_pr's fallback now (see capabilities/publish_pr.py) — the
    # primary path is deterministic and never touches opencode at all.
    # This module dict is the *default* shape (used as-is when a repo has
    # no protected branches); write_config() below patches in per-repo
    # `git push` denials for protected_branches.py's blacklist before
    # writing the actual opencode.json for a given request.
    "ari-publish": {
        "description": "Pushes the change branch and opens a pull request via the GitHub MCP server.",
        "mode": "primary",
        "permission": {
            "edit": "deny",
            "bash": {"git *": "allow", "*": "deny"},
            "webfetch": "deny",
            "skill": "deny",
        },
        "tools": {**_no_delegation_tools(), "github_*": True},
    },
    "ari-skill": {
        "description": "Runs one installed Agent Skill without a repository, using only that skill's own scripts and the commands its allowed-tools grants.",
        "mode": "primary",
        "permission": {
            "edit": "deny",
            "bash": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "glob": "deny",
            "grep": "deny",
            "list": "deny",
            "skill": "deny",
        },
        "tools": _no_delegation_tools(),
    },
}


def _protected_branch_push_denies(repo_url: str) -> dict[str, str]:
    """opencode bash-permission `deny` entries for the common `git push`
    forms that would write directly to one of this repo's protected
    branches (protected_branches.py) — empty for a repo with none.

    Best-effort, not a semantic parse of git syntax: opencode's own
    permission matching (as used throughout this file) is prefix +
    trailing-wildcard only, so this covers the common invocation shapes
    (`git push origin <branch>`, `... HEAD:<branch>`, with `-u`/`--force`/
    `-f`, `<branch>:<branch>`), not every syntactically possible one —
    see protected_branches.py's own docstring for why real branch
    protection on the GitHub repo itself is still the only guarantee."""
    denies: dict[str, str] = {}
    for branch in sorted(protected_branches(repo_url)):
        for prefix in (
            f"git push origin {branch}",
            f"git push origin HEAD:{branch}",
            f"git push -u origin {branch}",
            f"git push --set-upstream origin {branch}",
            f"git push -f origin {branch}",
            f"git push --force origin {branch}",
            f"git push origin {branch}:{branch}",
            f"git push origin +{branch}",
        ):
            denies[f"{prefix}*"] = "deny"
    return denies


def write_config(workdir: str, llm: dict, repo: dict) -> str:
    """Writes a per-request opencode.json into the cloned repo dir. Called
    once, right after clone, before any opencode invocation. Torn down with
    the rest of the sandbox in cleanup_workspace — nothing here outlives
    the request.
    """
    settings = get_settings()

    agent_defs = copy.deepcopy(_AGENT_DEFS)
    protected_denies = _protected_branch_push_denies(repo["url"])
    if protected_denies:
        # Denies first so they're the most specific entries ahead of the
        # blanket `"git *": "allow"` — same specific-before-general
        # convention `_READ_ONLY_GIT_BASH` above already relies on.
        publish_bash = agent_defs["ari-publish"]["permission"]["bash"]
        agent_defs["ari-publish"]["permission"]["bash"] = {**protected_denies, **publish_bash}

    repo_skills = skills.repo_agent_skill_permission(workdir)
    for name in SKILL_AGENTS_FOR_REPO_SKILLS:
        agent_defs[name]["permission"]["skill"] = copy.deepcopy(repo_skills)

    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            # Well-known provider id (e.g. "openai", "anthropic") — opencode
            # already knows its model catalog; we only need to override auth.
            llm["provider"]: {"options": {"apiKey": llm["api_key"]}}
        },
        # Every tool from the github MCP server is off by default; only the
        # ari-publish agent re-enables it (see _AGENT_DEFS) — reached only
        # as publish_pr's fallback (capabilities/publish_pr.py), not on the
        # normal deterministic path. Keeps the change/verify agents from
        # ever seeing (or spending context budget on) GitHub tools they
        # have no business calling. `task` (subagent delegation) is off
        # globally, full stop — see _no_delegation_tools() for why every
        # agent also repeats this explicitly rather than relying on this
        # default alone.
        "tools": {"github_*": False, "task": False},
        "agent": agent_defs,
    }

    if repo.get("pat"):
        config["mcp"] = {
            "github": {
                "type": "remote",
                "url": settings.github_mcp_server_url,
                "enabled": True,
                "oauth": False,
                "headers": {"Authorization": f"Bearer {repo['pat']}"},
            }
        }

    path = Path(workdir) / "opencode.json"
    path.write_text(json.dumps(config, indent=2))

    # Keep it out of `git status`/diffs/the eventual PR without touching the
    # repo's own .gitignore (which is itself something opencode might edit).
    exclude_path = Path(workdir) / ".git" / "info" / "exclude"
    if exclude_path.parent.is_dir():
        with exclude_path.open("a") as f:
            f.write("\nopencode.json\n")

    return str(path)


def _build_cache_env(build_cache_root: str) -> dict[str, str]:
    """The environment that puts each build tool's dependency cache on the
    persistent volume instead of the container's own filesystem.

    `HOME` alone is not enough, and that is the whole reason this exists.
    npm and Go do resolve their caches from `$HOME`, but **the JVM does
    not**: on Linux OpenJDK reads `user.home` from the passwd database, not
    the environment, so Maven's local repository stays at
    `/root/.m2/repository` however `HOME` is set. Measured in the worker,
    2026-10-10:

        $ HOME=/var/lib/ari-agent/build-cache java -XshowSettings:properties
          user.home = /root

    The cost of that was not subtle. On stg the same day `/root/.m2/
    repository` held **1.5 GB**, downloaded inside a 25-minute-old pod, while
    the build-cache volume held only `.npm` -- so every fresh worker paid a
    full cold Maven download. That is what spent `verify`'s entire 480s
    budget on a Java repo and failed it four attempts in a row (stg runs
    fc532091 and 1c2396a4). `-Dmaven.repo.local` is Maven's own documented
    override and does not go through `user.home`.

    Gradle is the same class of problem and gets `GRADLE_USER_HOME` for the
    same reason. `MAVEN_OPTS` is appended to rather than replaced:
    Dockerfile.oncall sets `-Xmx2g` there and that heap limit still applies.
    """
    maven_repo = os.path.join(build_cache_root, ".m2", "repository")
    existing_opts = os.environ.get("MAVEN_OPTS", "")
    return {
        "HOME": build_cache_root,
        "MAVEN_OPTS": f"{existing_opts} -Dmaven.repo.local={maven_repo}".strip(),
        "GRADLE_USER_HOME": os.path.join(build_cache_root, ".gradle"),
    }


async def run_opencode(ctx, instruction: str, *, continue_session: bool | None = None) -> dict:
    """Thin wrapper over common.opencode_runtime.run_opencode: resolves
    *this* agent kind's opencode agent name for the calling capability.
    Every capability calls it the same way:

        result = await ctx.tool("opencode")(ctx, "<instruction>")
    """
    agent = CAPABILITY_AGENTS[ctx.capability]

    if ctx.capability == "verify":
        # Only verify's bash commands (mvn/go/npm, run via opencode's bash
        # tool, which inherits this process's env) benefit from a real
        # dependency cache — see build_cache.py/settings.build_cache_root.
        # Scoped to this one capability, not every opencode call:
        # generate_change/publish_pr have no business sharing state
        # through $HOME, and opencode's own state is already isolated
        # separately per request (see opencode_runtime.run_opencode's
        # XDG_* handling). Held for the duration of the call so eviction
        # (build_cache.py) can never delete anything out from under an
        # in-flight build.
        async with reserve_build_cache_home() as build_cache_root:
            return await _run_opencode(
                ctx,
                instruction,
                agent,
                continue_session=continue_session,
                extra_env=_build_cache_env(build_cache_root),
            )

    return await _run_opencode(ctx, instruction, agent, continue_session=continue_session)


def skill_agent_definition(skill: skills.Skill, skill_dir: str) -> dict:
    definition = copy.deepcopy(_AGENT_DEFS["ari-skill"])
    inside = f"{skill_dir.rstrip('/')}/*"
    definition["permission"].update(
        {
            "bash": skills.bash_rules_for(skill_dir, skill.allowed_tools),
            "skill": skills.skill_permission([skill.name]),
            "read": {"*": "deny", inside: "allow"},
            "external_directory": {"*": "deny", inside: "allow"},
        }
    )
    return definition


def write_skill_config(path: str, llm: dict, skill: skills.Skill, skill_dir: str) -> str:
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {llm["provider"]: {"options": {"apiKey": llm["api_key"]}}},
        "tools": {"github_*": False, "task": False},
        "agent": {CAPABILITY_AGENTS["run_skill"]: skill_agent_definition(skill, skill_dir)},
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2))
    return str(target)


async def run_skill_agent(ctx, skill: skills.Skill, instruction: str) -> dict:
    workdir = Path(ctx.workspace) / SKILL_RUN_DIR
    workdir.mkdir(parents=True, exist_ok=True)
    skill_dir = skills.materialized_dir(opencode_config_home(ctx.workspace)) / skill.name
    config_path = Path(ctx.workspace) / ".opencode-state" / SKILL_CONFIG_FILE
    write_skill_config(str(config_path), ctx.config["llm"], skill, skill_dir.as_posix())
    return await _run_opencode(
        ctx,
        instruction,
        CAPABILITY_AGENTS["run_skill"],
        continue_session=False,
        extra_env={"OPENCODE_CONFIG": str(config_path), "OPENCODE_DISABLE_PROJECT_CONFIG": "1"},
        workdir=str(workdir),
        drop_env=SERVICE_SECRET_ENV,
    )
