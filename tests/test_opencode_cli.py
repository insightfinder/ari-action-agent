import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ari_oncall_agent.protected_branches as protected_branches_module  # noqa: E402
import ari_oncall_agent.tools.opencode_cli as opencode_cli  # noqa: E402
from ari_oncall_agent.tools.opencode_cli import (  # noqa: E402
    _AGENT_DEFS,
    _protected_branch_push_denies,
    CAPABILITY_AGENTS,
    run_opencode,
    write_config,
)


@pytest.fixture
def isolated_protected_branches_config(tmp_path, monkeypatch):
    """Points protected_branches.py's loader at a throwaway config instead
    of the real configs/oncall/protected_branches.yaml — some tests need
    to assert on an *unconfigured* repo getting exactly the defaults and
    nothing more, which would otherwise be at the mercy of whatever an
    operator has since added to that shared, editable file."""
    path = tmp_path / "protected_branches.yaml"
    path.write_text("default_protected_branches: [main, master]\nprotected_branches: {}\n")
    monkeypatch.setattr(protected_branches_module, "config_file", lambda name: path)
    protected_branches_module._load.cache_clear()
    yield
    protected_branches_module._load.cache_clear()

# From configs/oncall/protected_branches.yaml -- a real configured repo,
# not a fixture, since these tests exercise the actual blacklist.
_PROTECTED_REPO = {"url": "https://github.com/example-org/example-repo", "pat": "ghp_x"}


def test_agent_defs_all_use_primary_mode():
    # Regression test for a real bug found by running against a live
    # opencode binary: with mode="subagent", `opencode run --agent <name>`
    # silently falls back to the default "build" agent instead of using
    # ours (and does not error), which would have made every permission
    # scope in this file a no-op.
    for name, definition in _AGENT_DEFS.items():
        assert definition["mode"] == "primary", f"{name} must be mode=primary or --agent silently ignores it"


def test_capability_agent_mapping_covers_every_defined_agent():
    assert set(CAPABILITY_AGENTS.values()) == set(_AGENT_DEFS.keys())


