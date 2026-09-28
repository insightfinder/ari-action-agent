import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.llm import client as llm_client  # noqa: E402


@pytest.mark.asyncio
async def test_openai_complete_uses_responses_api_and_returns_output_text():
    fake_response = SimpleNamespace(output_text="the plan")
    fake_client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=fake_response)))

    with patch.object(llm_client, "AsyncOpenAI", return_value=fake_client) as ctor:
        result = await llm_client.complete(
            {"provider": "openai", "api_key": "sk-test", "model": "gpt-5"},
            "system prompt",
            "user prompt",
        )

    ctor.assert_called_once_with(
        api_key="sk-test", base_url=None, timeout=llm_client._TIMEOUT_SECONDS, max_retries=llm_client._MAX_RETRIES
    )
    fake_client.responses.create.assert_awaited_once_with(
        model="gpt-5", instructions="system prompt", input="user prompt"
    )
    assert result == "the plan"


@pytest.mark.asyncio
async def test_anthropic_complete_uses_messages_api_and_concatenates_text_blocks():
    blocks = [
        SimpleNamespace(type="text", text="hello "),
        SimpleNamespace(type="tool_use", text=None),
        SimpleNamespace(type="text", text="world"),
    ]
    fake_response = SimpleNamespace(content=blocks)
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=fake_response)))

    with patch.object(llm_client, "AsyncAnthropic", return_value=fake_client) as ctor:
        result = await llm_client.complete(
            {"provider": "anthropic", "api_key": "sk-ant-test", "model": "claude-opus-5"},
            "system prompt",
            "user prompt",
        )

    ctor.assert_called_once_with(
        api_key="sk-ant-test", base_url=None, timeout=llm_client._TIMEOUT_SECONDS, max_retries=llm_client._MAX_RETRIES
    )
    fake_client.messages.create.assert_awaited_once_with(
        model="claude-opus-5",
        max_tokens=4096,
        system="system prompt",
        messages=[{"role": "user", "content": "user prompt"}],
    )
    assert result == "hello world"


@pytest.mark.asyncio
async def test_unsupported_provider_raises():
    with pytest.raises(ValueError, match="Unsupported llm.provider"):
        await llm_client.complete({"provider": "cohere", "api_key": "x", "model": "y"}, "s", "u")


def test_the_sdk_deadline_fits_inside_the_activity_that_runs_it():
    """Both SDKs default to a 600s read timeout with max_retries=2 —
    measured in the deployed image (openai 3.19.2, anthropic 1.8.0) — so
    an unconfigured planner call could occupy 30 minutes, outliving the
    1200s `start_to_close_timeout` of the activity running it. Whatever
    these are tuned to, the worst case has to stay inside that budget.
    """
    worst_case = llm_client._TIMEOUT_SECONDS * (llm_client._MAX_RETRIES + 1)
    assert worst_case < 1200, f"worst-case planner call is {worst_case}s, past the activity's 1200s timeout"
