import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.capabilities_base import StepContext  # noqa: E402
from common.opencode_runtime import _build_trace, _parse_events, merge_traces, run_opencode  # noqa: E402

# Captured verbatim (minus the huge nested responseHeaders blob) from a live
# `opencode run --format json` invocation against a real opencode v1.18.31
# binary with a deliberately invalid OpenAI key — see opencode_runtime.py's
# module docstring. This is the actual shape opencode emits on stdout for a
# fatal error; stderr was empty for the same run.
REAL_ERROR_EVENT_LINE = json.dumps(
    {
        "type": "error",
        "timestamp": 1789502353830,
        "sessionID": "ses_f59591234ffekY85D1lYOz6MEt",
        "error": {
            "name": "APIError",
            "data": {
                "message": (
                    "Incorrect API key provided: sk-bogus*****-key. You can find your "
                    "API key at https://platform.openai.com/account/api-keys."
                ),
                "statusCode": 401,
                "isRetryable": False,
            },
        },
    }
)


def test_parse_events_extracts_real_fatal_error_from_stdout():
    result = _parse_events(REAL_ERROR_EVENT_LINE.encode())
    assert result["error"] is not None
    assert "Incorrect API key" in result["error"]
    assert result["text"] == ""


def test_parse_events_concatenates_text_parts():
    events = [
        {"type": "text", "text": "root cause: "},
        {"type": "step-start"},
        {"type": "text", "text": "null pointer in foo.py:42"},
    ]
    raw = "\n".join(json.dumps(e) for e in events).encode()
    result = _parse_events(raw)
    assert result["text"] == "root cause: \nnull pointer in foo.py:42"
    assert result["error"] is None


def test_parse_events_extracts_tool_calls():
    events = [
        {
            "type": "tool",
            "tool": "github_create_pull_request",
            "callID": "call_1",
            "state": {"status": "completed", "input": {"title": "fix"}, "output": {"html_url": "https://github.com/x/y/pull/9"}},
        }
    ]
    raw = "\n".join(json.dumps(e) for e in events).encode()
    result = _parse_events(raw)
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["tool"] == "github_create_pull_request"
    assert result["tool_calls"][0]["output"]["html_url"].endswith("/pull/9")


def test_parse_events_skips_unparseable_lines_without_raising():
    raw = b"not json\n{\"type\": \"text\", \"text\": \"ok\"}\n\n"
    result = _parse_events(raw)
    assert result["text"] == "ok"


def test_build_trace_counts_tools_and_finds_the_biggest_gap():
    # This is the exact signal that used to require a one-off manual
    # reproduction to find (docs/MEMORY.md, 2026-09-17: "4 delegated task
    # calls ate 68% of one run") — now computed automatically from
    # opencode's own event timestamps on every real call.
    events = [
        {"type": "step-start", "timestamp": 1000},
        {"type": "tool", "tool": "read", "timestamp": 1200, "state": {"status": "completed"}},
        {"type": "tool", "tool": "read", "timestamp": 1500, "state": {"status": "completed"}},
        {"type": "tool", "tool": "task", "timestamp": 8000, "state": {"status": "completed"}},
    ]

    trace = _build_trace(events, wall_elapsed_s=12.3)

    assert trace["elapsed_s"] == 12.3
    assert trace["tool_call_counts"] == {"read": 2, "task": 1}
    assert trace["tool_call_total"] == 3
    assert trace["slowest_gaps"][0] == {"tool": "task", "at_s": 7.0, "preceded_by_gap_s": 6.5}


def test_build_trace_handles_no_tool_calls():
    events = [{"type": "step-start", "timestamp": 1000}, {"type": "text", "timestamp": 1050, "text": "done"}]

    trace = _build_trace(events, wall_elapsed_s=1.0)

    assert trace["tool_call_counts"] == {}
    assert trace["tool_call_total"] == 0
    assert trace["slowest_gaps"] == []


def test_merge_traces_combines_elapsed_counts_and_top_gaps():
    first = {
        "elapsed_s": 5.0,
        "tool_call_counts": {"read": 2},
        "tool_call_total": 2,
        "slowest_gaps": [{"tool": "read", "at_s": 1.0, "preceded_by_gap_s": 3.0}],
    }
    second = {
        "elapsed_s": 2.5,
        "tool_call_counts": {"read": 1, "edit": 1},
        "tool_call_total": 2,
        "slowest_gaps": [{"tool": "edit", "at_s": 0.5, "preceded_by_gap_s": 1.0}],
    }

    merged = merge_traces(first, second)

    assert merged["elapsed_s"] == 7.5
    assert merged["tool_call_counts"] == {"read": 3, "edit": 1}
    assert merged["tool_call_total"] == 4
    assert merged["slowest_gaps"][0]["preceded_by_gap_s"] == 3.0


