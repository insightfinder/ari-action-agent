"""OpenTelemetry spans for one request, sent to the caller's InsightFinder
trace project.

The caller decides where a trace goes, never this deployment. ARI (the
mcp-service coordinator) resolves a per-organization trace project at
handshake time and records its own `agent:<name>` span around the call to
this agent; it hands both to us as the request's optional `trace` block (see
docs/DEVELOPER_GUIDE.md §5):

    {"traceparent": "00-<trace id>-<span id>-01",
     "trace_project": ..., "trace_system": ...,
     "trace_admin_user": ..., "trace_admin_user_license": ...,
     "session_id": ..., "username": ...}

The InsightFinder trace server (OpenTelemetryService-Trace) routes every span
by its `x-username` / `x-trace-project` / `x-licensekey` attributes, so each
span this module starts carries them. That is also why one process-wide
TracerProvider is enough: unlike mcp-service's per-identity provider cache,
nothing here depends on resource attributes.

Everything is a no-op unless `TRACE_SERVER_URL` is set *and* the request
carries a complete identity: the CLI, the chat gateway and the tests send no
`trace` block and run exactly as before. Tracing never fails a request — any
error inside this module is logged and swallowed.

Two attribute rules come from the trace server's own config on test, and
breaking either corrupts the caller's trace rather than this agent's:
- never set `chat.id` — the server overwrites the span id of whichever span
  carries it, which detaches its children;
- never set `chat.prompt`/`chat.response` — a span carrying them becomes a
  user prompt/response pair in the `-Prompt` project and is evaluated as one.
  LLM spans use the traceloop `gen_ai.*` keys, as mcp-service's do.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional

import structlog
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, Status, StatusCode, TraceFlags

from common.redaction import redact_secrets

logger = structlog.get_logger()

# Every free-text attribute (prompts, completions, tool input/output) is cut
# to this many characters. The trace server re-reads and re-sends a whole
# trace from Jaeger each time a late span arrives, and on test it ran out of
# heap (512Mi) on a trace whose LLM spans carried full tool catalogs.
MAX_TEXT = 4000

_TRACER_NAME = "ari-agent"
_provider = None
_current_target: contextvars.ContextVar[Optional["TraceTarget"]] = contextvars.ContextVar(
    "ari_trace_target", default=None
)


@dataclass(frozen=True)
class TraceTarget:
    """Whose trace project a request's spans belong to."""

    project: str
    user: str
    license_key: str
    system: str = ""
    session_id: str = ""
    username: str = ""
    traceparent: str = ""

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> Optional["TraceTarget"]:
        block = (config or {}).get("trace")
        if not isinstance(block, Mapping):
            return None
        project = block.get("trace_project")
        user = block.get("trace_admin_user")
        license_key = block.get("trace_admin_user_license")
        if not (project and user and license_key):
            return None
        return cls(
            project=str(project),
            user=str(user),
            license_key=str(license_key),
            system=str(block.get("trace_system") or ""),
            session_id=str(block.get("session_id") or ""),
            username=str(block.get("username") or ""),
            traceparent=str(block.get("traceparent") or ""),
        )

    def identity_attributes(self) -> dict[str, str]:
        # Same keys mcp-service's set_span_identity_attributes and its root
        # span use; the first three are what the trace server routes on.
        attrs = {
            "x-username": self.user,
            "x-licensekey": self.license_key,
            "x-trace-project": self.project,
        }
        if self.system:
            attrs["x-system-name"] = self.system
        if self.session_id:
            attrs["x-session-id"] = self.session_id
        if self.username:
            attrs["chat.username"] = self.username
        return attrs


# ─── Provider lifecycle ──────────────────────────────────────────────────


def init_tracing(service_name: str, endpoint: str) -> bool:
    """Installs the process-wide exporter. Returns whether tracing is on.

    `endpoint` is `TRACE_SERVER_URL`, an OTLP/gRPC address such as
    http://traceserver-llm-labs.traceserver.svc.cluster.local:4618. An
    `https://` address is exported over TLS; `http://` or no scheme, in
    plaintext. Empty leaves tracing off."""
    global _provider
    if _provider is not None:
        return True
    if not endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        tls = endpoint.startswith("https://")
        address = endpoint.removeprefix("https://").removeprefix("http://")
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=address, insecure=not tls)))
        _provider = provider
        logger.info("tracing_enabled", endpoint=endpoint, service_name=service_name)
        return True
    except Exception as exc:  # noqa: BLE001 - tracing must never stop the process
        logger.warning("tracing_init_failed", error=f"{type(exc).__name__}: {exc}")
        return False


def install_provider(provider) -> None:
    """Uses an already-built provider (tests: one with an in-memory exporter)."""
    global _provider
    _provider = provider


def shutdown_tracing() -> None:
    """Flushes buffered spans; called when the process stops."""
    global _provider
    provider, _provider = _provider, None
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracing_shutdown_failed", error=f"{type(exc).__name__}: {exc}")


def enabled() -> bool:
    return _provider is not None


def active() -> bool:
    """Whether a span started here would be recorded: an exporter is
    installed and a traced request is current. Lets a caller skip building
    attributes on the (default) untraced path."""
    return _provider is not None and _current_target.get() is not None


def target_for(config: Mapping[str, Any] | None) -> Optional[TraceTarget]:
    """The request's trace target, or None when this request is untraced."""
    if _provider is None:
        return None
    return TraceTarget.from_config(config)


# ─── W3C traceparent (same format as mcp-service's app/trace/Propagation.py) ─


def traceparent_of(span) -> Optional[str]:
    if span is None:
        return None
    ctx = span.get_span_context()
    if not ctx or not ctx.trace_id or not ctx.span_id:
        return None
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"


