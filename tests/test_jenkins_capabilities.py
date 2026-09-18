import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.capabilities_base import StepContext  # noqa: E402
from jenkins_agent.capabilities import (  # noqa: E402
    diagnose,
    execute_action,
    inspect,
    propose_action,
    resolve_target,
)
from jenkins_agent.tools import jenkins_rest, opencode_cli  # noqa: E402

_STUB_TRACE = {"elapsed_s": 0.0, "tool_call_counts": {}, "tool_call_total": 0, "slowest_gaps": []}

_JENKINS_CONFIG = {
    "base_url": "https://jenkins.example.com",
    "auth": {"type": "api_token", "user": "bot", "token": "tok"},
}


def _ctx(capability: str, results: dict, tools: dict, params: dict | None = None) -> StepContext:
    return StepContext(
        request_id="req-1",
        workspace="/tmp/ws",
        prompt="how's the checkout deploy doing?",
        config={
            "jenkins": _JENKINS_CONFIG,
            "llm": {"provider": "anthropic", "api_key": "sk-x", "model": "claude-sonnet-5"},
        },
        capability=capability,
        params=params or {},
        results=results,
        tools=tools,
    )


@pytest.mark.asyncio
async def test_resolve_target_writes_config_before_calling_opencode():
    write_config = Mock()
    opencode = AsyncMock(return_value={"text": "deploy/checkout-service/main", "trace": _STUB_TRACE})
    ctx = _ctx("resolve_target", results={}, tools={"opencode_config": write_config, "opencode": opencode})

    result = await resolve_target.run(ctx)

    write_config.assert_called_once_with("/tmp/ws", ctx.config["llm"], _JENKINS_CONFIG)
    assert result["resolution"] == "deploy/checkout-service/main"


@pytest.mark.asyncio
async def test_inspect_includes_the_resolved_target_in_its_instruction():
    opencode = AsyncMock(return_value={"text": "status: SUCCESS", "trace": _STUB_TRACE})
    ctx = _ctx(
        "inspect",
        results={"resolve_target": {"resolution": "deploy/checkout-service/main"}},
        tools={"opencode_config": Mock(), "opencode": opencode},
    )

    await inspect.run(ctx)

    instruction = opencode.await_args.args[1]
    assert "deploy/checkout-service/main" in instruction


@pytest.mark.asyncio
async def test_diagnose_returns_only_the_marked_cause_not_raw_narration():
    opencode = AsyncMock(
        return_value={
            "text": "Let's check the log.\nReading console output.\nSUMMARY:\nnpm test failed: 3 assertions.",
            "trace": _STUB_TRACE,
        }
    )
    ctx = _ctx(
        "diagnose",
        results={"resolve_target": {"resolution": "deploy/checkout-service/main #42"}},
        tools={"opencode_config": Mock(), "opencode": opencode},
    )

    result = await diagnose.run(ctx)

    assert result["cause"] == "npm test failed: 3 assertions."


@pytest.mark.asyncio
async def test_diagnose_issues_enforcement_followup_when_marker_missing():
    opencode = AsyncMock(
        side_effect=[
            {"text": "Reading the log now, looks like a timeout.", "trace": _STUB_TRACE},
            {"text": "Build timed out waiting for a test executor.", "trace": _STUB_TRACE},
        ]
    )
    ctx = _ctx("diagnose", results={}, tools={"opencode_config": Mock(), "opencode": opencode})

    result = await diagnose.run(ctx)

    assert result["cause"] == "Build timed out waiting for a test executor."
    assert opencode.await_count == 2
    _, kwargs = opencode.await_args_list[1]
    assert kwargs == {"continue_session": True}


@pytest.mark.asyncio
async def test_propose_action_extracts_the_proposal_json_into_a_pending_action():
    opencode = AsyncMock(
        return_value={
            "text": (
                "I'll trigger the deploy job.\n"
                "PROPOSAL_JSON:\n"
                '{"action": "trigger_build", "job": "deploy/checkout-service", '
                '"parameters": {"ENV": "staging"}, "reasoning": "user asked for staging"}'
            ),
            "trace": _STUB_TRACE,
        }
    )
    ctx = _ctx("propose_action", results={}, tools={"opencode_config": Mock(), "opencode": opencode})

    result = await propose_action.run(ctx)

    pending = result["pending_action"]
    assert pending["action"] == "trigger_build"
    assert pending["job"] == "deploy/checkout-service"
    assert pending["parameters"] == {"ENV": "staging"}
    assert pending["jenkins_url"] == _JENKINS_CONFIG["base_url"]
    assert "id" in pending and "created_at" in pending


