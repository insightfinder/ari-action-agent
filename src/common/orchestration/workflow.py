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

# A ceiling on the whole pipeline, not on any one step.
#
# Every per-step bound below is a *per-attempt* bound: the step loop's
# `start_to_close_timeout` caps one attempt, and `retry.max_attempts`
# multiplies it. Nothing capped their product, and nothing capped the
# `on_failure` loop that re-runs whole steps on top of that. Confirmed
# live (stg workflow 9b32a817, 2026-10-08) — a single request ran
# 45m56s and every second of it was inside the configured budget:
#
#   verify              TIMEOUT @480s   (timeout_seconds: 480)
#   generate_change     TIMEOUT @1200s  (default, attempt 1 of 2)
#   generate_change     OK @20s         (attempt 2, after a 10s backoff)
#   verify              TIMEOUT @481s
#   generate_change     ...             (cancelled by hand at 506s)
#
# For that plan the design maximum is ~3.2 hours (4 passes of the
# verify -> generate_change loop, each up to 1200s x 2 + 480s), which no
# caller waits for and no on-call engineer should have to kill by hand.
#
# Enforced with `schedule_to_close_timeout` rather than a check between
# steps, because a between-steps check cannot interrupt the step that is
# already overrunning — which is exactly the case that produced the 45
# minutes above. Temporal applies it across all of an activity's retries,
# so the remaining budget binds the attempt *and* its retries.
#
# 3000s, not a round hour, and the difference is the point. The caller
# that matters is MCP Service, whose own `ONCALL_TASK_AGENT_TIMEOUT_S`
# (3600s on test, 2026-10-09) is the Temporal activity timeout on one
# agent call — and its `agent_timeout_s()` docstring states plainly that
# "the task path sets no deadline inside the agent itself", so this
# budget is the only in-agent bound there is. A budget equal to that
# backstop would be worthless: this bounds the *step loop*, after which
# cleanup_workspace (30s x3), resolve_artifacts (10s x3) and respond
# (120s x2) can still add ~6 minutes, so a 3600s budget answers at ~3960s
# to a caller that gave up at 3600s and threw the answer away. 3000s
# leaves that tail room and ~10 minutes of margin. Raise this only
# together with the caller's own timeout.
#
# MCP's chat path is tighter still (`arioncall.yaml`'s `a2a.timeout_s`,
# 1200s) but needs nothing here: that caller cancels the remote task
# itself when it runs out of patience. This default is sized for the
# task path, which is the one that opens pull requests and, per the
# same docstring, imposes no deadline of its own. Note also that MCP
# sends `"options": {}` (app/oncall/ari_oncall_agent.py), so the
# per-request override below is never set in production — this constant
# is the number that actually runs.
_DEFAULT_PIPELINE_BUDGET_SECONDS = 3000

# Below this, scheduling the next step is pointless — it would be killed
# almost immediately and the run would report a timeout against a step
# that never had a chance to do anything. Stopping first gives a clearer
# failure, and `respond` still runs either way.
# How long Temporal waits for a beat from `execute_step` before declaring
# the attempt lost and retrying it. activities.py beats every 20s.
#
# This is the only thing that makes a dead worker cheap. Without it the
# only signal is `start_to_close_timeout`, which is 1200s for
# `generate_change` and 480s for `verify` — measured on stg 2026-10-09,
# one OOMKill cost 4m15s of a 50-minute budget and one pod replacement
# cost the full 20 minutes, both of them spent waiting on a worker that
# was already gone.
#
# 90s, not 30s: a beat is one RPC from an activity that is otherwise busy
# in a subprocess, and a worker briefly starved of CPU (stg runs on t3/t3a,
# where hypervisor steal of 50-70% has been measured) must not be declared
# dead for missing one. Four intervals of headroom.
#
# It also opens the channel Temporal delivers cancellation on, which is
# why a timed-out attempt's opencode subprocess used to keep running in
# the pod after the workflow had moved on.
_STEP_HEARTBEAT_TIMEOUT_SECONDS = 90

_MIN_USEFUL_STEP_SECONDS = 30


