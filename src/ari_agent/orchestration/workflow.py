"""The one workflow this agent runs, for every request. It stays
deterministic/pure — no filesystem, HTTP, subprocess, or registry lookups
here, only activity calls (by name, so this module never has to import
their non-deterministic dependencies) and plain data manipulation.

Delivery of the result to the caller (blocking wait, polling, or a
webhook) is entirely the A2A layer's concern (`a2a_server/executor.py`) —
this workflow doesn't know or care how its result reaches the caller, and
in particular never sends a callback itself: the SDK's own
`BasePushNotificationSender` handles that from the status updates the
executor publishes once this workflow returns. That's also why there's no
`mode` field here anymore — every request runs the exact same way.
"""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


def _fallback_message(status: str, results: dict[str, Any], failure_reason: str | None) -> str:
    """Only used if the `respond` step itself fails after retries — a
    templated last resort so a request always gets *some* message back,
    never an exception surfaced to the caller."""
    pr = results.get("publish_pr", {}).get("pr")
    if status == "completed" and pr:
        return f"Opened PR: {pr.get('url')}"
    if status == "failed":
        return f"Gave up after exhausting retries: {failure_reason}"
    return "No repository action was needed for this request."


def _build_response(request_id: str, status: str, results: dict[str, Any], message: str) -> dict:
    return {
        "request_id": request_id,
        "status": status,
        "message": message,
        "root_cause": results.get("analyze", {}).get("root_cause"),
        "pr": results.get("publish_pr", {}).get("pr"),
        "data": results,
    }


@workflow.defn(name="AriOnCallWorkflow")
class AriOnCallWorkflow:
    @workflow.run
    async def run(self, request: dict) -> dict:
        request_id = workflow.info().workflow_id
        config = request["config"]

        workspace = await workflow.execute_activity(
            "prepare_workspace",
            request_id,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        history = request.get("history", [])

        # Explicit, bounded retry — found live while testing the A2A
        # integration with a deliberately bad LLM key: without this,
        # Temporal's default retry policy has no attempt cap, so an
        # invalid API key (or any other non-retryable planner failure)
        # made `message/send`'s blocking wait hang far past any reasonable
        # timeout instead of failing fast.
        plan = await workflow.execute_activity(
            "plan_task",
            {"prompt": request["prompt"], "config": config, "history": history},
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5)),
        )

        steps: list[dict] = plan["steps"]
        results: dict[str, Any] = {}
        loop_attempts: dict[str, int] = {}
        final_status = "completed" if steps else "no_action"
        failure_reason: str | None = None

        i = 0
        while i < len(steps):
            step = steps[i]
            capability = step["capability"]
            retry = step.get("retry") or {}

            step_result = await workflow.execute_activity(
                "execute_step",
                {
                    "request_id": request_id,
                    "workspace": workspace,
                    "prompt": request["prompt"],
                    "config": config,
                    "capability": capability,
                    "params": step.get("params", {}),
                    "results": results,
                },
                start_to_close_timeout=timedelta(minutes=20),
                retry_policy=RetryPolicy(
                    maximum_attempts=retry.get("max_attempts", 1),
                    backoff_coefficient=2.0,
                    initial_interval=timedelta(seconds=retry.get("backoff_seconds") or 1),
                ),
            )
            results[capability] = step_result

            on_failure = step.get("on_failure")
            if on_failure and step_result.get("status") == "failed":
                attempts = loop_attempts.get(capability, 0) + 1
                loop_attempts[capability] = attempts
                if attempts > on_failure["max_attempts"]:
                    final_status = "failed" if on_failure["then"] == "fail_needs_human" else on_failure["then"]
                    failure_reason = step_result.get("failure")
                    break
                i = next(idx for idx, s in enumerate(steps) if s["capability"] == on_failure["goto"])
                continue

            i += 1

        await workflow.execute_activity(
            "cleanup_workspace",
            request_id,
            start_to_close_timeout=timedelta(seconds=30),
        )

        # Guaranteed final step, called directly rather than appended to
        # `steps` — appending it would let the fail_needs_human `break`
        # above skip it (see capabilities/respond.py). Runs regardless of
        # whether the planner chose any capabilities at all, so a plain
        # question always gets a real answer instead of a canned
        # "no action needed", and a full pipeline gets a synthesized
        # summary instead of a templated one-liner.
        try:
            respond_result = await workflow.execute_activity(
                "execute_step",
                {
                    "request_id": request_id,
                    "workspace": workspace,
                    "prompt": request["prompt"],
                    "config": config,
                    "capability": "respond",
                    "params": {"pipeline_status": final_status, "failure_reason": failure_reason},
                    "results": results,
                    "history": history,
                },
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=5)),
            )
            message = respond_result["message"]
        except ActivityError:
            message = _fallback_message(final_status, results, failure_reason)

        return _build_response(request_id, final_status, results, message)
