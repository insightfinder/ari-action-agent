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


ACTIVITIES = [prepare_workspace, plan_task, execute_step, cleanup_workspace, resolve_artifacts]


def build_workers(client, settings) -> list[Worker]:
    lanes = [(settings.temporal_task_queue, settings.max_concurrent_activities)]
    if settings.interactive_task_queue():
        lanes.append((settings.interactive_task_queue(), settings.interactive_max_concurrent_activities))
    return [
        Worker(
            client,
            task_queue=queue,
            workflows=[AriAgentWorkflow],
            activities=ACTIVITIES,
            max_concurrent_activities=limit,
        )
        for queue, limit in lanes
    ]


async def main() -> None:
    settings = get_settings()
    client = await get_temporal_client()
    workers = build_workers(client, settings)
    logger.info(
        "worker_starting",
        task_queue=settings.temporal_task_queue,
        namespace=settings.temporal_namespace,
        max_concurrent_activities=settings.max_concurrent_activities,
        interactive_task_queue=settings.interactive_task_queue() or None,
        interactive_max_concurrent_activities=settings.interactive_max_concurrent_activities,
    )
    await asyncio.gather(*(worker.run() for worker in workers))


if __name__ == "__main__":
    asyncio.run(main())
