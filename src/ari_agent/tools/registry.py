"""Loads configs/tools.yaml and resolves tool names to callables.

A tool function's signature is `async def fn(ctx: StepContext, **params) -> dict`.
"""

from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import Callable

import yaml

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "tools.yaml"


@dataclass
class ToolSpec:
    name: str
    module: str
    function: str

    def load(self) -> Callable:
        mod = import_module(self.module)
        return getattr(mod, self.function)


@lru_cache
def _load_specs() -> dict[str, ToolSpec]:
    raw = yaml.safe_load(_CONFIG_PATH.read_text())
    return {
        name: ToolSpec(name=name, module=entry["module"], function=entry["function"])
        for name, entry in raw.get("tools", {}).items()
    }


def get_tool(name: str) -> Callable:
    specs = _load_specs()
    if name not in specs:
        raise KeyError(f"Unknown tool: {name!r}. Registered: {sorted(specs)}")
    return specs[name].load()


def get_tools(names: list[str]) -> dict[str, Callable]:
    return {name: get_tool(name) for name in names}
