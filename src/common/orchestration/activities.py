"""Temporal activities. This is the only place non-deterministic work
(filesystem, subprocesses, HTTP, LLM calls) happens — `workflow.py` stays
pure so it can safely run in Temporal's workflow sandbox.
"""

import structlog
from temporalio import activity

from common.capabilities_base import StepContext
from common.capabilities_registry import get_capability, run_capability
from common.planner import create_plan
from common.sandbox import cleanup_workspace as _cleanup_workspace
from common.sandbox import create_workspace

logger = structlog.get_logger()


@activity.defn(name="prepare_workspace")
async def prepare_workspace(request_id: str) -> str:
    return create_workspace(request_id)


@activity.defn(name="cleanup_workspace")
async def cleanup_workspace(request_id: str) -> None:
    _cleanup_workspace(request_id)


@activity.defn(name="plan_task")
async def plan_task(payload: dict) -> dict:
    """Produces the step plan and enriches each step with its capability's
    retry/on_failure policy from the capability registry, so `workflow.py`
    never has to load the registry itself (keeps it sandbox-safe)."""
    plan = await create_plan(payload["prompt"], payload["config"], payload.get("history", []))
    enriched_steps = []
    for step in plan.steps:
        spec = get_capability(step.capability)
        on_failure = step.on_failure or spec.on_failure
        enriched_steps.append(
            {
                "capability": step.capability,
                "params": step.params,
                "retry": {"max_attempts": spec.retry.max_attempts, "backoff_seconds": spec.retry.backoff_seconds},
                "on_failure": on_failure.model_dump() if on_failure else None,
            }
        )
    return {"steps": enriched_steps, "rationale": plan.rationale}


@activity.defn(name="execute_step")
async def execute_step(payload: dict) -> dict:
    ctx = StepContext(
        request_id=payload["request_id"],
        workspace=payload["workspace"],
        prompt=payload["prompt"],
        config=payload["config"],
        capability=payload["capability"],
        params=payload["params"],
        results=payload["results"],
        history=payload.get("history", []),
    )
    logger.info("execute_step", request_id=ctx.request_id, capability=payload["capability"])
    return await run_capability(payload["capability"], ctx)