def remote_context(traceparent: Optional[str]):
    """An OTel context whose parent is the span `traceparent` names, or None."""
    if not traceparent:
        return None
    parts = traceparent.split("-")
    if len(parts) != 4:
        return None
    _, trace_id, span_id, flags = parts
    try:
        ctx = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(int(flags, 16)),
        )
    except (ValueError, TypeError):
        return None
    if not ctx.trace_id or not ctx.span_id:
        return None
    return trace.set_span_in_context(NonRecordingSpan(ctx))


# ─── Spans ───────────────────────────────────────────────────────────────


def truncate(text: Any, limit: int = MAX_TEXT) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


def start_span(
    name: str,
    *,
    target: Optional[TraceTarget] = None,
    parent=None,
    traceparent: Optional[str] = None,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Optional[Mapping[str, Any]] = None,
    start_time: Optional[int] = None,
):
    """Starts (but does not activate) a span; the caller must `.end()` it.

    For spans recorded after the fact with real timestamps (opencode's own
    event times). `parent` is a span; else `traceparent`; else the current
    context. `target` defaults to the request currently being traced.
    Returns None when untraced."""
    target = target or _current_target.get()
    if _provider is None or target is None:
        return None
    try:
        if parent is not None:
            ctx = trace.set_span_in_context(parent)
        elif traceparent:
            ctx = remote_context(traceparent)
        else:
            ctx = None
        span = _provider.get_tracer(_TRACER_NAME).start_span(
            name, context=ctx, kind=kind, start_time=start_time
        )
        span.set_attributes(target.identity_attributes())
        if attributes:
            set_attributes(span, attributes)
        return span
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracing_span_failed", span=name, error=f"{type(exc).__name__}: {exc}")
        return None


@contextmanager
def span(
    name: str,
    *,
    target: Optional[TraceTarget] = None,
    traceparent: Optional[str] = None,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Optional[Mapping[str, Any]] = None,
) -> Iterator[Any]:
    """A span that is current for the `with` body, so LLM and opencode spans
    started inside it become its children. Yields None when untraced.

    An exception from the body marks the span as failed and propagates
    unchanged."""
    target = target or _current_target.get()
    current = start_span(name, target=target, traceparent=traceparent, kind=kind, attributes=attributes)
    if current is None:
        yield None
        return
    target_token = _current_target.set(target)
    ctx_token = otel_context.attach(trace.set_span_in_context(current))
    try:
        yield current
    except BaseException as exc:
        mark_failed(current, exc)
        raise
    finally:
        otel_context.detach(ctx_token)
        _current_target.reset(target_token)
        try:
            current.end()
        except Exception:  # noqa: BLE001
            pass


def set_attributes(span, attributes: Mapping[str, Any]) -> None:
    """Sets attributes, skipping None values. Safe on a None span."""
    if span is None:
        return
    try:
        for key, value in attributes.items():
            if value is None:
                continue
            if not isinstance(value, (str, bool, int, float)):
                value = str(value)
            span.set_attribute(key, value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracing_attributes_failed", error=f"{type(exc).__name__}: {exc}")


def mark_failed(span, exc: BaseException | str) -> None:
    if span is None:
        return
    message = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    try:
        # A provider's or a command's error text can quote a key or an
        # authenticated URL, and the trace server stores what it is sent.
        message = redact_secrets(message)
        span.set_status(Status(StatusCode.ERROR, truncate(message, 500)))
        # The trace server's `error` mapping reads this tag.
        span.set_attribute("error", True)
        span.set_attribute("error.message", truncate(message, 1000))
    except Exception:  # noqa: BLE001
        pass


def set_token_attributes(span, input_tokens, output_tokens, cache_read_tokens=0,
                         cache_creation_tokens=0, model=None) -> None:
    """Token attributes for the AI WatchTower trace-costs dashboard, with the
    exact keys mcp-service's `set_span_token_attributes` emits.

    `input_tokens` is the raw input total, cache reads and writes included;
    the cache_* breakdown lets the cost backend price each bucket. Set only
    on spans that are one model call, so nothing is counted twice."""
    if span is None:
        return
    try:
        _set_token_attributes(span, input_tokens, output_tokens, cache_read_tokens,
                              cache_creation_tokens, model)
    except Exception as exc:  # noqa: BLE001 - a malformed usage must not fail the call
        logger.warning("tracing_tokens_failed", error=f"{type(exc).__name__}: {exc}")


def _set_token_attributes(span, input_tokens, output_tokens, cache_read_tokens,
                          cache_creation_tokens, model) -> None:
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    cache_read_tokens = int(cache_read_tokens or 0)
    cache_creation_tokens = int(cache_creation_tokens or 0)
    total = input_tokens + output_tokens
    set_attributes(span, {
        "chat.prompt_tokens": input_tokens,
        "chat.response_tokens": output_tokens,
        "chat.completion_tokens": output_tokens,
        "chat.total_tokens": total,
        "chat.cache_read_tokens": cache_read_tokens,
        "chat.cache_creation_tokens": cache_creation_tokens,
        "chat.uncached_prompt_tokens": max(input_tokens - cache_read_tokens - cache_creation_tokens, 0),
        "chat.model": model,
        "gen_ai.usage.prompt_tokens": input_tokens,
        "gen_ai.usage.completion_tokens": output_tokens,
        "gen_ai.usage.cache_read_input_tokens": cache_read_tokens,
        "llm.usage.total_tokens": total,
    })
    if input_tokens:
        span.set_attribute("prompt_tokens", input_tokens)
    if output_tokens:
        span.set_attribute("response_tokens", output_tokens)
    if total:
        span.set_attribute("total_tokens", total)
