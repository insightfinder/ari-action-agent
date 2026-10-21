import asyncio

import structlog
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskManager, TaskStore, TaskUpdater
from a2a.types import Part, Task

from common.a2a_server.task_store import FileTaskStore

from .executor import CoordinatorExecutor, publish

logger = structlog.get_logger()

CONCURRENCY = 4


class _CollectingQueue(EventQueue):
    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


async def _persist(store: TaskStore, context: ServerCallContext, task: Task, write) -> None:
    queue = _CollectingQueue()
    updater = TaskUpdater(queue, task.id, task.context_id)
    await write(updater)
    manager = TaskManager(store, context, task.id, task.context_id, None)
    for event in queue.events:
        await manager.process(event)


async def _reconcile_one(store: TaskStore, executor: CoordinatorExecutor, context: ServerCallContext, task: Task) -> None:
    link = executor.store.task(task.id)
    agent = executor.registry.get(link.agent) if link else None
    if link is None or agent is None:
        text = "The coordinator restarted before this request reached an agent. Send it again."

        async def write(updater: TaskUpdater) -> None:
            await updater.failed(message=updater.new_agent_message([Part(text=text)]))

        await _persist(store, context, task, write)
        return
    downstream = await executor.follow(agent, link.task_id)

    async def write(updater: TaskUpdater) -> None:
        await publish(updater, downstream)

    await _persist(store, context, task, write)


async def reconcile_unfinished_tasks(store: TaskStore, executor: CoordinatorExecutor) -> None:
    if not isinstance(store, FileTaskStore):
        return
    try:
        pending = store.unfinished()
    except Exception as exc:
        logger.warning("coordinator_reconcile_scan_failed", error=str(exc))
        return
    if not pending:
        return
    logger.info("coordinator_reconcile_start", count=len(pending), task_ids=[t.id for _, t in pending])
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(context: ServerCallContext, task: Task) -> None:
        async with gate:
            try:
                await _reconcile_one(store, executor, context, task)
                logger.info("coordinator_reconciled", task_id=task.id)
            except Exception as exc:
                logger.warning("coordinator_reconcile_failed", task_id=task.id, error=str(exc)[:300])

    await asyncio.gather(*(one(context, task) for context, task in pending))
