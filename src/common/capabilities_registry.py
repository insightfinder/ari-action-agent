"""Loads configs/capabilities.yaml — the single source of truth for which
capabilities exist, what they do (for the planner's prompt), which tools
they're allowed to call, and their retry/on_failure policy (for the
executor). Adding a capability means adding a module + an entry here;
nothing in orchestration/ changes.
"""

from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module
from typing import Any, Callable, Optional

import yaml

from common.config_paths import config_file
from common.models.plan import OnFailure
from common.capabilities_base import StepContext
from common.tools_registry import get_tools


@dataclass
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: int = 0


@dataclass
class CapabilitySpec:
    name: str
    module: str
    description: str
    tools: list[str] = field(default_factory=list)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    on_failure: Optional[OnFailure] = None
    # Internal capabilities (e.g. "respond") are run directly by the
    # workflow, never chosen by the planner — excluded from catalog() so
    # the planner's system prompt doesn't see them as an optional step.
    internal: bool = False

    def load_run(self) -> Callable[[StepContext], Any]:
        mod = import_module(self.module)
        return getattr(mod, "run")


@lru_cache
def _load_specs() -> dict[str, CapabilitySpec]:
    raw = yaml.safe_load(config_file("capabilities.yaml").read_text())
    specs: dict[str, CapabilitySpec] = {}
    for name, entry in raw.get("capabilities", {}).items():
        retry = RetryPolicy(**entry.get("retry", {}))
        on_failure = OnFailure(**entry["on_failure"]) if entry.get("on_failure") else None
        specs[name] = CapabilitySpec(
            name=name,
            module=entry["module"],
            description=entry["description"].strip(),
            tools=entry.get("tools", []),
            retry=retry,
            on_failure=on_failure,
            internal=entry.get("internal", False),
        )
    return specs


def get_capability(name: str) -> CapabilitySpec:
    specs = _load_specs()
    if name not in specs:
        raise KeyError(f"Unknown capability: {name!r}. Registered: {sorted(specs)}")
    return specs[name]


def catalog() -> list[dict[str, Any]]:
    """Capability descriptions for the planner's system prompt. Excludes
    `internal` capabilities (e.g. `respond`) — those are run directly by
    the workflow, never selected by the planner."""
    return [
        {"name": s.name, "description": s.description, "tools": s.tools}
        for s in _load_specs().values()
        if not s.internal
    ]


async def run_capability(name: str, ctx: StepContext) -> dict:
    spec = get_capability(name)
    ctx.tools = get_tools(spec.tools)
    run_fn = spec.load_run()
    return await run_fn(ctx)
