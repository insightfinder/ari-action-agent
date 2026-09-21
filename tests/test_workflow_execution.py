"""Full workflow-execution tests for AriAgentWorkflow, using Temporal's own
time-skipping test environment rather than mocking around it — the
capability-level unit tests elsewhere in this suite can't exercise
workflow.py's own control flow (the on_failure retry loop, how a raised
ActivityError is handled), only what a single capability's run() does.

Regression coverage for the live incident this addresses: `verify` getting
killed by an external cancel (a caller's own patience timeout, routinely
~8-10 minutes, while verify sat with no dependency cache and no timeout of
its own) used to abort the *entire* pipeline outright, skipping the exact
same on_failure retry loop a normally-reported verify failure already gets.
"""

import sys
import uuid
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.orchestration.workflow import AriAgentWorkflow  # noqa: E402

_TASK_QUEUE = "test-workflow-queue"


@activity.defn(name="prepare_workspace")
async def _prepare_workspace(request_id: str) -> str:
    return "/tmp/ws"


@activity.defn(name="cleanup_workspace")
async def _cleanup_workspace(request_id: str) -> None:
    return None


@activity.defn(name="resolve_artifacts")
async def _resolve_artifacts(results: dict) -> dict:
    return {}


async def _run_workflow(plan: dict, execute_step) -> dict:
    @activity.defn(name="plan_task")
    async def plan_task(payload: dict) -> dict:
        return plan

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[AriAgentWorkflow],
            activities=[_prepare_workspace, plan_task, execute_step, _cleanup_workspace, _resolve_artifacts],
        ):
            return await env.client.execute_workflow(
                AriAgentWorkflow.run,
                {"prompt": "fix it", "history": [], "config": {}, "confirmed_action": None},
                id=f"test-{uuid.uuid4()}",
                task_queue=_TASK_QUEUE,
            )


@pytest.mark.asyncio
async def test_raised_activity_error_loops_via_on_failure_instead_of_aborting():
    calls = {"generate_change": 0, "verify": 0}
    plan = {
        "steps": [
            {
                "capability": "generate_change",
                "params": {},
                "retry": {"max_attempts": 1, "backoff_seconds": 0},
                "on_failure": None,
                "timeout_seconds": None,
                "confirmed": False,
            },
            {
                "capability": "verify",
                "params": {},
                "retry": {"max_attempts": 1, "backoff_seconds": 0},
                "on_failure": {"goto": "generate_change", "max_attempts": 3, "then": "fail_needs_human"},
                "timeout_seconds": 5,
                "confirmed": False,
            },
        ],
        "rationale": "test",
    }

    @activity.defn(name="execute_step")
    async def execute_step(payload: dict) -> dict:
        capability = payload["capability"]
        if capability == "generate_change":
            calls["generate_change"] += 1
            return {"status": "ok"}
        if capability == "verify":
            calls["verify"] += 1
            # Simulates the activity being killed outright (timeout or an
            # external cancel), not a normal {"status": "failed"} report.
            raise RuntimeError("simulated: activity cancelled/timed out")
        if capability == "respond":
            return {"message": f"final:{payload['params'].get('pipeline_status')}"}
        raise AssertionError(f"unexpected capability {capability}")

    result = await _run_workflow(plan, execute_step)

    # on_failure.max_attempts=3: verify is tried 4 times total (the 4th
    # attempt exceeds max_attempts and gives up), looping back to
    # generate_change each time rather than aborting on the first failure.
    assert calls["verify"] == 4
    assert calls["generate_change"] == 4
    assert result["status"] == "failed"
    assert "final:failed" in result["message"]


@pytest.mark.asyncio
async def test_raised_activity_error_without_on_failure_still_aborts_immediately():
    # Contrast case: a step with no on_failure policy (e.g. analyze) must
    # keep the original behavior — abort the whole pipeline on the first
    # raised exception, not loop.
    calls = {"analyze": 0}
    plan = {
        "steps": [
            {
                "capability": "analyze",
                "params": {},
                "retry": {"max_attempts": 1, "backoff_seconds": 0},
                "on_failure": None,
                "timeout_seconds": None,
                "confirmed": False,
            },
        ],
        "rationale": "test",
    }

    @activity.defn(name="execute_step")
    async def execute_step(payload: dict) -> dict:
        capability = payload["capability"]
        if capability == "analyze":
            calls["analyze"] += 1
            raise RuntimeError("simulated: activity cancelled/timed out")
        if capability == "respond":
            return {"message": f"final:{payload['params'].get('pipeline_status')}"}
        raise AssertionError(f"unexpected capability {capability}")

    result = await _run_workflow(plan, execute_step)

    assert calls["analyze"] == 1
    assert result["status"] == "failed"
    assert "final:failed" in result["message"]
