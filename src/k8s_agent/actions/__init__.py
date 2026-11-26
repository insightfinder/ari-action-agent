"""Registry of every change this agent can propose. See `base.py`.

Removed by decision (2026-11-26, nothing that could break a product environment): rollback, set_image,
pause/resume_rollout, suspend/resume_cronjob, and every change to a StatefulSet or DaemonSet. See the "Never"
list in docs/K8S_AGENT_INTERFACE.md.

`expand_pvc` (2026-11-26) is the one change not on a Deployment: it grows a StatefulSet's claim-template PVC.
"""

from .base import ActionSpec, Operation, target_of
from .delete_pod import DeletePod
from .expand_pvc import ExpandPvc
from .resize_pod import ResizePod
from .restart_rollout import RestartRollout
from .scale_replicas import ScaleReplicas
from .set_autoscaler_bounds import SetAutoscalerBounds
from .set_resources import SetResources

# Order is the order the extraction prompt lists them in; new actions go last so existing names keep their place.
REGISTRY: dict[str, ActionSpec] = {
    spec.name: spec
    for spec in (
        ScaleReplicas(),
        SetAutoscalerBounds(),
        SetResources(),
        ResizePod(),
        RestartRollout(),
        DeletePod(),
        ExpandPvc(),
    )
}
ACTIONS = tuple(REGISTRY)


def get(name: str | None) -> ActionSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown action {name!r}") from None


__all__ = ["ACTIONS", "REGISTRY", "ActionSpec", "Operation", "get", "target_of"]
