import sys
from pathlib import Path

from temporalio.exceptions import ActivityError, ApplicationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.orchestration.workflow import (  # noqa: E402
    _DEFAULT_PIPELINE_BUDGET_SECONDS,
    _activity_failure_detail,
    _build_response,
    _fallback_message,
    _pipeline_budget_seconds,
)


def _activity_error(cause) -> ActivityError:
    exc = ActivityError(
        "activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="execute_step",
        activity_id="1",
        retry_state=None,
    )
    exc.__cause__ = cause
    return exc


def test_activity_failure_detail_uses_the_original_exception_message_not_the_wrapper():
    # ActivityError's own str() is generic wrapper noise ("activity task
    # failed") — what a human needs is the real capability exception's
    # message (e.g. publish_pr's "could not find a PR url...").
    cause = ApplicationError("publish_pr: could not find a PR url in opencode's response: 403 ...", type="RuntimeError")
    exc = _activity_error(cause)

    assert _activity_failure_detail(exc) == cause.message


def test_activity_failure_detail_falls_back_to_str_when_cause_has_no_message():
    exc = _activity_error(None)

    assert _activity_failure_detail(exc) == str(exc)


def test_fallback_message_states_the_failure_reason_when_respond_itself_failed():
    # This is the last-resort path if `respond` (the synthesis step) also
    # fails after retries — the caller must still learn *why* the pipeline
    # gave up, not get a bare exception.
    message = _fallback_message("failed", {}, "publish_pr failed: 403 Resource not accessible by personal access token")

    assert "403 Resource not accessible by personal access token" in message


def test_fallback_message_lists_resolved_artifacts_when_completed():
    # artifacts is precomputed by the resolve_artifacts activity (not
    # results) -- see workflow.py's own comment on why that split exists.
    message = _fallback_message("completed", {"pull_request": {"url": "https://x/pull/1"}}, None)

    assert "pull_request" in message and "https://x/pull/1" in message


def test_fallback_message_states_awaiting_confirmation_not_failure_for_needs_approval():
    # A deliberate pause is not a failure -- must not read like one even
    # in this rare last-resort templated path.
    message = _fallback_message("needs_approval", {"pending_action": {"id": "abc"}}, None)

    assert "Awaiting confirmation" in message
    assert "gave up" not in message.lower()


def test_build_response_includes_resolved_artifacts_at_the_top_level():
    response = _build_response("req-1", "completed", {"pull_request": {"url": "https://x/pull/1"}}, {"publish_pr": {"pr": {}}}, "done")

    assert response["pull_request"] == {"url": "https://x/pull/1"}
    assert response["data"] == {"publish_pr": {"pr": {}}}
    assert response["status"] == "completed"


def test_pipeline_budget_defaults_when_the_request_says_nothing():
    assert _pipeline_budget_seconds({}) == float(_DEFAULT_PIPELINE_BUDGET_SECONDS)
    assert _pipeline_budget_seconds({"options": {}}) == float(_DEFAULT_PIPELINE_BUDGET_SECONDS)
    assert _pipeline_budget_seconds({"options": None}) == float(_DEFAULT_PIPELINE_BUDGET_SECONDS)


def test_pipeline_budget_honours_a_per_request_override():
    # A caller that knows a particular repo is genuinely slow can raise it
    # for that request instead of needing the default changed and the
    # worker redeployed.
    assert _pipeline_budget_seconds({"options": {"pipeline_budget_seconds": 7200}}) == 7200.0
    assert _pipeline_budget_seconds({"options": {"pipeline_budget_seconds": "900"}}) == 900.0


def test_pipeline_budget_ignores_values_that_would_disable_the_cap():
    # Zero/negative/garbage must not become "no ceiling" -- that is exactly
    # the state this cap exists to end (stg workflow 9b32a817 ran 45m56s
    # with nothing bounding the pipeline as a whole).
    for bad in (0, -1, "", "soon", None, [], {}):
        assert _pipeline_budget_seconds({"options": {"pipeline_budget_seconds": bad}}) == float(
            _DEFAULT_PIPELINE_BUDGET_SECONDS
        ), bad


def test_pipeline_budget_default_leaves_room_inside_the_callers_own_backstop():
    # Two separate constraints, and the tighter one is the caller's.
    #
    # 9b32a817 ran 2756s against a ~3.2 hour design maximum, so the
    # default has to sit below that to be worth anything at all. But MCP
    # Service -- the caller that actually drives this agent -- gives one
    # agent call ONCALL_TASK_AGENT_TIMEOUT_S (3600s on test, 2026-10-09)
    # before its own Temporal activity gives up. This budget bounds the
    # step loop only; cleanup_workspace (30s x3), resolve_artifacts
    # (10s x3) and respond (120s x2) can add ~360s after it. A default
    # that does not leave that tail inside the caller's backstop produces
    # an answer nobody is still waiting for.
    callers_backstop = 3600
    post_loop_tail = 30 * 3 + 10 * 3 + 120 * 2
    assert _DEFAULT_PIPELINE_BUDGET_SECONDS + post_loop_tail < callers_backstop
