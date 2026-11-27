"""Documents what this kind expects in a request's config DataPart.

Like the other kinds' schemas, nothing validates against this at runtime —
capabilities read `ctx.config` as a plain dict — but it is the one place
the caller-facing shape is written down. There is deliberately no cluster
credential here: the agent acts with its own in-cluster ServiceAccount.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from common.models.trace_config import TraceConfig


class LLMConfig(BaseModel):
    provider: Literal["openai", "anthropic", "moonshotai"]
    api_key: str
    model: str
    base_url: Optional[str] = None


class K8sOptions(BaseModel):
    namespace: Optional[str] = Field(None, description="Default namespace when the request names none.")
    high_replica_threshold: int = Field(
        30, description="Replica targets above this are flagged as the highest risk tier in the proposal."
    )
    node_headroom_ratio: float = Field(
        0.2, description="CPU/memory caps use at most (1 - ratio) of the best node's allocatable capacity."
    )
    include_logs: bool = Field(True, description="Attach redacted previous-container log tails to crash findings.")
    proposal_max_age_hours: float = Field(
        24,
        description=(
            "An approval of a proposal older than this is refused (nothing is changed; ask again). Read from the "
            "approving request's config. 0 turns the limit off."
        ),
    )
    pipeline_budget_seconds: Optional[float] = None


class ConfirmPendingAction(BaseModel):
    id: str


class K8sRequestConfig(BaseModel):
    llm: LLMConfig
    options: K8sOptions = K8sOptions()
    confirm_pending_action: Optional[ConfirmPendingAction] = None
    trace: Optional[TraceConfig] = None
