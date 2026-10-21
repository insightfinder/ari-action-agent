import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.orchestration import worker  # noqa: E402
from common.settings import Settings  # noqa: E402

CHAT = {"options": {"lane": "interactive"}}


def test_chat_requests_get_their_own_queue():
    settings = Settings(temporal_task_queue="oncall-ari-queue")

    assert settings.task_queue_for(CHAT) == "oncall-ari-queue-interactive"


def test_everything_else_stays_on_the_main_queue():
    settings = Settings(temporal_task_queue="oncall-ari-queue")

    assert settings.task_queue_for({"options": {}}) == "oncall-ari-queue"
    assert settings.task_queue_for({}) == "oncall-ari-queue"
    assert settings.task_queue_for(None) == "oncall-ari-queue"
    assert settings.task_queue_for({"options": {"lane": "batch"}}) == "oncall-ari-queue"


def test_the_interactive_queue_name_can_be_set():
    settings = Settings(temporal_task_queue="q", temporal_interactive_task_queue="chat-q")

    assert settings.task_queue_for(CHAT) == "chat-q"


def test_zero_interactive_slots_turns_the_lane_off():
    settings = Settings(temporal_task_queue="q", interactive_max_concurrent_activities=0)

    assert settings.task_queue_for(CHAT) == "q"
    assert settings.interactive_task_queue() == ""


def test_the_worker_polls_both_queues_with_separate_limits(monkeypatch):
    built = []

    class _Worker:
        def __init__(self, client, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(worker, "Worker", _Worker)
    settings = Settings(temporal_task_queue="q", max_concurrent_activities=2, interactive_max_concurrent_activities=1)

    worker.build_workers(object(), settings)

    assert [(w["task_queue"], w["max_concurrent_activities"]) for w in built] == [("q", 2), ("q-interactive", 1)]
    assert all(w["activities"] == worker.ACTIVITIES for w in built)


def test_the_worker_polls_only_the_main_queue_when_the_lane_is_off(monkeypatch):
    built = []

    class _Worker:
        def __init__(self, client, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(worker, "Worker", _Worker)

    worker.build_workers(object(), Settings(temporal_task_queue="q", interactive_max_concurrent_activities=0))

    assert [w["task_queue"] for w in built] == ["q"]
