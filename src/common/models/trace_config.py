"""The optional `trace` block of a request's config DataPart, shared by every
agent kind. Documentation only, like the kinds' own schemas: common/tracing.py
reads the block as a plain dict."""

from typing import Optional

from pydantic import BaseModel, ConfigDict


class TraceConfig(BaseModel):
    """Where this request's spans go — the caller's own InsightFinder trace
    project, and the span to hang them under. Without it the request is not
    traced. Field names are mcp-service's ChatDto ones."""

    traceparent: Optional[str] = None  # W3C, the caller's span for this call
    trace_project: str
    trace_admin_user: str
    trace_admin_user_license: str
    trace_system: Optional[str] = None
    session_id: Optional[str] = None
    username: Optional[str] = None  # the end user, shown as chat.username

    model_config = ConfigDict(extra="allow")
