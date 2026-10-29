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
import re

import anthropic
import openai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

# 120s x 3 attempts = 6 minutes worst case, comfortably inside the
# activity budget with room for the rest of the step. The planner turns
# one prompt into a step plan — a few hundred tokens out — so a call
# still running after two minutes is stuck, not slow.
_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))


# A provider's error text can quote the key it was sent: OpenAI's 401 says "Incorrect API key
# provided: sk-proj-****abcd", first and last characters intact. Masked or not, it has no business in
# a failure reason, which reaches the `respond` step's prompt, the caller's reply and Temporal's history.
# "api key provided: X" / "api_key=X", where X looks like a key (has a digit or a mask), not a word.
_KEY_ECHO = re.compile(r"(?i)(api[ _-]?key\b[^:=\n]{0,30}[:=]\s*['\"]?)(?=[^\s'\",]*[0-9*])[^\s'\",]{6,}")
_KEY_LIKE = re.compile(
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_*.\-]{4,}"  # OpenAI, Anthropic and look-alikes
    r"|\bAIza[A-Za-z0-9_*\-]{8,}"  # Google
    r"|\bgsk_[A-Za-z0-9_*]{8,}"  # Groq
    r"|(?i:\bbearer\s+)[A-Za-z0-9._~+/=*\-]{8,}"
    r"|\b[0-9a-fA-F]{32,}\b"  # Azure OpenAI and other bare hex keys
)


class LLMProviderError(RuntimeError):
    """A provider's error response, worded so that it carries no part of the API key."""


def _provider_error(provider: str, exc: openai.APIStatusError | anthropic.APIStatusError) -> LLMProviderError:
    status = exc.status_code
    if status == 401:
        # The body of a rejected-key response is about the key, and nothing else in it helps.
        return LLMProviderError(f"{provider} rejected the API key (HTTP 401)")
    detail = _KEY_LIKE.sub("[REDACTED]", _KEY_ECHO.sub(r"\1[REDACTED]", str(exc.message)))
    return LLMProviderError(f"{provider} returned HTTP {status}: {detail[:1000]}")


async def complete(llm: dict, system_prompt: str, user_prompt: str) -> str:
    provider = llm["provider"].lower()
    try:
        if provider == "openai":
            return await _openai_complete(llm, system_prompt, user_prompt)
        if provider == "anthropic":
            return await _anthropic_complete(llm, system_prompt, user_prompt)
    except (openai.APIStatusError, anthropic.APIStatusError) as exc:
        # `from None`: the SDK exception, message and all, must not ride along as the cause.
        raise _provider_error(provider, exc) from None
    raise ValueError(f"Unsupported llm.provider: {provider!r}")


async def _openai_complete(llm: dict, system_prompt: str, user_prompt: str) -> str:
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
    return response.output_text


async def _anthropic_complete(llm: dict, system_prompt: str, user_prompt: str) -> str:
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
    return "".join(block.text for block in response.content if block.type == "text")
