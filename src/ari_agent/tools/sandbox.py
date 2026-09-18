"""Per-request ephemeral workspace management. One sandbox directory per
request_id; a failing/hung request can't corrupt another's state because
they never share a directory."""

import shutil
from pathlib import Path

from ..settings import get_settings


def create_workspace(request_id: str) -> str:
    root = Path(get_settings().sandbox_root)
    workspace = root / request_id
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


def cleanup_workspace(request_id: str) -> None:
    root = Path(get_settings().sandbox_root)
    workspace = root / request_id
    shutil.rmtree(workspace, ignore_errors=True)
