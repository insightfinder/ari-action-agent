"""Registry of every change this agent can propose. See `base.py`."""

from .base import ActionSpec, Operation, target_of
from .cronjob import ResumeCronJob, SuspendCronJob
from .delete_pod import DeletePod
from .node import CordonNode, DrainNode, UncordonNode
from .pause_rollout import PauseRollout, ResumeRollout
from .resize_pod import ResizePod
from .restart_rollout import RestartRollout
from .rollback import Rollback
from .scale_replicas import ScaleReplicas
from .set_autoscaler_bounds import SetAutoscalerBounds
from .set_image import SetImage
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
        Rollback(),
        DeletePod(),
        SetImage(),
        PauseRollout(),
        ResumeRollout(),
        SuspendCronJob(),
        ResumeCronJob(),
        CordonNode(),
        UncordonNode(),
        DrainNode(),
    )
}
ACTIONS = tuple(REGISTRY)


def get(name: str | None) -> ActionSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown action {name!r}") from None


__all__ = ["ACTIONS", "REGISTRY", "ActionSpec", "Operation", "get", "target_of"]
