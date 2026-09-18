"""Shared shape between the planner (produces a Plan) and the executor
(runs one). Deliberately generic — `params` and `on_failure` are per-step
free-form so new capabilities never need a schema change here."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class OnFailure(BaseModel):
    goto: str
    max_attempts: int = 3
    then: str = "fail_needs_human"


class Step(BaseModel):
    capability: str
    params: dict[str, Any] = Field(default_factory=dict)
    # Per-step override of the capability's default on_failure from
    # capabilities.yaml, if the planner decides this run needs different
    # looping behavior. Usually omitted — the registry default applies.
    on_failure: Optional[OnFailure] = None


class Plan(BaseModel):
    steps: list[Step]
    rationale: str = ""
