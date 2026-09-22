import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ari_oncall_agent.tools.opencode_cli as opencode_cli  # noqa: E402
from ari_oncall_agent.tools.opencode_cli import (  # noqa: E402
    _AGENT_DEFS,
    CAPABILITY_AGENTS,
    run_opencode,
    write_config,
)


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
    # verify's bash commands (mvn/go/npm) need a persistent, shared HOME
    # so their dependency caches survive across requests — see
    # build_cache.py/settings.build_cache_root. Regression coverage for
    # the live finding that verify had zero dependency caching, making it
    # slow enough to routinely outlast callers' own patience timeouts.
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
    assert kwargs["extra_env"] == {"HOME": "/fake/build-cache"}


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
