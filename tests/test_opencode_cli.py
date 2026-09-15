import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_agent.tools.opencode_cli import (  # noqa: E402
    _AGENT_DEFS,
    CAPABILITY_AGENTS,
    _parse_events,
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
