"""Dynamic planner: turns a free-form prompt + request config into an
ordered `Plan` of capability steps. This is what lets prompts vary
arbitrarily ("...and skip tests", "just open the PR", a bare question with
no repo work at all) without any hardcoded pipeline — the planner sees the
full capability catalog and decides what applies.

`options` in the request config (e.g. `{"skip_tests": true}`) are passed
through verbatim as directives; individual capabilities (see
capabilities/verify.py) also honor them directly as a belt-and-suspenders
guard in case the planner includes a step it shouldn't have.
"""

import json

from common.agent_meta import get_agent_meta
from common.capabilities_registry import catalog
from common.llm.client import complete
from common.models.plan import Plan

_SHARED_TEMPLATE = """You are the planning stage of an autonomous {description}. \
Given a user's prompt (and, for a multi-turn chat, prior conversation history) \
and the capabilities available below, produce a JSON plan: an ordered list of \
steps, each naming one capability and any params it needs. Only use capability \
names from the catalog. Respect explicit user directives. Use the conversation \
history to resolve references like "yes, go ahead and do it" to what was \
discussed earlier. If the prompt needs no action at all (a plain question, or a \
request already fully answered by the conversation so far), return an empty \
step list — a separate step always turns your plan (or the lack of one) into \
the actual response, so an empty list is a normal, complete answer, not a \
failure.

{domain_guidance}

Capability catalog:
{{catalog}}

Respond with ONLY JSON matching this schema:
{{{{"steps": [{{{{"capability": "<name>", "params": {{{{}}}}}}}}], "rationale": "<short reason>"}}}}
"""

async def create_plan(prompt: str, config: dict, history: list[dict] | None = None) -> Plan:
    meta = get_agent_meta()
    system_prompt = config.get("system_prompt") or _SHARED_TEMPLATE.format(
        description=meta.planner_description,
        domain_guidance=meta.planner_domain_guidance,
    ).format(catalog=json.dumps(catalog(), indent=2))
    user_prompt = json.dumps(
        {"prompt": prompt, "history": history or [], "options": config.get("options", {})}
    )

    raw = await complete(config["llm"], system_prompt, user_prompt)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fall back to this agent kind's own default pipeline rather than
        # failing the request outright — a malformed planner response
        # shouldn't be fatal. Real capabilities from *this agent's own*
        # catalog (see agent_meta.py) — a different agent kind's fallback
        # would reference capabilities that don't exist in this one's
        # registry at all and fail immediately.
        data = {
            "steps": meta.planner_fallback_steps,
            "rationale": "planner output was not valid JSON; used default pipeline",
        }
    return Plan.model_validate(data)
