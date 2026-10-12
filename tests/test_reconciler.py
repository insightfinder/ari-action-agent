import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a2a.server.tasks import InMemoryTaskStore  # noqa: E402
from a2a.types import Task, TaskState  # noqa: E402

from common.a2a_server import reconciler  # noqa: E402
from common.a2a_server.reconciler import reconcile_unfinished_tasks  # noqa: E402
from common.a2a_server.task_store import FileTaskStore  # noqa: E402


class _User:
    def __init__(self, name):
        self.user_name = name
        self.is_authenticated = True


class _Ctx:
    def __init__(self, user=""):
        self.user = _User(user)
        self.state = {}


class _Handle:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    async def result(self):
        if self._error is not None:
            raise self._error
        return self._result


class _Client:
    def __init__(self, handles):
        self._handles = handles
        self.asked = []

    def get_workflow_handle(self, workflow_id):
        self.asked.append(workflow_id)
        return self._handles[workflow_id]


def _task(task_id: str, state) -> Task:
    task = Task()
    task.id = task_id
    task.context_id = "ctx-" + task_id
    task.status.state = state
    return task


def _use_client(monkeypatch, client):
    monkeypatch.setattr(reconciler, "get_temporal_client", AsyncMock(return_value=client))


_PAUSE = {
    "status": "needs_approval",
    "message": "Approve opening the pull request?",
    "pending_action": {"id": "p-1", "action": "open_pull_request", "branch": "ari/fix-1"},
}


@pytest.mark.asyncio
async def test_a_task_orphaned_by_a_dead_request_gets_its_pending_action_persisted(tmp_path, monkeypatch):
    ctx = _Ctx()
    store = FileTaskStore(str(tmp_path))
    await store.save(_task("t-1", TaskState.TASK_STATE_WORKING), ctx)

    _use_client(monkeypatch, _Client({"t-1": _Handle(result=_PAUSE)}))

    await reconcile_unfinished_tasks(FileTaskStore(str(tmp_path)))

    restored = await FileTaskStore(str(tmp_path)).get("t-1", ctx)
    assert restored.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert [a.name for a in restored.artifacts] == ["pending_action"]


@pytest.mark.asyncio
async def test_the_persisted_pending_action_is_the_one_an_approval_resolves_against(tmp_path, monkeypatch):
    from common.a2a_server.executor import _find_pending_action

    ctx = _Ctx()
    store = FileTaskStore(str(tmp_path))
    await store.save(_task("t-2", TaskState.TASK_STATE_WORKING), ctx)
    _use_client(monkeypatch, _Client({"t-2": _Handle(result=_PAUSE)}))

    await reconcile_unfinished_tasks(FileTaskStore(str(tmp_path)))

    restored = await FileTaskStore(str(tmp_path)).get("t-2", ctx)
    assert _find_pending_action(restored, "p-1") == _PAUSE["pending_action"]


@pytest.mark.asyncio
async def test_a_finished_task_is_never_re_run(tmp_path, monkeypatch):
    ctx = _Ctx()
    store = FileTaskStore(str(tmp_path))
    await store.save(_task("done", TaskState.TASK_STATE_COMPLETED), ctx)
    await store.save(_task("failed", TaskState.TASK_STATE_FAILED), ctx)
    await store.save(_task("paused", TaskState.TASK_STATE_INPUT_REQUIRED), ctx)

    client = _Client({})
    _use_client(monkeypatch, client)

    await reconcile_unfinished_tasks(FileTaskStore(str(tmp_path)))

    assert client.asked == []


@pytest.mark.asyncio
async def test_a_workflow_that_cannot_be_read_leaves_the_task_alone(tmp_path, monkeypatch):
    ctx = _Ctx()
    store = FileTaskStore(str(tmp_path))
    await store.save(_task("t-3", TaskState.TASK_STATE_WORKING), ctx)

    _use_client(monkeypatch, _Client({"t-3": _Handle(error=RuntimeError("workflow not found"))}))

    await reconcile_unfinished_tasks(FileTaskStore(str(tmp_path)))

    restored = await FileTaskStore(str(tmp_path)).get("t-3", ctx)
    assert restored.status.state == TaskState.TASK_STATE_WORKING


@pytest.mark.asyncio
async def test_one_unreadable_workflow_does_not_stop_the_others(tmp_path, monkeypatch):
    ctx = _Ctx()
    store = FileTaskStore(str(tmp_path))
    await store.save(_task("bad", TaskState.TASK_STATE_WORKING), ctx)
    await store.save(_task("good", TaskState.TASK_STATE_WORKING), ctx)

    _use_client(
        monkeypatch,
        _Client(
            {
                "bad": _Handle(error=RuntimeError("gone")),
                "good": _Handle(result=_PAUSE),
            }
        ),
    )

    await reconcile_unfinished_tasks(FileTaskStore(str(tmp_path)))

    restored = await FileTaskStore(str(tmp_path)).get("good", ctx)
    assert restored.status.state == TaskState.TASK_STATE_INPUT_REQUIRED


@pytest.mark.asyncio
async def test_an_in_memory_store_is_a_no_op(monkeypatch):
    client = _Client({})
    _use_client(monkeypatch, client)

    await reconcile_unfinished_tasks(InMemoryTaskStore())

    assert client.asked == []


@pytest.mark.asyncio
async def test_a_task_saved_under_an_owner_is_found_and_written_back_under_that_owner(tmp_path, monkeypatch):
    ctx = _Ctx("alice")
    store = FileTaskStore(str(tmp_path))
    await store.save(_task("t-4", TaskState.TASK_STATE_WORKING), ctx)

    _use_client(monkeypatch, _Client({"t-4": _Handle(result=_PAUSE)}))

    await reconcile_unfinished_tasks(FileTaskStore(str(tmp_path)))

    restored = await FileTaskStore(str(tmp_path)).get("t-4", ctx)
    assert restored.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert (tmp_path / "alice" / "t-4.pb").exists()


@pytest.mark.asyncio
async def test_no_temporal_client_degrades_instead_of_crashing(tmp_path, monkeypatch):
    ctx = _Ctx()
    store = FileTaskStore(str(tmp_path))
    await store.save(_task("t-5", TaskState.TASK_STATE_WORKING), ctx)

    monkeypatch.setattr(
        reconciler, "get_temporal_client", AsyncMock(side_effect=RuntimeError("no frontend"))
    )

    await reconcile_unfinished_tasks(FileTaskStore(str(tmp_path)))

    restored = await FileTaskStore(str(tmp_path)).get("t-5", ctx)
    assert restored.status.state == TaskState.TASK_STATE_WORKING
