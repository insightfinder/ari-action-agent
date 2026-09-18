"""Temporal worker entrypoint. `python -m api.worker`.

Deliberately thin: the real worker core (`common.orchestration.worker.main`)
is agent-kind-agnostic already — which capabilities/tools it actually runs
is driven entirely by `settings.agent_kind`/`temporal_task_queue`, ordinary
per-deployment config, not anything here.
"""

import asyncio

from common.orchestration.worker import main

if __name__ == "__main__":
    asyncio.run(main())
