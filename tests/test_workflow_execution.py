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

import asyncio
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


async def _run_workflow(plan: dict, execute_step, config: dict | None = None) -> dict:
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
                {"prompt": "fix it", "history": [], "config": config or {}, "confirmed_action": None},
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



# --- pipeline wall-clock budget -------------------------------------------
#
# Regression coverage for stg workflow 9b32a817 (2026-10-08): a single
# request ran 45m56s without any one step ever exceeding its own
# configured timeout. Every bound in capabilities.yaml is per-*attempt*;
# `retry.max_attempts` multiplied them, the `on_failure` loop multiplied
# that again, and nothing capped the product.
#
# These drive the clock with retry backoff rather than a sleeping
# activity. The time-skipping environment only skips while nothing is
# running, so an activity that blocks for the budget costs that budget in
# real seconds; backoff between attempts is a server-side timer and is
# skipped instantly. It also exercises the stronger claim directly — that
# the budget binds *across* a step's retries, not just one attempt.

_BUDGET_SECONDS = 600
_BUDGET = {"options": {"pipeline_budget_seconds": _BUDGET_SECONDS}}

# 50 attempts backing off from 10s at coefficient 2.0 runs for days if
# nothing stops it. The budget is what has to stop it.
_RUNAWAY_RETRY = {"max_attempts": 50, "backoff_seconds": 10}


def _step(capability: str, **overrides) -> dict:
    step = {
        "capability": capability,
        "params": {},
        "retry": {"max_attempts": 1, "backoff_seconds": 0},
        "on_failure": None,
        "timeout_seconds": None,
        "confirmed": False,
    }
    step.update(overrides)
    return step


@pytest.mark.asyncio
async def test_the_budget_caps_a_step_across_all_of_its_retries():
    plan = {"steps": [_step("generate_change", retry=_RUNAWAY_RETRY)], "rationale": "test"}
    calls = {"n": 0}

    @activity.defn(name="execute_step")
    async def execute_step(payload: dict) -> dict:
        if payload["capability"] == "respond":
            return {"message": f"final:{payload['params'].get('pipeline_status')}"}
        calls["n"] += 1
        raise RuntimeError("simulated: attempt failed")

    result = await _run_workflow(plan, execute_step, config=_BUDGET)

    assert result["status"] == "failed"
    assert "final:failed" in result["message"]
    # Cut off by the clock, not by exhausting max_attempts -- which is the
    # whole point, since max_attempts was never the binding constraint.
    assert 0 < calls["n"] < _RUNAWAY_RETRY["max_attempts"]


@pytest.mark.asyncio
async def test_the_on_failure_loop_stops_once_the_budget_is_spent():
    # The loop's own `max_attempts: 3` bounds how many times it repeats,
    # not how long those repeats take -- exactly the gap that let
    # 9b32a817 run 45m56s. Budget and backoff are sized here so the clock
    # runs out before that loop cap would have, which is the case the cap
    # alone cannot cover.
    plan = {
        "steps": [
            _step("generate_change"),
            _step(
                "verify",
                retry={"max_attempts": 50, "backoff_seconds": 1},
                on_failure={"goto": "generate_change", "max_attempts": 3, "then": "fail_needs_human"},
            ),
        ],
        "rationale": "test",
    }
    calls = {"generate_change": 0, "verify": 0}
    reason = {}

    @activity.defn(name="execute_step")
    async def execute_step(payload: dict) -> dict:
        capability = payload["capability"]
        if capability == "respond":
            reason["text"] = payload["params"].get("failure_reason") or ""
            return {"message": f"final:{payload['params'].get('pipeline_status')}"}
        calls[capability] += 1
        if capability == "verify":
            raise RuntimeError("simulated: verify failed")
        return {"status": "ok"}

    # Backoff has to stay small relative to the budget: Temporal will not
    # sleep past an activity's schedule_to_close, so a backoff longer than
    # the budget's remainder ends that attempt without the clock moving,
    # and the budget would never actually be spent.
    result = await _run_workflow(plan, execute_step, config={"options": {"pipeline_budget_seconds": 200}})

    assert result["status"] == "failed"
    # The run must end because the clock ran out, not because the loop hit
    # its own attempt cap -- and must say which, since the two call for
    # completely different follow-up.
    assert "budget" in reason["text"], reason
    assert "generate_change" in reason["text"], "must name the step that never got to run"
    # Without the budget this loop runs generate_change 4 times (see the
    # on_failure test above).
    assert calls["generate_change"] < 4


