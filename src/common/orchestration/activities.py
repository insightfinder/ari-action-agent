"""Temporal activities. This is the only place non-deterministic work
(filesystem, subprocesses, HTTP, LLM calls) happens — `workflow.py` stays
pure so it can safely run in Temporal's workflow sandbox.
"""

import asyncio
import os
from dataclasses import replace

import structlog
from temporalio import activity

from common.agent_meta import get_agent_meta
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


@activity.defn(name="resolve_artifacts")
async def resolve_artifacts(results: dict) -> dict:
    """This agent kind's own declared artifacts (agent_meta.artifacts,
    e.g. oncall's "root_cause": "analyze.root_cause"), resolved against
    this run's actual results — only the ones present and truthy.

    Lives here, not in workflow.py, because get_agent_meta() transitively
    imports a2a.types (for AgentSkill) — pulling that into the workflow's
    own module chain breaks Temporal's default SandboxedWorkflowRunner:
    confirmed live against a real Temporal dev server that constructing
    a Worker for AriAgentWorkflow raises "Failed validating workflow
    AriAgentWorkflow" (a sniffio/anyio restricted-import failure several
    layers down) the moment workflow.py imports anything from
    common.agent_meta at module scope. Activities run outside the
    sandbox — same reasoning as plan_task being the one that calls
    get_capability(), not workflow.py itself."""
    found = {}
    for name, dotted in get_agent_meta().artifacts.items():
        capability, key = dotted.split(".", 1)
        value = results.get(capability, {}).get(key)
        if value:
            found[name] = value
    return found


@activity.defn(name="plan_task")
async def plan_task(payload: dict) -> dict:
    """Produces the step plan and enriches each step with its capability's
    retry/on_failure policy from the capability registry, so `workflow.py`
    never has to load the registry itself (keeps it sandbox-safe).

    A `confirmed_action` in the payload (set by a2a_server/executor.py
    once it's resolved a structured confirm_pending_action against a
    paused task's own artifacts — see docs/design.md §5b) bypasses the
    LLM planner entirely: no `create_plan()` call, no model in the loop
    for this decision at all. The resulting step's `confirmed: True` is
    what execute_action.py's trigger_build gate actually checks — the
    normal branch below always sets `confirmed: False`, and the LLM's own
    JSON output has no way to set this field itself (it only ever
    populates a step's `params`, never a sibling top-level key)."""
    confirmed_action = payload.get("confirmed_action")
    if confirmed_action:
        capability_name = get_agent_meta().confirm_capability
        if not capability_name:
            raise RuntimeError("plan_task: confirmed_action given but this agent kind has no confirm_capability")
        spec = get_capability(capability_name)
        return {
            "steps": [
                {
                    "capability": capability_name,
                    "params": confirmed_action,
                    "retry": {"max_attempts": spec.retry.max_attempts, "backoff_seconds": spec.retry.backoff_seconds},
                    "on_failure": spec.on_failure.model_dump() if spec.on_failure else None,
                    "timeout_seconds": spec.timeout_seconds,
                    "confirmed": True,
                }
            ],
            "rationale": "resuming a confirmed pending action",
        }

    plan = await create_plan(payload["prompt"], payload["config"], payload.get("history", []))
    steps = plan.steps
    default_step_policy = get_agent_meta().default_step_policy
    if default_step_policy:
        steps = default_step_policy(plan, payload["config"].get("options", {}))

    enriched_steps = []
    for step in steps:
        spec = get_capability(step.capability)
        on_failure = step.on_failure or spec.on_failure
        enriched_steps.append(
            {
                "capability": step.capability,
                "params": step.params,
                "retry": {"max_attempts": spec.retry.max_attempts, "backoff_seconds": spec.retry.backoff_seconds},
                "on_failure": on_failure.model_dump() if on_failure else None,
                "timeout_seconds": spec.timeout_seconds,
                "confirmed": False,
            }
        )
    return {"steps": enriched_steps, "rationale": plan.rationale}


# How often this activity tells Temporal it is still alive. Must stay well
# under the `heartbeat_timeout` workflow.py sets, since one missed beat is
# all it takes to be declared dead.
_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("ACTIVITY_HEARTBEAT_INTERVAL_SECONDS", "20"))


