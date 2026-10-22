import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chat_gateway.session import SessionStore


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _paused(task_id="t1", action_id="act-1"):
    return {
        "id": task_id,
        "contextId": "c1",
        "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
        "artifacts": [{"name": "pending_action", "parts": [{"data": {"id": action_id}}]}],
    }


def test_start_creates_once_and_then_reuses_the_conversation():
    store = SessionStore()
    first = store.start("th", "alice")
    second = store.start("th", "bob")
    assert first is second
    assert second.requester == "alice"
    assert len(store) == 1


def test_a_fresh_store_is_not_falsy_for_its_users():
    assert len(SessionStore()) == 0


def test_record_task_tracks_ids_state_pending_action_and_config():
    store = SessionStore()
    store.start("th", "alice")
    conversation = store.record_task("th", _paused(), config={"repo": {"url": "u"}})
    assert (conversation.task_id, conversation.context_id) == ("t1", "c1")
    assert conversation.task_state == "TASK_STATE_INPUT_REQUIRED"
    assert conversation.pending_action_id == "act-1"
    assert conversation.config == {"repo": {"url": "u"}}


def test_pending_action_is_cleared_once_the_task_leaves_input_required():
    store = SessionStore()
    store.start("th", "alice")
    store.record_task("th", _paused())
    done = {"id": "t1", "contextId": "c1", "status": {"state": "TASK_STATE_COMPLETED"}}
    conversation = store.record_task("th", done)
    assert conversation.pending_action_id == ""
    assert conversation.config == {}


def test_record_task_keeps_earlier_values_when_the_update_lacks_them():
    store = SessionStore()
    store.start("th", "alice")
    store.record_task("th", {"id": "t1", "contextId": "c1", "status": {"state": "TASK_STATE_WORKING"}}, config={"a": 1})
    conversation = store.record_task("th", {"status": {"state": "TASK_STATE_COMPLETED"}})
    assert (conversation.task_id, conversation.context_id, conversation.config) == ("t1", "c1", {"a": 1})


def test_conversations_expire_after_the_ttl():
    clock = Clock()
    store = SessionStore(ttl_seconds=60, clock=clock)
    store.start("old", "alice")
    clock.now += 61
    store.start("fresh", "bob")
    assert store.get("old") is None
    assert store.get("fresh") is not None
    assert len(store) == 1


def test_recording_a_task_refreshes_the_ttl():
    clock = Clock()
    store = SessionStore(ttl_seconds=60, clock=clock)
    store.start("th", "alice")
    clock.now += 50
    store.record_task("th", {"id": "t1", "status": {"state": "TASK_STATE_WORKING"}})
    clock.now += 50
    assert store.get("th") is not None


def test_forget_drops_a_conversation():
    store = SessionStore()
    store.start("th", "alice")
    store.forget("th")
    store.forget("missing")
    assert store.get("th") is None


def test_a_file_backed_store_survives_a_restart_without_keeping_secrets(tmp_path):
    path = tmp_path / "state" / "sessions.json"
    clock = Clock()
    store = SessionStore(path=path, clock=clock)
    store.start("th", "alice")
    store.record_task("th", _paused(), config={"llm": {"api_key": "sk-secret"}, "repo": {"pat": "ghp_secret"}})
    text = path.read_text(encoding="utf-8")
    assert "sk-secret" not in text and "ghp_secret" not in text
    again = SessionStore(path=path, clock=clock)
    conversation = again.get("th")
    assert (conversation.requester, conversation.task_id, conversation.context_id, conversation.pending_action_id) == ("alice", "t1", "c1", "act-1")
    assert conversation.task_state == "TASK_STATE_INPUT_REQUIRED"
    assert conversation.config == {}


def test_expired_conversations_are_dropped_on_load_and_from_the_file(tmp_path):
    path = tmp_path / "sessions.json"
    clock = Clock()
    store = SessionStore(path=path, clock=clock, ttl_seconds=60)
    store.start("old", "alice")
    clock.now += 61
    again = SessionStore(path=path, clock=clock, ttl_seconds=60)
    assert again.get("old") is None
    assert '"old"' not in path.read_text(encoding="utf-8")


def test_a_damaged_file_starts_empty_instead_of_failing(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{not json", encoding="utf-8")
    assert len(SessionStore(path=path)) == 0
    path.write_text('{"conversations": [{"thread_key": ""}, "junk", {"thread_key": "ok", "updated_at": "nan-ish"}]}', encoding="utf-8")
    assert len(SessionStore(path=path)) == 0


def test_forget_and_an_unwritable_path_do_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    store = SessionStore(path=blocker / "sessions.json")
    store.start("th", "alice")
    store.forget("th")
    assert store.get("th") is None


def test_the_saved_file_is_private(tmp_path):
    import os
    import stat

    import pytest

    if os.name != "posix":
        pytest.skip("file modes are POSIX only")
    path = tmp_path / "sessions.json"
    SessionStore(path=path).start("th", "alice")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
