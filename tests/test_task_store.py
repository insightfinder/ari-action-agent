import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a2a.server.tasks import InMemoryTaskStore  # noqa: E402
from a2a.types import Task  # noqa: E402

from common.a2a_server.task_store import FileTaskStore, build_task_store  # noqa: E402


class _User:
    def __init__(self, name):
        self.user_name = name
        self.is_authenticated = True


class _Ctx:
    def __init__(self, user="anonymous"):
        self.user = _User(user)
        self.state = {}


def _task(task_id: str) -> Task:
    task = Task()
    task.id = task_id
    task.context_id = "ctx-" + task_id
    return task


@pytest.mark.asyncio
async def test_a_saved_task_survives_a_new_store_over_the_same_directory(tmp_path):
    ctx = _Ctx()
    first = FileTaskStore(str(tmp_path))
    await first.save(_task("t-1"), ctx)

    second = FileTaskStore(str(tmp_path))
    restored = await second.get("t-1", ctx)

    assert restored is not None
    assert restored.id == "t-1"
    assert restored.context_id == "ctx-t-1"


@pytest.mark.asyncio
async def test_an_unknown_task_is_still_none(tmp_path):
    store = FileTaskStore(str(tmp_path))
    assert await store.get("never-saved", _Ctx()) is None


@pytest.mark.asyncio
async def test_delete_removes_it_from_disk_too(tmp_path):
    ctx = _Ctx()
    store = FileTaskStore(str(tmp_path))
    await store.save(_task("t-2"), ctx)
    await store.delete("t-2", ctx)

    assert await FileTaskStore(str(tmp_path)).get("t-2", ctx) is None


@pytest.mark.asyncio
async def test_a_write_failure_does_not_fail_the_request(tmp_path, monkeypatch):
    ctx = _Ctx()
    store = FileTaskStore(str(tmp_path))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write", boom)
    await store.save(_task("t-3"), ctx)

    assert (await store.get("t-3", ctx)).id == "t-3"


@pytest.mark.asyncio
async def test_a_corrupt_file_is_ignored_rather_than_raising(tmp_path):
    ctx = _Ctx()
    store = FileTaskStore(str(tmp_path))
    await store.save(_task("t-4"), ctx)

    for path in tmp_path.rglob("*.pb"):
        path.write_bytes(b"not a protobuf at all")

    assert await FileTaskStore(str(tmp_path)).get("t-4", ctx) is None


def test_an_unset_root_keeps_the_in_memory_store():
    assert isinstance(build_task_store(""), InMemoryTaskStore)


def test_a_root_that_cannot_be_created_falls_back_instead_of_crashing(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    assert isinstance(build_task_store(str(blocker / "tasks")), InMemoryTaskStore)