async def _beat(interval: float) -> None:
    """Heartbeat until cancelled.

    Two things this buys, and the second is the one that is easy to miss:

    1. A worker that dies mid-step is noticed in `heartbeat_timeout`
       instead of in `start_to_close_timeout`. Measured on stg 2026-10-09:
       the worker was OOMKilled at 17:29:05 and Temporal did not retry
       `verify` until 17:33:20 — 4m15s of a 50-minute budget doing
       nothing — and in workflow c0296796 the same gap was the full 20
       minutes, because `generate_change`'s `start_to_close` is 1200s.
    2. Temporal delivers cancellation *in the heartbeat response*. Without
       a heartbeat there is no channel to deliver it on, so a timed-out
       attempt's opencode subprocess keeps running in the pod after the
       workflow has moved on. This was recorded as a known gap twice in
       docs/MEMORY.md before it was finally paid for.

    `activity.heartbeat()` is non-blocking; the cost is one RPC per
    interval, batched by the SDK.
    """
    while True:
        await asyncio.sleep(interval)
        activity.heartbeat()


# Steps that never read the checkout, so a missing one is not a problem to fix.
#
# `respond` is the reason this exists. workflow.py runs it *after*
# `cleanup_workspace`, deliberately and on every ending, so by then the sandbox
# is gone because something deleted it on purpose — and without this the heal
# below treated that as a lost pod and cloned the whole repository again to
# write a summary that only reads `results`. Observed on stg 2026-10-10
# (dbc75291): `recloning_lost_sandbox capability=respond`, two seconds after
# cleanup, on an otherwise healthy run.
_NO_REPO_NEEDED = frozenset({"respond"})


async def _reclone_if_the_sandbox_is_gone(ctx: StepContext) -> None:
    """Re-run `clone_repo` when its checkout has vanished from disk.

    The sandbox is an `emptyDir` (the chart persists only the repo and
    build caches), so it belongs to one pod — while a Temporal activity
    belongs to none. Any pod replacement between the clone and a step that
    reads it loses the working tree.

    Confirmed live, not theoretical (stg, workflow
    c0296796-9df4-43b0-8691-d191e1343278, 2026-10-09). The activity
    identities in that history show it exactly: `prepare_workspace`,
    `plan_task` and `clone_repo` all ran on `ari-oncall-worker-766fc5765-tr59k`,
    a deployment roll then replaced the pod (`strategy: Recreate`), and
    `generate_change` started 20 minutes later as attempt 2 on
    `ari-oncall-worker-7c4c9499dd-qq677`, where it failed instantly with
    `FileNotFoundError: .../sandbox/c0296796-.../repo`. The incident was
    reported to the caller as "the cloned repository directory became
    inaccessible", which is true and useless.

    Healing here rather than persisting the sandbox on a volume: making
    the step not depend on pod affinity is the property we actually want,
    and a PVC would only move the assumption somewhere harder to see. The
    re-clone is cheap because the shared mirror *is* PVC-backed.

    Note this is deliberately not `require_cloned_repo`'s job — that one
    answers "did the planner include clone_repo at all", a planning
    question, and it runs after the capability has already started.
    """
    if ctx.capability in _NO_REPO_NEEDED:
        return
    clone_result = ctx.results.get("clone_repo")
    if not clone_result:
        return
    cloned_path = clone_result.get("cloned_path")
    if not cloned_path or os.path.isdir(cloned_path):
        return

    logger.warning(
        "recloning_lost_sandbox",
        request_id=ctx.request_id,
        capability=ctx.capability,
        cloned_path=cloned_path,
    )
    clone_ctx = replace(ctx, capability="clone_repo", params={}, tools={})
    ctx.results["clone_repo"] = await run_capability("clone_repo", clone_ctx)


@activity.defn(name="execute_step")
async def execute_step(payload: dict) -> dict:
    info = activity.info()
    ctx = StepContext(
        request_id=payload["request_id"],
        workspace=payload["workspace"],
        prompt=payload["prompt"],
        config=payload["config"],
        capability=payload["capability"],
        params=payload["params"],
        results=payload["results"],
        history=payload.get("history", []),
        confirmed=payload.get("confirmed", False),
        step_deadline_seconds=(
            info.start_to_close_timeout.total_seconds() if info.start_to_close_timeout else None
        ),
    )
    logger.info("execute_step", request_id=ctx.request_id, capability=payload["capability"])

    heartbeat = asyncio.create_task(_beat(_HEARTBEAT_INTERVAL_SECONDS))
    try:
        await _reclone_if_the_sandbox_is_gone(ctx)
        return await run_capability(payload["capability"], ctx)
    finally:
        heartbeat.cancel()
