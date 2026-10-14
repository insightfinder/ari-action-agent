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