@pytest.mark.asyncio
async def test_propose_action_raises_clearly_when_no_proposal_json_found():
    opencode = AsyncMock(return_value={"text": "I couldn't figure out which job you mean.", "trace": _STUB_TRACE})
    ctx = _ctx("propose_action", results={}, tools={"opencode_config": Mock(), "opencode": opencode})

    with pytest.raises(RuntimeError, match="could not extract a PROPOSAL_JSON"):
        await propose_action.run(ctx)


@pytest.mark.asyncio
async def test_execute_action_refuses_without_a_confirmed_action_in_params():
    ctx = _ctx("execute_action", results={}, tools={}, params={})

    with pytest.raises(RuntimeError, match="no confirmed pending_action"):
        await execute_action.run(ctx)


@pytest.mark.asyncio
async def test_execute_action_refuses_a_stale_confirmed_action():
    ctx = _ctx(
        "execute_action",
        results={},
        tools={},
        params={
            "action": "trigger_build",
            "job": "deploy/checkout-service",
            "created_at": "2020-01-01T00:00:00+00:00",
        },
    )

    with pytest.raises(RuntimeError, match="too stale"):
        await execute_action.run(ctx)


@pytest.mark.asyncio
async def test_execute_action_triggers_via_opencode_with_exact_confirmed_parameters():
    opencode = AsyncMock(return_value={"text": "triggered", "trace": _STUB_TRACE})
    ctx = _ctx(
        "execute_action",
        results={},
        tools={"opencode_config": Mock(), "opencode": opencode},
        params={"action": "trigger_build", "job": "deploy/checkout-service", "parameters": {"ENV": "staging"}},
    )

    result = await execute_action.run(ctx)

    instruction = opencode.await_args.args[1]
    assert "deploy/checkout-service" in instruction
    assert "ENV" in instruction and "staging" in instruction
    assert result["result"]["action"] == "trigger_build"


@pytest.mark.asyncio
async def test_execute_action_stops_via_direct_rest_not_opencode():
    stop_build = AsyncMock()
    ctx = _ctx(
        "execute_action",
        results={},
        tools={"jenkins_stop_build": stop_build},
        params={"action": "stop_build", "job": "deploy/checkout-service", "build_number": 42},
    )

    result = await execute_action.run(ctx)

    stop_build.assert_awaited_once_with(_JENKINS_CONFIG, "deploy/checkout-service", 42)
    assert result["result"] == {"action": "stop_build", "job": "deploy/checkout-service", "build_number": 42}


def test_only_ari_jenkins_act_can_call_a_mutating_jenkins_tool():
    assert opencode_cli._AGENT_DEFS["ari-jenkins-read"]["tools"]["jenkins_*"] is False
    assert opencode_cli._AGENT_DEFS["ari-jenkins-act"]["tools"]["jenkins_*"] is True
    for name, definition in opencode_cli._AGENT_DEFS.items():
        assert definition["mode"] == "primary", f"{name} must be mode=primary or --agent silently ignores it"
        assert definition["tools"]["task"] is False
        assert definition["permission"]["edit"] == "deny"
        assert definition["permission"]["bash"] == "deny"


def test_jenkins_rest_job_url_handles_nested_folders():
    url = jenkins_rest.job_url("https://jenkins.example.com", "team/deploy/checkout-service")
    assert url == "https://jenkins.example.com/job/team/job/deploy/job/checkout-service"


def test_jenkins_rest_auth_header_basic_for_api_token():
    header = jenkins_rest._auth_header({"type": "api_token", "user": "bot", "token": "tok"})
    assert header.startswith("Basic ")


def test_jenkins_rest_auth_header_bearer():
    header = jenkins_rest._auth_header({"type": "bearer", "token": "tok"})
    assert header == "Bearer tok"