def _pipeline_budget_seconds(config: dict) -> float:
    """Per-request override, falling back to the module default. Lives in
    `options` alongside `skip_pr` so a caller that knows a particular repo
    is slow can raise it without a redeploy."""
    raw = (config.get("options") or {}).get("pipeline_budget_seconds")
    try:
        budget = float(raw)
    except (TypeError, ValueError):
        return float(_DEFAULT_PIPELINE_BUDGET_SECONDS)
    return budget if budget > 0 else float(_DEFAULT_PIPELINE_BUDGET_SECONDS)


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


def _fallback_message(status: str, artifacts: dict[str, Any], failure_reason: str | None) -> str:
    """Only used if the `respond` step itself fails after retries — a
    templated last resort so a request always gets *some* message back,
    never an exception surfaced to the caller. `artifacts` is precomputed
    by the `resolve_artifacts` activity (see orchestration/activities.py)
    — this function takes the resolved dict directly rather than calling
    get_agent_meta() itself, since that transitively imports a2a.types
    and would break Temporal's workflow-sandbox import restrictions if
    called from here (confirmed live — see that activity's docstring)."""
    if status == "failed":
        return f"Gave up after exhausting retries: {failure_reason}"
    if status == "completed":
        if artifacts:
            return "Completed: " + ", ".join(f"{k}: {v}" for k, v in artifacts.items())
        return "Completed."
    if status == "needs_approval":
        return "Awaiting confirmation before proceeding: " + ", ".join(f"{k}: {v}" for k, v in artifacts.items())
    return "No action was needed for this request."


