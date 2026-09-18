import sys
from pathlib import Path

from temporalio.exceptions import ActivityError, ApplicationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_agent.orchestration.workflow import _activity_failure_detail, _fallback_message  # noqa: E402


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
