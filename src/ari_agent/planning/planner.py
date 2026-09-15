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

from ..capabilities.registry import catalog
from ..llm.client import complete
from ..models.plan import Plan

_SYSTEM_PROMPT_TEMPLATE = """You are the planning stage of an autonomous on-call \
code-fix agent. Given a user's prompt (and, for a multi-turn chat, prior \
conversation history) and the capabilities available below, produce a JSON \
plan: an ordered list of steps, each naming one capability and any params it \
needs. Only use capability names from the catalog. Respect explicit user \
directives (e.g. "skip tests" means omit the "verify" step; "just open a \
PR" means skip straight to publish_pr assuming a fix already exists in the \
workspace). Use the conversation history to resolve references like "yes, \
go ahead and fix it" to what was discussed earlier. If the prompt needs no \
repo/code action at all (a plain question, or a request already fully \
answered by the conversation so far), return an empty step list — a \
separate step always turns your plan (or the lack of one) into the actual \
response, so an empty list is a normal, complete answer, not a failure.

Branches are dynamic per request, not fixed — read the prompt carefully for \
them: if it names a specific branch to work from ("check the repo in dev \
branch", "look at the release-2.3 branch"), set that as the clone_repo \
step's `ref` param. If it names a specific branch to open the PR against \
("raise a PR against staging", "target main"), set that as the publish_pr \
step's `base_ref` param — this can be a *different* branch than `ref` \
(work from dev, PR against staging is a normal, valid request). Leave \
either param unset when the prompt doesn't name that branch explicitly; \
the capability already has a sensible default.

Capability catalog:
{catalog}

Respond with ONLY JSON matching this schema:
{{"steps": [{{"capability": "<name>", "params": {{}}}}], "rationale": "<short reason>"}}
"""


async def create_plan(prompt: str, config: dict, history: list[dict] | None = None) -> Plan:
    system_prompt = config.get("system_prompt") or _SYSTEM_PROMPT_TEMPLATE.format(
        catalog=json.dumps(catalog(), indent=2)
    )
    user_prompt = json.dumps(
        {"prompt": prompt, "history": history or [], "options": config.get("options", {})}
    )

    raw = await complete(config["llm"], system_prompt, user_prompt)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fall back to the full standard pipeline rather than failing the
        # request outright — a malformed planner response shouldn't be fatal.
        data = {
            "steps": [
                {"capability": "clone_repo"},
                {"capability": "analyze"},
                {"capability": "generate_fix"},
                {"capability": "verify"},
                {"capability": "publish_pr"},
            ],
            "rationale": "planner output was not valid JSON; used default pipeline",
        }
    return Plan.model_validate(data)
