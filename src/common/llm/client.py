"""Per-request LLM client factory, used only by the planner
(`planning/planner.py`) to turn a prompt into a step plan. There is
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
ARI's own planning step.
"""

import os

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

# 120s x 3 attempts = 6 minutes worst case, comfortably inside the
# activity budget with room for the rest of the step. The planner turns
# one prompt into a step plan — a few hundred tokens out — so a call
# still running after two minutes is stuck, not slow.
_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))


async def complete(llm: dict, system_prompt: str, user_prompt: str) -> str:
    provider = llm["provider"].lower()
    if provider == "openai":
        return await _openai_complete(llm, system_prompt, user_prompt)
    if provider == "anthropic":
        return await _anthropic_complete(llm, system_prompt, user_prompt)
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
