"""Per-request LLM client factory: ARI's own model calls — the planner
(`common/planner.py`), the shared `respond` step, and every capability that
calls the `llm_complete` tool (configs/<kind>/tools.yaml). There is
deliberately no global/shared LLM key anywhere in this service — every call
is made with the provider, model, and API key carried in *that request's*
own `config.llm`, so a fresh SDK client is constructed per call rather than
cached.

Uses the official SDKs (openai>=3.14, anthropic>=1.6) rather than raw HTTP,
so we get their retry/timeout machinery and stay compatible as the APIs
evolve. Their *defaults*, however, are wrong for us and are overridden
below. Measured in the deployed image (openai 3.19.2, anthropic 1.8.0):
both ship `Timeout(connect=5, read=600, write=600, pool=600)` with
`max_retries=2`, so a hung planner call could occupy 600s x 3 = 30
minutes — longer than the 1200s `start_to_close_timeout` the activity
running it is given, meaning Temporal would kill the activity while the
SDK was still patiently waiting. Those defaults are tuned for a batch
script with no deadline of its own; this call has one.

This is unrelated to opencode's own LLM calls (opencode manages those
itself via the `provider.apiKey` written into its per-request
opencode.json — see tools/opencode_cli.py); this client exists solely for
ARI's own calls.
"""

import os

import anthropic
import openai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from opentelemetry.trace import SpanKind

from common import tracing
from common.redaction import redact_secrets

# 120s x 3 attempts = 6 minutes worst case, comfortably inside the
# activity budget with room for the rest of the step. The planner turns
# one prompt into a step plan — a few hundred tokens out — so a call
# still running after two minutes is stuck, not slow.
_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))


class LLMProviderError(RuntimeError):
    """A provider's error response, worded so that it carries no part of the API key."""


def _provider_error(provider: str, exc: openai.APIStatusError | anthropic.APIStatusError) -> LLMProviderError:
    status = exc.status_code
    if status == 401:
        # The body of a rejected-key response is about the key, and nothing else in it helps.
        return LLMProviderError(f"{provider} rejected the API key (HTTP 401)")
    # The error text can quote the key it was sent; it reaches the reply and Temporal history.
    detail = redact_secrets(str(exc.message))
    return LLMProviderError(f"{provider} returned HTTP {status}: {detail[:1000]}")


async def complete(llm: dict, system_prompt: str, user_prompt: str) -> str:
    provider = llm["provider"].lower()
    if provider == "openai":
        call = _openai_complete
    elif provider == "anthropic":
        call = _anthropic_complete
    else:
        raise ValueError(f"Unsupported llm.provider: {provider!r}")

    if not tracing.active():
        text, _ = await _call(provider, call, llm, system_prompt, user_prompt)
        return text

    # One model call, so the span gets the token attributes. Keys follow the
    # traceloop spans mcp-service's own LLM calls produce. A provider error is
    # reworded inside the span, so the span records the key-free message.
    with tracing.span(
        f"llm:{provider}/{llm['model']}",
        kind=SpanKind.CLIENT,
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.system": provider,
            "gen_ai.request.model": llm["model"],
            "gen_ai.prompt.0.role": "system",
            "gen_ai.prompt.0.content": tracing.truncate(system_prompt),
            "gen_ai.prompt.1.role": "user",
            "gen_ai.prompt.1.content": tracing.truncate(user_prompt),
        },
    ) as span:
        text, usage = await _call(provider, call, llm, system_prompt, user_prompt)
        tracing.set_attributes(span, {
            "gen_ai.response.model": usage.get("model"),
            "gen_ai.completion.0.role": "assistant",
            "gen_ai.completion.0.content": tracing.truncate(text),
        })
        tracing.set_token_attributes(
            span, usage.get("input_tokens"), usage.get("output_tokens"),
            usage.get("cache_read"), usage.get("cache_write"), llm["model"],
        )
        return text


async def _call(provider: str, call, llm: dict, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
    try:
        return await call(llm, system_prompt, user_prompt)
    except (openai.APIStatusError, anthropic.APIStatusError) as exc:
        # `from None`: the SDK exception, message and all, must not ride along as the cause.
        raise _provider_error(provider, exc) from None


async def _openai_complete(llm: dict, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
    client = AsyncOpenAI(
        api_key=llm["api_key"],
        base_url=llm.get("base_url"),
        timeout=_TIMEOUT_SECONDS,
        max_retries=_MAX_RETRIES,
    )
    response = await client.responses.create(
        model=llm["model"],
        instructions=system_prompt,
        input=user_prompt,
    )
    return response.output_text, _openai_usage(response)


async def _anthropic_complete(llm: dict, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
    client = AsyncAnthropic(
        api_key=llm["api_key"],
        base_url=llm.get("base_url"),
        timeout=_TIMEOUT_SECONDS,
        max_retries=_MAX_RETRIES,
    )
    response = await client.messages.create(
        model=llm["model"],
        max_tokens=llm.get("max_tokens", 4096),
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return text, _anthropic_usage(response)


def _openai_usage(response) -> dict:
    """OpenAI's input_tokens already include cached ones."""
    usage = getattr(response, "usage", None)
    details = getattr(usage, "input_tokens_details", None)
    return {
        "model": getattr(response, "model", None),
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read": getattr(details, "cached_tokens", 0) or 0,
        "cache_write": 0,
    }


def _anthropic_usage(response) -> dict:
    """Anthropic's input_tokens exclude the cache, so the raw input total is
    the sum of all three — the same convention mcp-service records."""
    usage = getattr(response, "usage", None)
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return {
        "model": getattr(response, "model", None),
        "input_tokens": (getattr(usage, "input_tokens", 0) or 0) + cache_read + cache_write,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read": cache_read,
        "cache_write": cache_write,
    }
