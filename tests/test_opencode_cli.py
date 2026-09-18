import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_agent.tools.opencode_cli import (  # noqa: E402
    _AGENT_DEFS,
    CAPABILITY_AGENTS,
    _build_trace,
    _parse_events,
    merge_traces,
    write_config,
)

# Captured verbatim (minus the huge nested responseHeaders blob) from a live
# `opencode run --format json` invocation against a real opencode v1.18.31
# binary with a deliberately invalid OpenAI key — see tools/opencode_cli.py's
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


def test_ari_analyze_and_ari_change_cannot_commit_or_push():
    # Only ari-publish should be able to mutate git state (commit/push/
    # branch) — analyze is read-only by design, and change's job is
    # editing files, not staging/committing them (that's publish_pr's
    # instruction to ari-publish: "Stage and commit exactly these files").
    for name in ("ari-analyze", "ari-change"):
        bash_perms = _AGENT_DEFS[name]["permission"]["bash"]
        assert "git *" not in bash_perms, f"{name} must not have blanket git access"
        assert bash_perms["*"] == "deny"
        # Only read-only git subcommands may be allowed.
        for pattern, mode in bash_perms.items():
            if pattern == "*":
                continue
            assert mode == "allow"
            assert pattern.split()[1].rstrip("*") in {"diff", "status", "log", "show", "blame"}
