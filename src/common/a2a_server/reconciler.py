import asyncio
import os

import structlog
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskManager, TaskStore, TaskUpdater
from a2a.types import Task

from common.a2a_server.executor import _publish_result
from common.a2a_server.task_store import FileTaskStore
from common.orchestration.temporal_client import get_temporal_client

logger = structlog.get_logger()

_CONCURRENCY = int(os.getenv("TASK_RECONCILE_CONCURRENCY", "4"))


class _CollectingQueue(EventQueue):
    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


async def _persist(store: TaskStore, context: ServerCallContext, task: Task, result: dict) -> None:
    queue = _CollectingQueue()
    updater = TaskUpdater(queue, task.id, task.context_id)
    await _publish_result(updater, result)
    manager = TaskManager(store, context, task.id, task.context_id, None)
    for event in queue.events:
        await manager.process(event)


async def _reconcile_one(store: TaskStore, context: ServerCallContext, task: Task, client) -> None:
    handle = client.get_workflow_handle(task.id)
    result = await handle.result()
    await _persist(store, context, task, result)


async def reconcile_unfinished_tasks(store: TaskStore) -> None:
    if not isinstance(store, FileTaskStore):
        return

    try:
        pending = store.unfinished()
    except Exception as exc:
        logger.warning("task_reconcile_scan_failed", error=str(exc))
        return

    if not pending:
        return

    logger.info("task_reconcile_start", count=len(pending), task_ids=[t.id for _, t in pending])

    try:
        client = await get_temporal_client()
    except Exception as exc:
        logger.warning("task_reconcile_no_client", error=str(exc))
        return

    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def run(context: ServerCallContext, task: Task) -> None:
        async with semaphore:
            try:
                await _reconcile_one(store, context, task, client)
                logger.info("task_reconciled", task_id=task.id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("task_reconcile_failed", task_id=task.id, error=str(exc))

    await asyncio.gather(*(run(context, task) for context, task in pending))
