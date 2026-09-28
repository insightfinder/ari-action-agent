"""FastAPI/A2A entrypoint. `uvicorn api.main:app`.

Deliberately thin: everything real lives in common/app_factory.py, which
is agent-kind-agnostic (driven by `settings.agent_kind`/`agent_name`/
`agent_public_url`, all ordinary per-deployment config). This module is
just the fixed target Docker/Helm point uvicorn at.
"""

from common.app_factory import create_app

app = create_app()