def _build_response(request_id: str, status: str, artifacts: dict[str, Any], results: dict[str, Any], message: str) -> dict:
    return {
        "request_id": request_id,
        "status": status,
        "message": message,
        **artifacts,
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
                    {
                        "prompt": request["prompt"],
                        "config": config,
                        "history": history,
                        "confirmed_action": request.get("confirmed_action"),
                    },
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5)),
                )
                steps = plan["steps"]
                final_status = "completed" if steps else "no_action"
            except ActivityError as exc:
                final_status = "failed"
                failure_reason = f"planning failed: {_activity_failure_detail(exc)}"

        # `workflow.info().start_time` is the deterministic workflow clock,
        # so the budget covers planning and workspace setup too — the
        # caller's wait started there, not here. A confirmation resume runs
        # as its own workflow, so it gets its own full budget rather than
        # inheriting whatever the proposing run spent.
        deadline = workflow.info().start_time + timedelta(seconds=_pipeline_budget_seconds(config))

        i = 0
        while i < len(steps):
            step = steps[i]
            capability = step["capability"]
            retry = step.get("retry") or {}
            on_failure = step.get("on_failure")
            remaining = (deadline - workflow.now()).total_seconds()
            if remaining < _MIN_USEFUL_STEP_SECONDS:
                final_status = "failed"
                failure_reason = (
                    f"pipeline budget of {_pipeline_budget_seconds(config):g}s exhausted before "
                    f"{capability} could run"
                )
                break

            # A capability can fail two ways: a "soft" failure it reports
            # itself (e.g. verify's {"status": "failed", ...}, handled by
            # the on_failure retry-loop below), or a raised exception (e.g.
            # publish_pr's RuntimeError when no PR came back, run_opencode's
            # RuntimeError on a non-zero exit, git_ops.clone's RuntimeError,
            # an activity timing out, or Temporal cancelling it because the
            # caller gave up waiting) that Temporal retries at the activity
            # level and then re-raises as ActivityError once exhausted.
            # Confirmed live: `verify` (no `on_failure` policy before this
            # fix would have skipped it) routinely got killed this way —
            # its build/test run outlasted callers that cancel after
            # ~8-10 minutes — and every single one of those runs aborted
            # the *entire* pipeline instead of looping back to
            # generate_change like a normal reported test failure already
            # does. When the step has an `on_failure` policy, a raised
            # exception is now folded into the same soft-failure shape and
            # handled by the identical retry loop below; only a step with
            # no `on_failure` policy still aborts the pipeline outright.
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
                        "confirmed": step.get("confirmed", False),
                    },
                    start_to_close_timeout=timedelta(seconds=step.get("timeout_seconds") or 1200),
                    # The budget left for the whole run, applied across this
                    # step's retries — see _DEFAULT_PIPELINE_BUDGET_SECONDS.
                    # A step that would outlive the budget is cut short here
                    # rather than after the fact.
                    #
                    # Temporal also clamps `start_to_close_timeout` down to
                    # this when it is the smaller of the two, so no `min()`
                    # is needed above. Confirmed live (test, 2026-10-09,
                    # workflow 613127ee with a 120s budget): `verify` was
                    # scheduled with sttc=stc=44.86s, not the 480s its own
                    # `timeout_seconds` asks for, because 44.86s was all the
                    # budget had left — and it was cut at exactly 120.0s.
                    # Worth knowing when reading a history: the per-step
                    # timeout shown there is the effective one, not the one
                    # in capabilities.yaml.
                    schedule_to_close_timeout=timedelta(seconds=remaining),
                    heartbeat_timeout=timedelta(seconds=_STEP_HEARTBEAT_TIMEOUT_SECONDS),
                    retry_policy=RetryPolicy(
                        maximum_attempts=retry.get("max_attempts", 1),
                        backoff_coefficient=2.0,
                        initial_interval=timedelta(seconds=retry.get("backoff_seconds") or 1),
                    ),
                )
            except ActivityError as exc:
                if not on_failure:
                    final_status = "failed"
                    failure_reason = f"{capability} failed: {_activity_failure_detail(exc)}"
                    break
                step_result = {"status": "inconclusive", "failure": _activity_failure_detail(exc)}

            results[capability] = step_result

            # A step can request a hard human-in-the-loop pause (e.g.
            # jenkins_agent's propose_action for a trigger_build) — this
            # ends the run here as a genuine "waiting for your input"
            # state, not a failure: `_TERMINAL_STATE_BY_STATUS` in
            # a2a_server/executor.py maps "needs_approval" to
            # TASK_STATE_INPUT_REQUIRED. `respond` still runs below
            # unconditionally, same as any other ending.
            if step_result.get("requires_approval"):
                final_status = "needs_approval"
                break

            if on_failure and step_result.get("status") in ("failed", "inconclusive"):
                attempts = loop_attempts.get(capability, 0) + 1
                loop_attempts[capability] = attempts
                if attempts > on_failure["max_attempts"]:
                    # `then: continue` keeps the pipeline going with the
                    # failure recorded, instead of ending the run here.
                    #
                    # Added because the alternative threw away finished work.
                    # `verify` is the only step with an `on_failure` loop, and
                    # on a large Java repo it exhausted all four attempts on
                    # stg (2026-10-10, runs fc532091 and 1c2396a4) — at which
                    # point `break` skipped `propose_pr` entirely, so a change
                    # that had already been generated was never pushed, never
                    # proposed, and never seen by anyone. The run reported
                    # `failed` with the diff discarded.
                    #
                    # That is the wrong trade for this pipeline. A pull request
                    # is not merged by the agent; it pauses for a person who
                    # reads the diff. An unverified change, labelled as
                    # unverified, is strictly more useful to that person than
                    # no change at all — and they can always run the build
                    # themselves, which is exactly what the agent could not
                    # finish in time.
                    #
                    # `fail_needs_human` is still available and unchanged, for
                    # a step whose failure really should stop everything.
                    failure_reason = step_result.get("failure")
                    if on_failure["then"] == "continue":
                        if step_result.get("status") == "inconclusive":
                            i += 1
                            continue
                        final_status = "failed"
                        break
                    final_status = "failed" if on_failure["then"] == "fail_needs_human" else on_failure["then"]
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

        # This agent kind's own declared artifacts (agent_meta.artifacts),
        # resolved against this run's actual results. An activity, not a
        # plain function called from here directly — get_agent_meta()
        # transitively imports a2a.types, which breaks Temporal's
        # workflow-sandbox import restrictions if pulled into this
        # module's own import chain (confirmed live: the default
        # SandboxedWorkflowRunner refuses to even construct a Worker for
        # this workflow otherwise). Same reasoning as why plan_task, not
        # workflow.py, is the one that calls get_capability().
        try:
            artifacts = await workflow.execute_activity(
                "resolve_artifacts",
                results,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2)),
            )
        except ActivityError:
            artifacts = {}

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
                heartbeat_timeout=timedelta(seconds=_STEP_HEARTBEAT_TIMEOUT_SECONDS),
                retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=5)),
            )
            message = respond_result["message"]
        except ActivityError:
            message = _fallback_message(final_status, artifacts, failure_reason)

        return _build_response(request_id, final_status, artifacts, results, message)
