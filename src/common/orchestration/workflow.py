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

from common.agent_meta import get_agent_meta


def _activity_failure_detail(exc: ActivityError) -> str:
    """The full message from whatever exception a capability raised (e.g.
    `publish_pr`'s "could not find a PR url...", `run_opencode`'s "opencode
    (...) failed...", a raw `git clone failed: ...`) — Temporal wraps it in
    an `ApplicationError` as `exc.cause`, whose `.message` is the original
    exception's clean `str()` (no "ActivityError: ..." wrapper noise)."""
    cause = exc.cause
    if cause is not None and getattr(cause, "message", None):
        return cause.message
    return str(cause) if cause is not None else str(exc)


def _artifacts(results: dict[str, Any]) -> dict[str, Any]:
    """This agent kind's own declared artifacts (agent_meta.artifacts,
    e.g. oncall's "root_cause": "analyze.root_cause"), resolved against
    this run's actual results — only the ones present and truthy."""
    found = {}
    for name, dotted in get_agent_meta().artifacts.items():
        capability, key = dotted.split(".", 1)
        value = results.get(capability, {}).get(key)
        if value:
            found[name] = value
    return found


def _fallback_message(status: str, results: dict[str, Any], failure_reason: str | None) -> str:
    """Only used if the `respond` step itself fails after retries — a
    templated last resort so a request always gets *some* message back,
    never an exception surfaced to the caller."""
    if status == "failed":
        return f"Gave up after exhausting retries: {failure_reason}"
    if status == "completed":
        artifacts = _artifacts(results)
        if artifacts:
            return "Completed: " + ", ".join(f"{k}: {v}" for k, v in artifacts.items())
        return "Completed."
    return "No action was needed for this request."


def _build_response(request_id: str, status: str, results: dict[str, Any], message: str) -> dict:
    return {
        "request_id": request_id,
        "status": status,
        "message": message,
        **_artifacts(results),
        "data": results,
    }


@workflow.defn(name="AriAgentWorkflow")
class AriAgentWorkflow:
    @workflow.run
    async def run(self, request: dict) -> dict:
        request_id = workflow.info().workflow_id
        config = request["config"]

        history = request.get("history", [])

        results: dict[str, Any] = {}
        loop_attempts: dict[str, int] = {}
        steps: list[dict] = []
        workspace: str | None = None
        final_status = "no_action"
        failure_reason: str | None = None

        # Wrapped like every other activity call below: if this fails after
        # retries, nothing was actually created (create_workspace is a
        # single mkdir), so there's no sandbox to leak — but letting the
        # exception escape `run()` would still skip `respond`, leaving the
        # caller with the SDK's generic error instead of a real explanation.
        try:
            workspace = await workflow.execute_activity(
                "prepare_workspace",
                request_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            final_status = "failed"
            failure_reason = f"workspace setup failed: {_activity_failure_detail(exc)}"

        # Explicit, bounded retry — found live while testing the A2A
        # integration with a deliberately bad LLM key: without this,
        # Temporal's default retry policy has no attempt cap, so an
        # invalid API key (or any other non-retryable planner failure)
        # made `message/send`'s blocking wait hang far past any reasonable
        # timeout instead of failing fast. Also wrapped in try/except, same
        # reasoning as the step loop below: `prepare_workspace` already ran
        # by this point, so letting this exception escape `run()` would
        # leak that workspace (never reaching `cleanup_workspace`) and
        # leave the caller with the SDK's generic error instead of a real
        # explanation from `respond`. Skipped entirely if workspace setup
        # itself already failed above — there's nothing to plan against.
        if workspace is not None:
            try:
                plan = await workflow.execute_activity(
                    "plan_task",
                    {"prompt": request["prompt"], "config": config, "history": history},
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5)),
                )
                steps = plan["steps"]
                final_status = "completed" if steps else "no_action"
            except ActivityError as exc:
                final_status = "failed"
                failure_reason = f"planning failed: {_activity_failure_detail(exc)}"

        i = 0
        while i < len(steps):
            step = steps[i]
            capability = step["capability"]
            retry = step.get("retry") or {}

            # A capability can fail two ways: a "soft" failure it reports
            # itself (e.g. verify's {"status": "failed", ...}, handled by
            # the on_failure retry-loop below), or a raised exception (e.g.
            # publish_pr's RuntimeError when no PR came back, run_opencode's
            # RuntimeError on a non-zero exit, git_ops.clone's RuntimeError)
            # that Temporal retries at the activity level and then re-raises
            # as ActivityError once exhausted. That second case has no
            # `on_failure` policy to catch it, so it must be caught here —
            # otherwise it escapes `run()` entirely, skipping both
            # `cleanup_workspace` and `respond` below and leaving the caller
            # with nothing but the SDK's generic "workflow execution failed",
            # no explanation of what actually went wrong.
            try:
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
            except ActivityError as exc:
                final_status = "failed"
                failure_reason = f"{capability} failed: {_activity_failure_detail(exc)}"
                break

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

        # Bounded retry, same reasoning as plan_task above — an unset
        # RetryPolicy defaults to unlimited attempts, and this must never be
        # what stalls a response indefinitely. In practice `cleanup_workspace`
        # (tools/sandbox.py) uses `shutil.rmtree(..., ignore_errors=True)`, so
        # it essentially never raises — this cap is defense in depth, not a
        # response to an observed failure.
        try:
            await workflow.execute_activity(
                "cleanup_workspace",
                request_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5)),
            )
        except ActivityError:
            # Never let a cleanup failure prevent the caller from getting a
            # real response below — a leaked sandbox directory is a disk-
            # space concern to investigate separately, not a reason to come
            # back empty-handed.
            pass

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