def _ctx(workspace: str) -> StepContext:
    return StepContext(
        request_id="req-1",
        workspace=workspace,
        prompt="fix the bug",
        config={"llm": {"provider": "anthropic", "api_key": "sk-x", "model": "claude-x"}},
        capability="analyze",
        params={},
        results={},
    )


class _FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"", b""


@pytest.mark.asyncio
async def test_run_opencode_isolates_xdg_state_per_request_workspace(tmp_path, monkeypatch):
    # Regression test for the live "database is locked" failure: two
    # concurrent requests' opencode subprocesses must never share
    # XDG_DATA_HOME (and therefore never share opencode's own local
    # session/auth SQLite state), see the comment in
    # opencode_runtime.run_opencode. Real $HOME is deliberately left alone
    # so verify's build-tool dependency caches (~/.m2, ~/go, ~/.npm) stay
    # shared/warm across requests.
    captured_env = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured_env.update(kwargs["env"])
        return _FakeProcess()

    monkeypatch.setattr("common.opencode_runtime.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    workspace = tmp_path / "req-1-workspace"
    workspace.mkdir()
    ctx = _ctx(str(workspace))

    await run_opencode(ctx, "investigate", "ari-analyze")

    expected_state_dir = str(workspace / ".opencode-state")
    assert "HOME" not in captured_env or captured_env["HOME"] == os.environ.get("HOME")
    assert captured_env["XDG_DATA_HOME"] == str(Path(expected_state_dir) / "data")
    assert captured_env["XDG_CONFIG_HOME"] == str(Path(expected_state_dir) / "config")
    assert Path(expected_state_dir).is_dir()


@pytest.mark.asyncio
async def test_run_opencode_state_isolation_differs_across_requests(tmp_path, monkeypatch):
    captured_envs = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured_envs.append(kwargs["env"])
        return _FakeProcess()

    monkeypatch.setattr("common.opencode_runtime.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    ws_a = tmp_path / "request-a"
    ws_b = tmp_path / "request-b"
    ws_a.mkdir()
    ws_b.mkdir()

    await run_opencode(_ctx(str(ws_a)), "investigate", "ari-analyze")
    await run_opencode(_ctx(str(ws_b)), "investigate", "ari-analyze")

    assert captured_envs[0]["XDG_DATA_HOME"] != captured_envs[1]["XDG_DATA_HOME"]


@pytest.mark.asyncio
async def test_run_opencode_extra_env_overrides_state_isolation_defaults(tmp_path, monkeypatch):
    captured_env = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured_env.update(kwargs["env"])
        return _FakeProcess()

    monkeypatch.setattr("common.opencode_runtime.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    workspace = tmp_path / "req-1-workspace"
    workspace.mkdir()

    await run_opencode(
        _ctx(str(workspace)), "investigate", "ari-analyze", extra_env={"XDG_DATA_HOME": "/custom/data"}
    )

    assert captured_env["XDG_DATA_HOME"] == "/custom/data"


def test_the_worker_image_runs_an_init_as_pid_1():
    # opencode spawns its own `git` children. When an activity is cancelled
    # or times out and opencode goes away, any still running are orphaned
    # and reparented to pid 1. Python reaps only what it spawned itself --
    # asyncio's child watcher tracks those by pid -- so an inherited orphan
    # is never waited on and stays a zombie for the life of the pod.
    #
    # Measured on the bastion, 2026-10-09: a worker up 3h35m had 15
    # `[git] <defunct>` entries parented to `python -m api.worker` as pid 1,
    # and all 15 went away when that container was removed. The end state is
    # pid exhaustion, slowly enough that nobody would connect it to this.
    #
    # Guarded here rather than left to review because the failure is silent,
    # cumulative, and invisible to every other test in this suite: they all
    # assert on what one call returns, and this shows up only in `ps`.
    from pathlib import Path

    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile.oncall"
    source = dockerfile.read_text(encoding="utf-8")

    entrypoints = [line for line in source.splitlines() if line.startswith("ENTRYPOINT")]
    assert entrypoints, (
        "Dockerfile.oncall has no ENTRYPOINT, so CMD becomes pid 1 and nothing reaps the "
        "orphaned git children opencode leaves behind"
    )
    assert any("tini" in line for line in entrypoints), (
        f"pid 1 must be an init that reaps orphans; got {entrypoints}"
    )
    assert "tini" in source.split("ENTRYPOINT")[0], "tini must also be installed, not just invoked"
