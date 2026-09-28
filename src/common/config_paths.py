"""Locates the project's configs/ directory.

Both registries used to resolve it as `Path(__file__).resolve().parents[3] /
"configs"`, which is the repo root only when running straight from the source
tree. The Dockerfile runs `pip install .`, so in the image the package sits in
site-packages and parents[3] is `/usr/local/lib/python3.<minor>` -- while the
Dockerfile's `COPY configs ./configs` under `WORKDIR /app` puts the files at
/app/configs. Nothing bridged the two, so every container run failed the first
activity with

    FileNotFoundError: '/usr/local/lib/python3.12/configs/capabilities.yaml'

The API process survived (it never loads a registry at startup) but no workflow
could complete. Resolution is now by search rather than by a fixed hop count,
so the same wheel works from a source checkout, from an editable install, and
from the image.
"""

import os
from pathlib import Path

_ENV_VAR = "ARI_CONFIGS_DIR"


def _candidates() -> list[Path]:
    found: list[Path] = []
    override = os.getenv(_ENV_VAR)
    if override:
        found.append(Path(override))
    # Source tree / editable install: src/common/config_paths.py -> repo root.
    found.append(Path(__file__).resolve().parents[2] / "configs")
    # Image: WORKDIR is /app and `COPY configs ./configs` lands there. Use the
    # working directory rather than hard-coding /app so a different WORKDIR
    # still resolves.
    found.append(Path.cwd() / "configs")
    found.append(Path("/app/configs"))
    return found


def configs_dir() -> Path:
    """First candidate directory that exists; raises with all of them if none do."""
    tried = _candidates()
    for path in tried:
        if path.is_dir():
            return path
    raise FileNotFoundError(
        "could not locate the configs/ directory. Looked in: "
        + ", ".join(str(p) for p in tried)
        + f". Set {_ENV_VAR} to point at it explicitly."
    )


def config_file(name: str, agent_kind: str | None = None) -> Path:
    """Path to a named file inside configs/<agent_kind>/ (e.g.
    "capabilities.yaml") — every agent kind resolves the same way, under
    its own subdirectory, so two agent kinds' registries can never
    collide with or silently reuse each other's YAML. `agent_kind`
    defaults to `settings.agent_kind` — imported lazily to avoid a
    module-load-order cycle with settings.py.
    """
    if agent_kind is None:
        from .settings import get_settings

        agent_kind = get_settings().agent_kind
    return configs_dir() / agent_kind / name
