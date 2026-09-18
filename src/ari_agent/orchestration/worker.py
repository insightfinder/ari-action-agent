"""Temporal worker entrypoint — registers the workflow + activities and
polls `settings.temporal_task_queue`. Run with `scripts/run_worker.sh` (or
`python -m ari_agent.orchestration.worker`)."""

import asyncio

import structlog
from temporalio.worker import Worker

from ..settings import get_settings
from .activities import (
    cleanup_workspace,
    execute_step,
    plan_task,
    prepare_workspace,
)
from .temporal_client import get_temporal_client
from .workflow import AriOnCallWorkflow

logger = structlog.get_logger()


async def main() -> None:
    settings = get_settings()
    client = await get_temporal_client()

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[AriOnCallWorkflow],
        activities=[prepare_workspace, plan_task, execute_step, cleanup_workspace],
    )
    logger.info("worker_starting", task_queue=settings.temporal_task_queue, namespace=settings.temporal_namespace)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