@pytest.mark.asyncio
async def test_a_run_that_finishes_inside_the_budget_is_untouched_by_it():
    # The cap must be invisible to every healthy request -- a guard that
    # fires on normal work is worse than no guard.
    plan = {"steps": [_step("generate_change"), _step("propose_pr")], "rationale": "test"}
    seen = []

    @activity.defn(name="execute_step")
    async def execute_step(payload: dict) -> dict:
        if payload["capability"] == "respond":
            return {"message": f"final:{payload['params'].get('pipeline_status')}"}
        seen.append(payload["capability"])
        return {"status": "ok"}

    result = await _run_workflow(plan, execute_step, config=_BUDGET)

    assert seen == ["generate_change", "propose_pr"]
    assert result["status"] == "completed"
    assert "final:completed" in result["message"]


@pytest.mark.asyncio
async def test_the_budget_still_lets_respond_run_so_the_caller_learns_why():
    # Cutting the pipeline off must not cut off the explanation: a caller
    # that waited out the budget deserves a real message, not the SDK's
    # generic error. `respond` runs on every exit path.
    plan = {"steps": [_step("generate_change", retry=_RUNAWAY_RETRY)], "rationale": "test"}
    responded = {"ran": False}

    @activity.defn(name="execute_step")
    async def execute_step(payload: dict) -> dict:
        if payload["capability"] == "respond":
            responded["ran"] = True
            return {"message": "explained"}
        raise RuntimeError("simulated: attempt failed")

    result = await _run_workflow(plan, execute_step, config=_BUDGET)

    assert responded["ran"] is True
    assert result["message"] == "explained"


@pytest.mark.asyncio
async def test_without_an_override_the_default_budget_leaves_normal_runs_alone():
    # The override is a convenience; the shipped default must not be so
    # tight that an ordinary pipeline trips it.
    plan = {"steps": [_step("clone_repo"), _step("generate_change"), _step("propose_pr")], "rationale": "test"}

    @activity.defn(name="execute_step")
    async def execute_step(payload: dict) -> dict:
        if payload["capability"] == "respond":
            return {"message": f"final:{payload['params'].get('pipeline_status')}"}
        return {"status": "ok"}

    result = await _run_workflow(plan, execute_step)

    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_exhausted_on_failure_with_then_continue_still_reaches_the_later_steps():
    # The whole point of the change. With `then: fail_needs_human` the loop
    # broke out here and `propose_pr` never ran, so a change that had already
    # been generated was pushed nowhere and shown to nobody -- observed on stg
    # 2026-10-10 (fc532091, 1c2396a4), where verify spent all four attempts on
    # a cold Maven download and the finished diff was discarded.
    calls = {"generate_change": 0, "verify": 0, "propose_pr": 0}
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
                "on_failure": {"goto": "generate_change", "max_attempts": 3, "then": "continue"},
                "timeout_seconds": 5,
                "confirmed": False,
            },
            {
                "capability": "propose_pr",
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
        calls[capability] = calls.get(capability, 0) + 1
        if capability == "verify":
            return {"status": "failed", "failure": "mvn: BUILD FAILURE"}
        if capability == "propose_pr":
            return {"pending_action": {"id": "p1"}, "requires_approval": True}
        if capability == "respond":
            return {"message": f"final:{payload['params'].get('pipeline_status')}"}
        return {"status": "ok"}

    result = await _run_workflow(plan, execute_step)

    assert calls["verify"] == 4, "the retry loop itself must be unchanged"
    assert calls["propose_pr"] == 1, "the step after the exhausted loop must still run"
    assert result["status"] == "needs_approval"


@pytest.mark.asyncio
async def test_then_fail_needs_human_still_stops_the_pipeline():
    # `continue` is opt-in per capability; a step whose failure really should
    # end the run keeps doing exactly that.
    calls = {"verify": 0, "propose_pr": 0}
    plan = {
        "steps": [
            {
                "capability": "verify",
                "params": {},
                "retry": {"max_attempts": 1, "backoff_seconds": 0},
                "on_failure": {"goto": "verify", "max_attempts": 1, "then": "fail_needs_human"},
                "timeout_seconds": 5,
                "confirmed": False,
            },
            {
                "capability": "propose_pr",
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
        calls[capability] = calls.get(capability, 0) + 1
        if capability == "verify":
            return {"status": "failed", "failure": "boom"}
        if capability == "respond":
            return {"message": f"final:{payload['params'].get('pipeline_status')}"}
        return {"status": "ok"}

    result = await _run_workflow(plan, execute_step)

    assert calls["propose_pr"] == 0
    assert result["status"] == "failed"
