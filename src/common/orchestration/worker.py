"""Generic Temporal worker core — registers the shared workflow + generic
activities and polls `settings.temporal_task_queue`. Agent-kind-agnostic:
which capabilities/tools actually run is entirely driven by
`settings.agent_kind` (via the registries), not anything here. The actual
deployable entrypoint is `api/worker.py` (`python -m api.worker`), which
just calls `main()` below."""

import asyncio

import structlog
from temporalio.worker import Worker

from common.settings import get_settings
from .activities import (
    cleanup_workspace,
    execute_step,
    plan_task,
    prepare_workspace,
    resolve_artifacts,
)
from .temporal_client import get_temporal_client
from .workflow import AriAgentWorkflow

logger = structlog.get_logger()


async def main() -> None:
    settings = get_settings()
    client = await get_temporal_client()

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[AriAgentWorkflow],
        activities=[prepare_workspace, plan_task, execute_step, cleanup_workspace, resolve_artifacts],
    )
    logger.info("worker_starting", task_queue=settings.temporal_task_queue, namespace=settings.temporal_namespace)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