def test_write_config_includes_mcp_only_when_pat_present(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)

    with_pat = write_config(
        str(tmp_path), {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"}, {"url": "https://github.com/x/y", "pat": "ghp_x"}
    )
    cfg = json.loads(Path(with_pat).read_text())
    assert cfg["mcp"]["github"]["headers"]["Authorization"] == "Bearer ghp_x"
    assert cfg["mcp"]["github"]["oauth"] is False
    assert set(cfg["agent"].keys()) == set(CAPABILITY_AGENTS.values())

    without_pat = write_config(
        str(tmp_path), {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"}, {"url": "https://github.com/x/y"}
    )
    cfg2 = json.loads(Path(without_pat).read_text())
    assert "mcp" not in cfg2


def test_write_config_git_excludes_itself(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    write_config(str(tmp_path), {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"}, {"url": "https://github.com/x/y"})
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert "opencode.json" in exclude


def test_every_agent_disables_the_task_delegation_tool():
    # Real bug, not theoretical: opencode's built-in "task" tool lets an
    # agent delegate to a subagent that runs OUTSIDE the delegating
    # agent's own `permission` block. A reproduction of ari-change used it
    # to hand off "commit and open a PR" to a general-purpose subagent
    # that then ran `gh pr create` — a command ari-change's own bash
    # permission would have denied outright — and it dominated wall-clock
    # time (4 delegated calls were ~68% of a ~400s run). Every agent must
    # disable it explicitly, not rely on a single global default.
    for name, definition in _AGENT_DEFS.items():
        assert definition.get("tools", {}).get("task") is False, f"{name} must disable the task tool"


def test_write_config_disables_task_tool_globally_too(tmp_path):
    path = write_config(
        str(tmp_path), {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"}, {"url": "https://github.com/x/y"}
    )
    cfg = json.loads(Path(path).read_text())
    assert cfg["tools"]["task"] is False


@pytest.mark.asyncio
async def test_run_opencode_points_verify_at_the_shared_build_cache_home(monkeypatch):
    # verify's bash commands (mvn/go/npm) need a persistent, shared cache
    # so their dependencies survive across requests — see
    # build_cache.py/settings.build_cache_root. Regression coverage for
    # the live finding that verify had zero dependency caching, making it
    # slow enough to routinely outlast callers' own patience timeouts.
    #
    # This used to assert the whole env equalled {"HOME": ...}, which pinned
    # the half-working version: HOME alone moves npm and Go but not Maven,
    # whose repository the JVM resolves from the passwd database. The equality
    # is gone on purpose — asserting the exact set of variables would fail
    # again the next time a build tool needs one — and the per-tool
    # expectations live in their own cases below.
    @asynccontextmanager
    async def fake_reserve_build_cache_home():
        yield "/fake/build-cache"

    monkeypatch.setattr(opencode_cli, "reserve_build_cache_home", fake_reserve_build_cache_home)
    fake_run_opencode = AsyncMock(return_value={"text": "ok", "trace": {}})
    monkeypatch.setattr(opencode_cli, "_run_opencode", fake_run_opencode)

    ctx = SimpleNamespace(capability="verify")
    await run_opencode(ctx, "run the test suite")

    fake_run_opencode.assert_awaited_once()
    _, kwargs = fake_run_opencode.await_args
    assert kwargs["extra_env"]["HOME"] == "/fake/build-cache"
    assert "/fake/build-cache" in kwargs["extra_env"]["MAVEN_OPTS"]


@pytest.mark.asyncio
async def test_run_opencode_leaves_home_untouched_for_other_capabilities(monkeypatch):
    # Only verify shells out to build tooling — generate_change/publish_pr
    # have no business sharing state through a real $HOME (and opencode's
    # own state is isolated separately, per-request, in
    # opencode_runtime.py). The build-cache reservation (with its own
    # eviction sweep + lock acquisition) must not even be entered for them.
    def fail_if_called():
        raise AssertionError("reserve_build_cache_home must not be used outside verify")

    monkeypatch.setattr(opencode_cli, "reserve_build_cache_home", fail_if_called)
    fake_run_opencode = AsyncMock(return_value={"text": "ok", "trace": {}})
    monkeypatch.setattr(opencode_cli, "_run_opencode", fake_run_opencode)

    ctx = SimpleNamespace(capability="generate_change")
    await run_opencode(ctx, "investigate and edit")

    _, kwargs = fake_run_opencode.await_args
    assert kwargs.get("extra_env") is None


def test_ari_verify_denies_git_push_unconditionally():
    # verify has no legitimate reason to push anything, ever, regardless
    # of branch -- unconditional, not tied to protected_branches.yaml,
    # since its bash is otherwise fully open for arbitrary build tooling.
    bash_perms = _AGENT_DEFS["ari-verify"]["permission"]["bash"]
    assert bash_perms["git push*"] == "deny"
    assert bash_perms["*"] == "allow"


def test_protected_branch_push_denies_covers_common_push_forms():
    denies = _protected_branch_push_denies(_PROTECTED_REPO["url"])

    assert denies["git push origin dev*"] == "deny"
    assert denies["git push origin staging*"] == "deny"
    assert denies["git push origin test*"] == "deny"
    assert denies["git push origin HEAD:dev*"] == "deny"
    assert denies["git push --force origin dev*"] == "deny"
    assert denies["git push origin dev:dev*"] == "deny"
    # main/master come from default_protected_branches (protected_branches.py)
    # and apply to every repo -- must be included here too, on top of this
    # repo's own dev/staging/test entry.
    assert denies["git push origin main*"] == "deny"
    assert denies["git push origin master*"] == "deny"


def test_protected_branch_push_denies_still_covers_main_and_master_for_an_unconfigured_repo(
    isolated_protected_branches_config,
):
    # No entry under protected_branches: at all -- main/master are still
    # denied by default (protected_branches.py's default_protected_branches).
    # Isolated config so this doesn't depend on whatever an operator has
    # since added to the real configs/oncall/protected_branches.yaml.
    denies = _protected_branch_push_denies("https://github.com/some-org/unrelated-repo")

    assert denies["git push origin main*"] == "deny"
    assert denies["git push origin master*"] == "deny"
    assert not any("dev" in pattern or "staging" in pattern for pattern in denies)


def test_write_config_adds_protected_branch_denies_for_a_configured_repo(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)

    path = write_config(str(tmp_path), {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"}, _PROTECTED_REPO)

    cfg = json.loads(Path(path).read_text())
    publish_bash = cfg["agent"]["ari-publish"]["permission"]["bash"]
    assert publish_bash["git push origin dev*"] == "deny"
    assert publish_bash["git push origin staging*"] == "deny"
    assert publish_bash["git push origin test*"] == "deny"
    # The blanket allow (needed to push a fresh ari/... branch) is still
    # there -- only the specific protected-branch forms are denied.
    assert publish_bash["git *"] == "allow"
    # Every other agent is untouched by this repo-specific patch.
    assert cfg["agent"]["ari-change"]["permission"]["bash"] == _AGENT_DEFS["ari-change"]["permission"]["bash"]


def test_write_config_still_denies_main_and_master_for_a_repo_with_no_own_entry(
    tmp_path, isolated_protected_branches_config
):
    # main/master are protected for every repo by default (see
    # protected_branches.py) — a repo with no entry of its own under
    # protected_branches: still must not get the untouched, unpatched
    # default bash permission. Isolated config (main/master only, no
    # per-repo entries) so this doesn't depend on whatever an operator has
    # since added to the real configs/oncall/protected_branches.yaml.
    (tmp_path / ".git" / "info").mkdir(parents=True)

    path = write_config(
        str(tmp_path), {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"}, {"url": "https://github.com/x/y", "pat": "t"}
    )

    cfg = json.loads(Path(path).read_text())
    publish_bash = cfg["agent"]["ari-publish"]["permission"]["bash"]
    assert publish_bash != _AGENT_DEFS["ari-publish"]["permission"]["bash"]
    assert publish_bash["git push origin main*"] == "deny"
    assert publish_bash["git push origin master*"] == "deny"
    assert not any("dev" in pattern or "staging" in pattern for pattern in publish_bash)


def test_write_config_never_leaks_one_repos_denies_into_anothers(tmp_path, monkeypatch):
    # A repo with its own extra protected branch, called first, must not
    # leak those denies into a later call for an unrelated repo (a
    # shared-mutable-dict bug would do exactly this). Isolated config with
    # an explicit per-repo entry, so the "no leak" assertion is actually
    # meaningful rather than trivially true.
    config_path = tmp_path / "protected_branches.yaml"
    config_path.write_text(
        "default_protected_branches: [main, master]\n"
        "protected_branches:\n"
        "  https://github.com/example-org/example-repo: [canary]\n"
    )
    monkeypatch.setattr(protected_branches_module, "config_file", lambda name: config_path)
    protected_branches_module._load.cache_clear()

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git" / "info").mkdir(parents=True)
    try:
        write_config(
            str(repo_dir),
            {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"},
            {"url": "https://github.com/example-org/example-repo", "pat": "ghp_x"},
        )
        path = write_config(
            str(repo_dir), {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"}, {"url": "https://github.com/x/y", "pat": "t"}
        )

        cfg = json.loads(Path(path).read_text())
        publish_bash = cfg["agent"]["ari-publish"]["permission"]["bash"]
        assert not any("canary" in pattern for pattern in publish_bash)
        assert publish_bash["git push origin main*"] == "deny"
    finally:
        protected_branches_module._load.cache_clear()


def test_ari_change_cannot_commit_or_push():
    # Only ari-publish should be able to mutate git state (commit/push/
    # branch) — ari-change's job is investigating and editing files, not
    # staging/committing them (that's publish_pr's instruction to
    # ari-publish: "Stage and commit exactly these files").
    bash_perms = _AGENT_DEFS["ari-change"]["permission"]["bash"]
    assert "git *" not in bash_perms, "ari-change must not have blanket git access"
    assert bash_perms["*"] == "deny"
    # Only read-only git subcommands may be allowed.
    for pattern, mode in bash_perms.items():
        if pattern == "*":
            continue
        assert mode == "allow"
        assert pattern.split()[1].rstrip("*") in {"diff", "status", "log", "show", "blame"}


# --- the build cache each tool actually reads ---------------------------


def test_maven_is_pointed_at_the_cache_explicitly_not_via_home():
    # The JVM does not take user.home from $HOME -- measured in the worker,
    # 2026-10-10: `HOME=/var/lib/ari-agent/build-cache java
    # -XshowSettings:properties` still reports user.home = /root. So setting
    # HOME alone left 1.5 GB of Maven downloads on the container filesystem
    # while the persistent volume held only .npm, and every fresh worker paid
    # that download again inside verify's 480s budget.
    env = opencode_cli._build_cache_env("/cache")

    expected = os.path.join("/cache", ".m2", "repository")
    assert f"-Dmaven.repo.local={expected}" in env["MAVEN_OPTS"]


def test_the_heap_limit_from_the_image_is_kept(monkeypatch):
    # Dockerfile.oncall sets MAVEN_OPTS=-Xmx2g; replacing it instead of
    # appending would quietly drop the heap cap on every build.
    monkeypatch.setenv("MAVEN_OPTS", "-Xmx2g")
    env = opencode_cli._build_cache_env("/cache")

    assert "-Xmx2g" in env["MAVEN_OPTS"]
    assert "-Dmaven.repo.local=" in env["MAVEN_OPTS"]


def test_home_is_still_set_for_the_tools_that_do_read_it():
    # npm and Go resolve their caches from $HOME and were already working.
    env = opencode_cli._build_cache_env("/cache")
    assert env["HOME"] == "/cache"


def test_gradle_gets_its_own_variable_for_the_same_reason():
    env = opencode_cli._build_cache_env("/cache")
    assert "/cache" in env["GRADLE_USER_HOME"]
