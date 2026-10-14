import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chat_gateway.rendering import (
    MAX_HUNK_CHARS,
    render,
    render_final,
    render_pending,
    render_progress,
    render_proposal,
    state_label,
)
from chat_gateway.tasks import artifacts, is_finished, is_paused, is_running, pending_action, status_text


def _task(state, artifacts_=None, text=""):
    task = {"id": "0123456789abcdef", "contextId": "c1", "status": {"state": state}}
    if text:
        task["status"]["message"] = {"role": "ROLE_AGENT", "parts": [{"text": text}]}
    if artifacts_:
        task["artifacts"] = artifacts_
    return task


def _pending(**overrides):
    action = {
        "id": "act-1",
        "action": "open_pull_request",
        "action_name": "Open pull request",
        "repo": "org/repo",
        "repo_url": "https://github.com/org/repo",
        "branch": "ari/fix-abc",
        "base_ref": "dev",
        "title": "Fix the null check",
        "changed_files": ["a.py", "b.py"],
        "verification": {"status": "inconclusive", "detail": "timed out"},
        "compare_url": "https://github.com/org/repo/compare/dev...ari/fix-abc",
        "key_hunks": "diff --git a/a.py b/a.py\n+if x is None:\n+    return",
    }
    action.update(overrides)
    return [{"name": "pending_action", "parts": [{"data": action}]}]


def test_artifacts_takes_the_latest_data_part_per_name():
    task = _task(
        "TASK_STATE_COMPLETED",
        [
            {"name": "root_cause", "parts": [{"text": "ignored"}, {"data": {"summary": "first"}}]},
            {"name": "root_cause", "parts": [{"data": {"summary": "second"}}]},
            {"name": "pull_request", "parts": [{"data": {"url": "u"}}]},
        ],
    )
    assert artifacts(task) == {"root_cause": {"summary": "second"}, "pull_request": {"url": "u"}}


def test_a_second_proposal_on_the_same_task_replaces_the_first():
    task = _task("TASK_STATE_INPUT_REQUIRED", _pending(id="act-1") + _pending(id="act-2", title="Second"))
    assert pending_action(task)["id"] == "act-2"
    assert [b.pending_action_id for b in render_pending(task).buttons] == ["act-2", "act-2"]


def test_malformed_response_shapes_do_not_raise():
    task = {
        "id": 7,
        "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"parts": "oops"}},
        "artifacts": ["junk", None, {"name": "pending_action", "parts": [{"data": {"id": 3, "changed_files": [1, None], "key_hunks": ["x"], "verification": "no"}}]}],
    }
    rendered = render(task)
    assert "Files: 1, " in rendered.text
    assert rendered.buttons[0].pending_action_id == "3"
    assert status_text(task) == ""
    assert artifacts({"artifacts": None}) == {}


def test_status_text_joins_text_parts_only():
    task = _task("TASK_STATE_COMPLETED")
    task["status"]["message"] = {"parts": [{"text": "a"}, {"data": {}}, {"text": "b"}]}
    assert status_text(task) == "a\nb"


def test_state_predicates():
    assert is_running(_task("TASK_STATE_WORKING"))
    assert is_paused(_task("TASK_STATE_INPUT_REQUIRED"))
    assert is_finished(_task("TASK_STATE_FAILED"))
    assert not is_finished(_task("TASK_STATE_INPUT_REQUIRED"))
    assert not is_finished({"status": {}})


def test_state_label_falls_back_to_the_raw_state():
    assert state_label(_task("TASK_STATE_REJECTED")) == "rejected"
    assert state_label(_task("TASK_STATE_WORKING")) == "working"


def test_progress_names_artifacts_produced_so_far_but_not_the_proposal():
    task = _task("TASK_STATE_WORKING", _pending() + [{"name": "root_cause", "parts": [{"data": {"summary": "s"}}]}])
    rendered = render_progress(task)
    assert rendered.text == "working (task 01234567); produced: root_cause"
    assert rendered.state == "TASK_STATE_WORKING"


def test_pending_render_shows_the_proposal_and_offers_both_buttons():
    rendered = render_pending(_task("TASK_STATE_INPUT_REQUIRED", _pending(), text="Ready to open a PR."))

    assert rendered.text.startswith("Ready to open a PR.")
    assert "Proposed: Open pull request" in rendered.text
    assert "Repo: org/repo (ari/fix-abc -> dev)" in rendered.text
    assert "Title: Fix the null check" in rendered.text
    assert "Files: a.py, b.py" in rendered.text
    assert "Verification: inconclusive (timed out)" in rendered.text
    assert "Full diff: https://github.com/org/repo/compare/dev...ari/fix-abc" in rendered.text
    assert "+if x is None:" in rendered.text
    assert [(b.action, b.task_id, b.pending_action_id) for b in rendered.buttons] == [
        ("approve", "0123456789abcdef", "act-1"),
        ("decline", "0123456789abcdef", "act-1"),
    ]
    assert rendered.links == ("https://github.com/org/repo/compare/dev...ari/fix-abc",)


def test_pending_render_without_an_artifact_has_no_buttons():
    rendered = render_pending(_task("TASK_STATE_INPUT_REQUIRED", text="waiting"))
    assert rendered.text == "waiting"
    assert rendered.buttons == ()


def test_proposal_truncates_long_file_lists_and_hunks():
    files = [f"f{i}.py" for i in range(12)]
    text = render_proposal(_pending(changed_files=files, key_hunks="x" * (MAX_HUNK_CHARS + 50))[0]["parts"][0]["data"])
    assert "f7.py (+4 more)" in text
    assert "... (truncated)" in text
    assert "x" * (MAX_HUNK_CHARS + 1) not in text


def test_final_render_adds_the_pull_request_link():
    task = _task(
        "TASK_STATE_COMPLETED",
        [{"name": "pull_request", "parts": [{"data": {"url": "https://github.com/org/repo/pull/7", "branch": "b"}}]}],
        text="Opened the PR.",
    )
    rendered = render_final(task)
    assert rendered.text == "Opened the PR.\n\nPull request: https://github.com/org/repo/pull/7"
    assert rendered.links == ("https://github.com/org/repo/pull/7",)


def test_final_render_without_a_message_states_the_outcome():
    rendered = render_final(_task("TASK_STATE_CANCELED"))
    assert rendered.text == "canceled (task 01234567)"


def test_render_dispatches_on_state():
    assert render(_task("TASK_STATE_SUBMITTED")).text.startswith("queued")
    assert render(_task("TASK_STATE_INPUT_REQUIRED", _pending())).buttons
    assert render(_task("TASK_STATE_FAILED", text="boom")).text == "boom"
