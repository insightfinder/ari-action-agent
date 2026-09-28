import sys
from pathlib import Path

from temporalio.exceptions import ActivityError, ApplicationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.orchestration.workflow import (  # noqa: E402
    _activity_failure_detail,
    _build_response,
    _fallback_message,
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
