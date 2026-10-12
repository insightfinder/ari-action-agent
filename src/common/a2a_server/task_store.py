import os
import tempfile
import time

import structlog
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.server.tasks.inmemory_task_store import OwnerResolver, resolve_user_scope
from a2a.types import Task, TaskState

logger = structlog.get_logger()

_MAX_AGE_SECONDS = float(os.getenv("TASK_STORE_MAX_AGE_SECONDS", str(7 * 24 * 3600)))

_UNFINISHED_STATES = frozenset(
    {
        TaskState.TASK_STATE_SUBMITTED,
        TaskState.TASK_STATE_WORKING,
    }
)


class _ScopedUser(User):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return bool(self._name)

    @property
    def user_name(self) -> str:
        return self._name


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:200]


class FileTaskStore(TaskStore):
    def __init__(
        self,
        root: str,
        owner_resolver: OwnerResolver = resolve_user_scope,
    ) -> None:
        self._root = root
        self._resolve = owner_resolver
        self._memory = InMemoryTaskStore(owner_resolver=owner_resolver)
        os.makedirs(self._root, exist_ok=True)
        self._prune()

    def _owner_dir(self, owner: str) -> str:
        return os.path.join(self._root, _safe(owner))

    def _path(self, owner: str, task_id: str) -> str:
        return os.path.join(self._owner_dir(owner), _safe(task_id) + ".pb")

    def _prune(self) -> None:
        cutoff = time.time() - _MAX_AGE_SECONDS
        removed = 0
        for dirpath, _, filenames in os.walk(self._root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    continue
        if removed:
            logger.info("task_store_pruned", removed=removed, root=self._root)

    def _write(self, owner: str, task: Task) -> None:
        directory = self._owner_dir(owner)
        os.makedirs(directory, exist_ok=True)
        payload = task.SerializeToString()
        handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, self._path(owner, task.id))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read(self, owner: str, task_id: str) -> Task | None:
        try:
            with open(self._path(owner, task_id), "rb") as fh:
                return Task.FromString(fh.read())
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning("task_store_unreadable", task_id=task_id, error=str(exc))
            return None

    def _remove(self, owner: str, task_id: str) -> None:
        try:
            os.remove(self._path(owner, task_id))
        except OSError:
            pass

    async def _restore_owner(self, owner: str, context: ServerCallContext) -> None:
        directory = self._owner_dir(owner)
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if not name.endswith(".pb"):
                continue
            task_id = name[: -len(".pb")]
            if await self._memory.get(task_id, context) is not None:
                continue
            task = self._read(owner, task_id)
            if task is not None:
                await self._memory.save(task, context)

    async def save(self, task: Task, context: ServerCallContext) -> None:
        await self._memory.save(task, context)
        try:
            self._write(self._resolve(context), task)
        except Exception as exc:
            logger.warning("task_store_write_failed", task_id=task.id, error=str(exc))

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        found = await self._memory.get(task_id, context)
        if found is not None:
            return found
        owner = self._resolve(context)
        task = self._read(owner, task_id)
        if task is None:
            return None
        logger.info("task_store_restored", task_id=task_id)
        await self._memory.save(task, context)
        return task

    async def list(self, params, context: ServerCallContext):
        await self._restore_owner(self._resolve(context), context)
        return await self._memory.list(params, context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        await self._memory.delete(task_id, context)
        self._remove(self._resolve(context), task_id)

    def unfinished(self) -> "list[tuple[ServerCallContext, Task]]":
        found = []
        for dirpath, _, filenames in os.walk(self._root):
            relative = os.path.relpath(dirpath, self._root)
            owner = "" if relative == os.curdir else relative
            for name in sorted(filenames):
                if not name.endswith(".pb"):
                    continue
                task = self._read(owner, name[: -len(".pb")])
                if task is None or task.status.state not in _UNFINISHED_STATES:
                    continue
                found.append((ServerCallContext(user=_ScopedUser(owner)), task))
        return found


def build_task_store(root: str) -> TaskStore:
    if not root:
        return InMemoryTaskStore()
    try:
        return FileTaskStore(root)
    except Exception as exc:
        logger.warning("task_store_falling_back_to_memory", root=root, error=str(exc))
        return InMemoryTaskStore()
