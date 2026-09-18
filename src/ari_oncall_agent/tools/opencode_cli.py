"""oncall agent's own opencode wiring: which capability maps to which
scoped opencode agent, each agent's least-privilege permission block, and
the per-request opencode.json (LLM key + GitHub MCP server + PAT). The
actual subprocess/event-parsing/tracing mechanics are agent-kind-agnostic
and live in common/opencode_runtime.py — this module supplies only what's
specific to fixing code.
"""

import json
from pathlib import Path
from typing import Any

from common.opencode_runtime import merge_traces, run_opencode as _run_opencode  # noqa: F401  (re-exported)
from common.settings import get_settings

# Mirrors configs/capabilities.yaml's capability→agent mapping. Kept here
# (not just in the YAML) because it's opencode-specific wiring, not a fact
# about the capability itself.
CAPABILITY_AGENTS = {
    "analyze": "ari-analyze",
    "generate_change": "ari-change",
    "verify": "ari-verify",
    "publish_pr": "ari-publish",
}

# Read-only git inspection only — `ari-analyze`/`ari-change` have no
# business committing or pushing anything (that's `ari-publish`'s job,
# per `capabilities/publish_pr.py`'s own instruction: "Stage and commit
# exactly these files"). Confirmed live this mattered, not just in
# principle: the old blanket `"git *": "allow"` let a reproduction of
# `ari-change` create a branch, commit, push, and open a real PR itself,
# entirely bypassing `ari-publish` (see docs/MEMORY.md for the full
# incident) — `git *` also matches destructive commands like `git reset
# --hard`/`git clean -fdx`, not just the read-only inspection these two
# agents actually need.
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
    # Read-only: investigate (root cause for a bug, or scope for a
    # feature/edit) without touching the tree.
    "ari-analyze": {
        "description": "Investigates the repo — root cause for a bug, or scope for a feature/edit. Never edits files.",
        "mode": "primary",
        "permission": {
            "edit": "deny",
            "bash": _READ_ONLY_GIT_BASH,
            "webfetch": "deny",
        },
        # See _no_delegation_tools() — disables opencode's built-in "task"
        # subagent-delegation tool, which otherwise runs outside this
        # agent's own `permission` block entirely.
        "tools": _no_delegation_tools(),
    },
    # Can edit, can inspect git history, cannot commit/push (ari-publish's
    # job), cannot run arbitrary build/test commands (that's verify's job),
    # and cannot touch GitHub.
    "ari-change": {
        "description": "Makes the requested code change — a bug fix, feature, refactor, or edit.",
        "mode": "primary",
        "permission": {
            "edit": "allow",
            "bash": _READ_ONLY_GIT_BASH,
            "webfetch": "deny",
        },
        "tools": _no_delegation_tools(),
    },
    # Needs to actually run the project's build/lint/test tooling, which is
    # unpredictable per-repo — broad bash allow is required here. Isolation
    # comes from the ephemeral per-request sandbox, not command whitelisting.
    # Never edits code — a failure just reports failure, generate_change retries.
    "ari-verify": {
        "description": "Runs the project's build/lint/test suite and reports pass/fail.",
        "mode": "primary",
        "permission": {
            "edit": "deny",
            "bash": "allow",
            "webfetch": "deny",
        },
        "tools": _no_delegation_tools(),
    },
    # The only agent with GitHub MCP tools enabled. No build tooling. Keeps
    # its own broad `git *` — pushing/committing/branching is exactly its
    # job — but still no delegation: it should do its own git/MCP calls
    # directly, not hand them to an unrestricted subagent.
    "ari-publish": {
        "description": "Pushes the change branch and opens a pull request via the GitHub MCP server.",
        "mode": "primary",
        "permission": {
            "edit": "deny",
            "bash": {"git *": "allow", "*": "deny"},
            "webfetch": "deny",
        },
        "tools": {**_no_delegation_tools(), "github_*": True},
    },
}


def write_config(workdir: str, llm: dict, repo: dict) -> str:
    """Writes a per-request opencode.json into the cloned repo dir. Called
    once, right after clone, before any opencode invocation. Torn down with
    the rest of the sandbox in cleanup_workspace — nothing here outlives
    the request.
    """
    settings = get_settings()

    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            # Well-known provider id (e.g. "openai", "anthropic") — opencode
            # already knows its model catalog; we only need to override auth.
            llm["provider"]: {"options": {"apiKey": llm["api_key"]}}
        },
        # Every tool from the github MCP server is off by default; only the
        # ari-publish agent re-enables it (see _AGENT_DEFS). Keeps the
        # analyze/fix/verify agents from ever seeing (or spending context
        # budget on) GitHub tools they have no business calling. `task`
        # (subagent delegation) is off globally, full stop — see
        # _no_delegation_tools() for why every agent also repeats this
        # explicitly rather than relying on this default alone.
        "tools": {"github_*": False, "task": False},
        "agent": _AGENT_DEFS,
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


async def run_opencode(ctx, instruction: str, *, continue_session: bool | None = None) -> dict:
    """Thin wrapper over common.opencode_runtime.run_opencode: resolves
    *this* agent kind's opencode agent name for the calling capability.
    Every capability calls it the same way:

        result = await ctx.tool("opencode")(ctx, "<instruction>")
    """
    agent = CAPABILITY_AGENTS[ctx.capability]
    return await _run_opencode(ctx, instruction, agent, continue_session=continue_session)
