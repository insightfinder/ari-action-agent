"""The declarative-registry pattern already used for capabilities/tools
(a name in YAML, resolved to a dotted Python path), applied to the small
set of things that vary per agent kind but aren't a capability or a tool
themselves: the Agent Card's description/skill, the planner's domain
guidance and JSON-parse-failure fallback plan, and which capability
results should surface as named A2A artifacts.

Every agent-kind-specific module in common/ (agent_card.py, planner.py,
orchestration/workflow.py, a2a_server/executor.py) reads this instead of
hardcoding another agent's specifics — that's what keeps them genuinely
shared code, not oncall's code with a jenkins branch bolted on.
"""

import importlib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional

from a2a.types import AgentSkill

from .models.plan import Plan, Step
from .settings import get_settings

# The only place this module knows a specific agent kind's name maps to a
# specific Python package — a one-line addition per new agent kind, same
# cost as registering a new capability or tool.
_AGENT_PACKAGES = {
    "oncall": "ari_oncall_agent",
    "jenkins": "jenkins_agent",
}


@dataclass
class AgentMeta:
    description: str
    skill: AgentSkill
    planner_description: str  # e.g. "on-call agent that fixes bugs and ships code changes"
    planner_domain_guidance: str
    planner_fallback_steps: list[dict]
    # One clause slotted into the shared respond capability's system
    # prompt (common/respond.py) — what "synthesize what was found and
    # done" actually means for this agent kind.
    respond_domain_note: str
    # artifact_name -> "capability.result_key" — which nested results
    # (if present and truthy) get surfaced as named A2A artifacts by
    # a2a_server/executor.py, and mirrored at the top level of the
    # workflow's own return value by orchestration/workflow.py. A dotted
    # string, not a tuple, so an agent package can declare it as plain
    # data with no imports needed.
    artifacts: dict[str, str] = field(default_factory=dict)
    # Which capability a resolved `confirmed_action` (a2a_server/
    # executor.py's structured, non-LLM resume of a paused
    # TASK_STATE_INPUT_REQUIRED task) should run — orchestration/
    # activities.py's plan_task reads this to build a one-step plan
    # directly, bypassing the LLM planner entirely for that turn. None
    # for an agent kind that never pauses for human-in-the-loop
    # confirmation (e.g. oncall).
    confirm_capability: str | None = None
    # Optional per-agent-kind policy applied to the LLM planner's raw step
    # list right after it comes back (orchestration/activities.py's
    # plan_task, an activity — safe to call this from here; never
    # imported by workflow.py itself, see the sandbox-import warning
    # above `_AGENT_PACKAGES`). Exists because prompt guidance alone isn't
    # reliable enough for "always do X by default" policies — confirmed
    # live (docs/MEMORY.md): a smaller/cheaper planner model inconsistently
    # included oncall's own "publish_pr" step for the same kind of request
    # across separate runs, purely from wording in its own system prompt.
    # None for an agent kind with no such default-step policy (e.g.
    # jenkins).
    default_step_policy: Optional[Callable[[Plan, dict], list[Step]]] = None
    direct_capabilities: tuple[str, ...] = ()
    planner_skills: bool = True


def _agent_package(agent_kind: str) -> str:
    return _AGENT_PACKAGES.get(agent_kind, _AGENT_PACKAGES["oncall"])


@lru_cache
def get_agent_meta(agent_kind: str | None = None) -> AgentMeta:
    """`agent_kind` defaults to the global `get_settings().agent_kind` —
    but callers that already have a specific `Settings` instance in hand
    (e.g. `build_agent_card(settings)`) must pass `settings.agent_kind`
    explicitly, not rely on this default. The global settings singleton
    and a locally-constructed `Settings(...)` are different objects; only
    the former is affected by env vars, so silently reading it here would
    make a caller's own explicit `settings` argument do nothing."""
    if agent_kind is None:
        agent_kind = get_settings().agent_kind
    package = _agent_package(agent_kind)
    module = importlib.import_module(f"{package}.meta")
    return module.META
