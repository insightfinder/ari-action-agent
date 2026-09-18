"""
Internal config models — used to validate the `RequestConfig`-shaped
dict a caller sends as a `DataPart` alongside the prompt's `TextPart` (see
`a2a_server/executor.py:_extract_config`). These are no longer the wire
schema themselves (the real A2A `Message`/`Part`/`Task` types from
`a2a.types` are); they're just what we expect *inside* one DataPart.

Design note unchanged from before: every setting a request needs (LLM
key/model, repo credentials, prompt overrides, ad-hoc options like "skip
tests") travels inline in `RequestConfig` — there is no server-side secret
store or env-var indirection to look values up from, because this agent
is stateless and each request is independent. `RequestConfig` allows
extra fields so new config sections (a future capability's own settings)
don't require a schema change here. There is deliberately no `callback`
field anymore — that's now the A2A protocol's own concern
(`configuration.task_push_notification_config` on the request, or a
separate `tasks/pushNotificationConfig/set` call), not something this
agent's own config object needs to carry.
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class RepoConfig(BaseModel):
    provider: Literal["github", "gitlab", "generic-git"] = "github"
    url: str
    # None (not "main") when the caller doesn't name a branch — the remote
    # doesn't necessarily use "main" (e.g. "master", or something else
    # entirely), so clone_repo resolves it dynamically from the remote
    # itself rather than this model assuming a fixed name.
    ref: Optional[str] = None
    # Credentials are inlined in the request payload (not env-var refs) —
    # this agent has nowhere else to keep them and never persists a request.
    pat: Optional[str] = None
    ssh_key: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class LLMConfig(BaseModel):
    provider: str  # e.g. "openai", "anthropic"
    api_key: str
    model: str
    base_url: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class RequestConfig(BaseModel):
    repo: RepoConfig
    llm: LLMConfig
    system_prompt: Optional[str] = None  # optional override of the default planner/executor system prompt
    # Free-form dynamic directives the planner takes into account, e.g.
    # {"skip_tests": true}. Deliberately not enumerated — new directives
    # are a planner-prompt change, not a schema change.
    options: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


class RootCause(BaseModel):
    summary: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class PullRequestRef(BaseModel):
    url: str
    branch: str
