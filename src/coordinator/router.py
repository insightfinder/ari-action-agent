import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import structlog

from common.llm import client as llm_client
from common.llm_json import extract

from .registry import Registry

logger = structlog.get_logger()

Complete = Callable[[dict, str, str], Awaitable[str]]

SYSTEM_PROMPT = """You route a user's request to exactly one specialist agent.

You get the request, the last few turns of this conversation, the agent the conversation used
last (current_agent), and each available agent with its description and skills.

Rules:
- Pick the agent whose description and skills fit the request.
- Keep current_agent unless the request clearly belongs to another agent. Short follow-ups such as
  "yes", "and the other one?", "status" or "try again" stay with current_agent.
- If no agent fits well, pick the closest one; never invent an agent name.

Answer with only this JSON object and nothing else:
{"agent": "<one of the agent names>", "reason": "<one short sentence>"}
"""


class NoAgentAvailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Route:
    agent: str
    reason: str
    decided_by: str


def _fallback(registry: Registry, names: list[str], current: str) -> str:
    if current in names:
        return current
    if registry.default_agent in names:
        return registry.default_agent
    return names[0]


async def choose(
    registry: Registry,
    prompt: str,
    *,
    llm: Any,
    turns: list[dict[str, Any]] | None = None,
    current: str = "",
    complete: Complete | None = None,
) -> Route:
    available = registry.available()
    if not available:
        details = "; ".join(f"{name}: {state}" for name, state in registry.status().items())
        raise NoAgentAvailable(f"no agent is reachable right now ({details})")
    names = [agent.name for agent in available]
    if len(names) == 1:
        return Route(names[0], "the only available agent", "only")
    fallback = _fallback(registry, names, current)
    if not isinstance(llm, dict) or not llm.get("provider") or not llm.get("api_key"):
        return Route(fallback, "no model was given to route with", "fallback")
    request = {
        "request": prompt,
        "recent_turns": turns or [],
        "current_agent": current or None,
        "agents": [registry.describe(agent) for agent in available],
    }
    try:
        raw = await (complete or llm_client.complete)(llm, SYSTEM_PROMPT, json.dumps(request))
        answer = extract(raw)
    except Exception as exc:
        logger.warning("coordinator_route_failed", error=f"{type(exc).__name__}: {exc}"[:300], fallback=fallback)
        return Route(fallback, "the routing model gave no usable answer", "fallback")
    agent = answer.get("agent") if isinstance(answer, dict) else None
    if agent not in names:
        logger.warning("coordinator_route_unknown_agent", answer=str(agent)[:100], fallback=fallback)
        return Route(fallback, "the routing model named an agent that is not available", "fallback")
    return Route(agent, str(answer.get("reason") or "")[:300], "llm")
