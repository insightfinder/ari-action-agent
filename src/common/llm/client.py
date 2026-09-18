"""Per-request LLM client factory, used only by the planner
(`planning/planner.py`) to turn a prompt into a step plan. There is
deliberately no global/shared LLM key anywhere in this service — every call
is made with the provider, model, and API key carried in *that request's*
own `config.llm`, so a fresh SDK client is constructed per call rather than
cached.

Uses the official SDKs (openai>=3.14, anthropic>=1.6) rather than raw HTTP,
so we inherit their retry/timeout handling and stay compatible as the APIs
evolve. This is unrelated to opencode's own LLM calls (opencode manages
those itself via the `provider.apiKey` written into its per-request
opencode.json — see tools/opencode_cli.py); this client exists solely for
ARI's own planning step.
"""

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI


async def complete(llm: dict, system_prompt: str, user_prompt: str) -> str:
    provider = llm["provider"].lower()
    if provider == "openai":
        return await _openai_complete(llm, system_prompt, user_prompt)
    if provider == "anthropic":
        return await _anthropic_complete(llm, system_prompt, user_prompt)
    raise ValueError(f"Unsupported llm.provider: {provider!r}")


async def _openai_complete(llm: dict, system_prompt: str, user_prompt: str) -> str:
    client = AsyncOpenAI(api_key=llm["api_key"], base_url=llm.get("base_url"))
    response = await client.responses.create(
        model=llm["model"],
        instructions=system_prompt,
        input=user_prompt,
    )
    return response.output_text


async def _anthropic_complete(llm: dict, system_prompt: str, user_prompt: str) -> str:
    client = AsyncAnthropic(api_key=llm["api_key"], base_url=llm.get("base_url"))
    response = await client.messages.create(
        model=llm["model"],
        max_tokens=llm.get("max_tokens", 4096),
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")
