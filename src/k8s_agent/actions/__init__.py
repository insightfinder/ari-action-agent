"""Registry of every change this agent can propose. See `base.py`."""

from .base import ActionSpec, Operation, target_of
from .resize_pod import ResizePod
from .scale_replicas import ScaleReplicas
from .set_autoscaler_bounds import SetAutoscalerBounds
from .set_resources import SetResources

REGISTRY: dict[str, ActionSpec] = {spec.name: spec for spec in (ScaleReplicas(), SetAutoscalerBounds(), SetResources(), ResizePod())}
ACTIONS = tuple(REGISTRY)


def get(name: str | None) -> ActionSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown action {name!r}") from None


__all__ = ["ACTIONS", "REGISTRY", "ActionSpec", "Operation", "get", "target_of"]
