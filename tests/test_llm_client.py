import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anthropic
import httpx
import openai
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common import planner  # noqa: E402
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


def _status_error(cls, status, message):
    response = httpx.Response(status, request=httpx.Request("POST", "https://llm.example.com/v1"))
    return cls(message, response=response, body=None)


def _failing_openai(exc):
    return SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=exc)))


@pytest.mark.asyncio
async def test_a_rejected_key_error_carries_no_part_of_the_key():
    # The shape OpenAI really answers a wrong key with: first and last characters of the key intact.
    exc = _status_error(
        openai.AuthenticationError,
        401,
        "Error code: 401 - {'error': {'message': 'Incorrect API key provided: sk-proj-****************abcd. "
        "You can find your API key at https://platform.openai.com/account/api-keys.'}}",
    )
    with patch.object(llm_client, "AsyncOpenAI", return_value=_failing_openai(exc)):
        with pytest.raises(llm_client.LLMProviderError) as raised:
            await llm_client.complete({"provider": "openai", "api_key": "sk-x", "model": "m"}, "s", "u")

    assert str(raised.value) == "openai rejected the API key (HTTP 401)"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.asyncio
async def test_other_provider_errors_keep_their_detail_with_keys_masked():
    exc = _status_error(
        anthropic.BadRequestError,
        400,
        "model claude-nope not found; api key used: sk-ant-api03-abcdefgh, also sk-proj-****wxyz",
    )
    fake = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=exc)))
    with patch.object(llm_client, "AsyncAnthropic", return_value=fake):
        with pytest.raises(llm_client.LLMProviderError) as raised:
            await llm_client.complete({"provider": "anthropic", "api_key": "k", "model": "m"}, "s", "u")

    text = str(raised.value)
    assert text.startswith("anthropic returned HTTP 400: model claude-nope not found")
    assert "abcdefgh" not in text and "wxyz" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message, secret",
    [
        ("api_key=abc123def456 is not valid", "abc123def456"),
        ("upstream said: Authorization: Bearer eyJhbGciOi.payload.sig failed", "eyJhbGciOi"),
        ("key 0123456789abcdef0123456789abcdef rejected", "0123456789abcdef0123456789abcdef"),
        ("gsk_abcdefghijk is invalid", "gsk_abcdefghijk"),
    ],
)
async def test_key_shapes_in_other_errors_are_masked(message, secret):
    exc = _status_error(openai.BadRequestError, 400, message)
    with patch.object(llm_client, "AsyncOpenAI", return_value=_failing_openai(exc)):
        with pytest.raises(llm_client.LLMProviderError) as raised:
            await llm_client.complete({"provider": "openai", "api_key": "k", "model": "m"}, "s", "u")
    assert secret not in str(raised.value)


@pytest.mark.asyncio
async def test_an_api_key_mention_without_a_key_is_left_readable():
    exc = _status_error(openai.RateLimitError, 429, "The api key for model gpt-4o: rate limit exceeded")
    with patch.object(llm_client, "AsyncOpenAI", return_value=_failing_openai(exc)):
        with pytest.raises(llm_client.LLMProviderError) as raised:
            await llm_client.complete({"provider": "openai", "api_key": "k", "model": "m"}, "s", "u")
    assert str(raised.value).endswith("The api key for model gpt-4o: rate limit exceeded")


@pytest.mark.asyncio
async def test_a_planning_failure_from_a_rejected_key_names_no_key():
    exc = _status_error(openai.AuthenticationError, 401, "Incorrect API key provided: sk-proj-****abcd.")
    config = {"llm": {"provider": "openai", "api_key": "sk-x", "model": "m"}}
    with patch.object(llm_client, "AsyncOpenAI", return_value=_failing_openai(exc)):
        with pytest.raises(llm_client.LLMProviderError) as raised:
            await planner.create_plan("check the pods", config)

    assert "abcd" not in str(raised.value)
