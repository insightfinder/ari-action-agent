"""Spans sent to the caller's InsightFinder trace project (common/tracing.py).

Each test installs an in-memory exporter, so these check exactly what the
trace server would receive: the routing identity on every span, the parent
chain back to the caller's span, and the attribute rules the trace server's
config imposes."""

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a2a.types import Message, Part, Role, TaskState  # noqa: E402

import common.a2a_server.executor as executor  # noqa: E402
import common.orchestration.activities as activities  # noqa: E402
from common import tracing  # noqa: E402
from common.capabilities_base import StepContext  # noqa: E402
from common.llm import client as llm_client  # noqa: E402
from common.models.plan import Plan, Step  # noqa: E402
from common.opencode_runtime import record_spans  # noqa: E402

CALLER_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_SPAN_ID = "00f067aa0ba902b7"
CALLER_TRACEPARENT = f"00-{CALLER_TRACE_ID}-{CALLER_SPAN_ID}-01"

TRACE_BLOCK = {
    "traceparent": CALLER_TRACEPARENT,
    "trace_project": "acme-MCP-Anthropic-claude-x-claude-x-LLM",
    "trace_system": "acme-MCP-Anthropic-claude-x-claude-x",
    "trace_admin_user": "acme-admin",
    "trace_admin_user_license": "if-license-test",
    "session_id": "session-1",
    "username": "alice",
}

IDENTITY = {
    "x-username": "acme-admin",
    "x-licensekey": "if-license-test",
    "x-trace-project": "acme-MCP-Anthropic-claude-x-claude-x-LLM",
    "x-system-name": "acme-MCP-Anthropic-claude-x-claude-x",
    "x-session-id": "session-1",
    "chat.username": "alice",
}

# Keys the InsightFinder trace server treats specially; see tracing.py.
FORBIDDEN_KEYS = {"chat.id", "chat.prompt", "chat.response"}


@pytest.fixture
def exporter():
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracing.install_provider(provider)
    yield memory
    tracing.install_provider(None)


def _by_name(spans, name):
    matches = [s for s in spans if s.name == name]
    assert matches, f"no span named {name!r} in {[s.name for s in spans]}"
    return matches[0]


def _traced_config(**overrides):
    return {
        "llm": {"provider": "anthropic", "api_key": "sk-ant-test", "model": "claude-x"},
        "trace": {**TRACE_BLOCK, **overrides},
    }


def _assert_routable(span):
    for key, value in IDENTITY.items():
        assert span.attributes.get(key) == value, f"{span.name} lacks {key}"
    assert not FORBIDDEN_KEYS & set(span.attributes), f"{span.name} sets {FORBIDDEN_KEYS & set(span.attributes)}"


# ─── When nothing is traced ─────────────────────────────────────────────


def test_no_exporter_means_no_target_even_with_a_trace_block():
    tracing.install_provider(None)
    assert tracing.target_for(_traced_config()) is None


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"trace": None},
        {"trace": "not a block"},
        {"trace": {**TRACE_BLOCK, "trace_admin_user_license": ""}},
        {"trace": {k: v for k, v in TRACE_BLOCK.items() if k != "trace_project"}},
    ],
)
def test_a_request_without_a_complete_identity_is_not_traced(exporter, config):
    # The trace server drops a span it cannot route; sending one would only
    # cost the caller's trace a broken branch.
    assert tracing.target_for(config) is None
    with tracing.span("anything") as span:
        assert span is None
    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize("endpoint, address, insecure", [
    ("http://traceserver:4618", "traceserver:4618", True),
    ("traceserver:4618", "traceserver:4618", True),
    ("https://traceserver.example:4618", "traceserver.example:4618", False),
])
def test_an_https_endpoint_is_exported_over_tls(monkeypatch, endpoint, address, insecure):
    import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as otlp

    seen = {}
    monkeypatch.setattr(otlp, "OTLPSpanExporter", lambda **kw: seen.update(kw) or InMemorySpanExporter())
    tracing.install_provider(None)
    try:
        assert tracing.init_tracing("ARI On-Call Agent", endpoint) is True
    finally:
        tracing.shutdown_tracing()
    assert seen == {"endpoint": address, "insecure": insecure}


def test_init_tracing_without_an_endpoint_leaves_tracing_off():
    tracing.install_provider(None)
    assert tracing.init_tracing("ARI On-Call Agent", "") is False
    assert not tracing.enabled()


# ─── Identity, parenting, failures ──────────────────────────────────────


def test_a_span_carries_the_routing_identity_and_hangs_under_the_callers_span(exporter):
    target = tracing.target_for(_traced_config())

    with tracing.span("a2a-task:oncall", target=target, traceparent=target.traceparent):
        pass

    [span] = exporter.get_finished_spans()
    _assert_routable(span)
    assert format(span.context.trace_id, "032x") == CALLER_TRACE_ID
    assert format(span.parent.span_id, "016x") == CALLER_SPAN_ID


def test_children_inherit_the_target_and_parent_from_the_enclosing_span(exporter):
    target = tracing.target_for(_traced_config())

    with tracing.span("outer", target=target, traceparent=target.traceparent) as outer:
        with tracing.span("inner"):
            pass

    inner = _by_name(exporter.get_finished_spans(), "inner")
    _assert_routable(inner)
    assert inner.parent.span_id == outer.get_span_context().span_id


def test_an_exception_marks_the_span_failed_and_still_propagates(exporter):
    target = tracing.target_for(_traced_config())

    with pytest.raises(RuntimeError, match="boom"):
        with tracing.span("step", target=target):
            raise RuntimeError("boom")

    [span] = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["error"] is True
    assert "boom" in span.attributes["error.message"]


def test_traceparent_round_trips():
    ctx = tracing.remote_context(CALLER_TRACEPARENT)
    from opentelemetry import trace

    span = trace.get_current_span(ctx)
    assert tracing.traceparent_of(span) == CALLER_TRACEPARENT


@pytest.mark.parametrize("bad", ["", "garbage", "00-xyz-abc-01", f"00-{'0' * 32}-{CALLER_SPAN_ID}-01"])
def test_a_malformed_traceparent_gives_no_parent(bad):
    assert tracing.remote_context(bad) is None


def test_long_text_is_cut():
    assert tracing.truncate("x" * 10, limit=4) == "xxxx... [6 more chars]"
    assert tracing.truncate(None) == ""


# ─── LLM calls ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_anthropic_call_records_raw_input_tokens_including_the_cache(exporter, monkeypatch):
    usage = SimpleNamespace(input_tokens=100, output_tokens=40, cache_read_input_tokens=900,
                            cache_creation_input_tokens=50)
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="the plan")], usage=usage,
                               model="claude-x-2026")
    fake = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    monkeypatch.setattr(llm_client, "AsyncAnthropic", lambda **kw: fake)
    config = _traced_config()

    with tracing.span("plan_task", target=tracing.target_for(config)):
        text = await llm_client.complete(config["llm"], "system prompt", "user prompt")

    assert text == "the plan"
    span = _by_name(exporter.get_finished_spans(), "llm:anthropic/claude-x")
    _assert_routable(span)
    assert span.kind.name == "CLIENT"
    assert span.attributes["gen_ai.operation.name"] == "chat"
    assert span.attributes["gen_ai.request.model"] == "claude-x"
    assert span.attributes["gen_ai.prompt.1.content"] == "user prompt"
    assert span.attributes["gen_ai.completion.0.content"] == "the plan"
    # Same convention as mcp-service: raw input total, then the breakdown.
    assert span.attributes["chat.prompt_tokens"] == 1050
    assert span.attributes["chat.cache_read_tokens"] == 900
    assert span.attributes["chat.cache_creation_tokens"] == 50
    assert span.attributes["chat.uncached_prompt_tokens"] == 100
    assert span.attributes["chat.response_tokens"] == 40
    assert span.attributes["chat.total_tokens"] == 1090
    # No API key anywhere on the span.
    assert "sk-ant-test" not in str(dict(span.attributes))


@pytest.mark.asyncio
async def test_an_openai_call_records_its_tokens(exporter, monkeypatch):
    usage = SimpleNamespace(input_tokens=300, output_tokens=20,
                            input_tokens_details=SimpleNamespace(cached_tokens=200))
    response = SimpleNamespace(output_text="ok", usage=usage, model="gpt-x")
    fake = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    monkeypatch.setattr(llm_client, "AsyncOpenAI", lambda **kw: fake)
    config = {**_traced_config(), "llm": {"provider": "openai", "api_key": "sk-test", "model": "gpt-x"}}

    with tracing.span("respond", target=tracing.target_for(config)):
        await llm_client.complete(config["llm"], "s", "u")

    span = _by_name(exporter.get_finished_spans(), "llm:openai/gpt-x")
    assert span.attributes["chat.prompt_tokens"] == 300
    assert span.attributes["chat.cache_read_tokens"] == 200
    assert span.attributes["chat.response_tokens"] == 20


@pytest.mark.asyncio
async def test_a_failed_llm_call_records_only_the_key_free_error(exporter, monkeypatch):
    # The span sees the reworded error, never the provider's text that quotes the key.
    import httpx
    import openai

    response = httpx.Response(401, request=httpx.Request("POST", "https://llm.example.com/v1"))
    exc = openai.AuthenticationError("Incorrect API key provided: sk-proj-****abcd", response=response, body=None)
    fake = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=exc)))
    monkeypatch.setattr(llm_client, "AsyncOpenAI", lambda **kw: fake)
    config = {**_traced_config(), "llm": {"provider": "openai", "api_key": "sk-proj-abcd", "model": "m"}}

    with pytest.raises(llm_client.LLMProviderError):
        with tracing.span("plan_task", target=tracing.target_for(config)):
            await llm_client.complete(config["llm"], "s", "u")

    span = _by_name(exporter.get_finished_spans(), "llm:openai/m")
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["error.message"] == "LLMProviderError: openai rejected the API key (HTTP 401)"
    assert "abcd" not in str(dict(span.attributes)) + str(span.status.description)


@pytest.mark.asyncio
async def test_an_untraced_llm_call_still_works(exporter, monkeypatch):
    response = SimpleNamespace(output_text="ok", usage=None, model=None)
    fake = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    monkeypatch.setattr(llm_client, "AsyncOpenAI", lambda **kw: fake)

    assert await llm_client.complete({"provider": "openai", "api_key": "k", "model": "m"}, "s", "u") == "ok"
    assert exporter.get_finished_spans() == ()


# ─── opencode ───────────────────────────────────────────────────────────


def _ctx(config):
    return StepContext(request_id="req-1", workspace="/tmp/ws", prompt="fix it", config=config,
                       capability="generate_change", params={}, results={})


def _opencode_events():
    # opencode 1.18.31 `--format json`, trimmed to the fields read.
    return [
        {"type": "step_start", "timestamp": 1_000, "part": {"type": "step-start"}},
        {"type": "text", "timestamp": 1_100, "part": {"type": "text", "text": "Reading the file."}},
        {"type": "tool_use", "timestamp": 1_500, "part": {
            "type": "tool", "callID": "c1", "tool": "read",
            "state": {"status": "completed", "input": {"filePath": "a.py"}, "output": "print(1)",
                      "time": {"start": 1_200, "end": 1_500}}}},
        # A second update for the same call must not become a second span.
        {"type": "tool_use", "timestamp": 1_600, "part": {
            "type": "tool", "callID": "c1", "tool": "read",
            "state": {"status": "completed", "input": {"filePath": "a.py"}, "output": "print(1)",
                      "time": {"start": 1_200, "end": 1_500}}}},
        {"type": "tool_use", "timestamp": 1_800, "part": {
            "type": "tool", "callID": "c2", "tool": "bash",
            "state": {"status": "error", "input": {"command": "pytest"}, "error": "exit 1",
                      "time": {"start": 1_600, "end": 1_800}}}},
        {"type": "step_finish", "timestamp": 2_000, "part": {
            "type": "step-finish", "reason": "tool-calls", "cost": 0.0123,
            "tokens": {"input": 10, "output": 30, "reasoning": 5, "cache": {"read": 1000, "write": 200}}}},
        {"type": "step_start", "timestamp": 2_100, "part": {"type": "step-start"}},
        {"type": "text", "timestamp": 2_200, "part": {"type": "text", "text": "Done."}},
        {"type": "step_finish", "timestamp": 2_500, "part": {
            "type": "step-finish", "reason": "stop", "cost": 0.001,
            "tokens": {"input": 5, "output": 7, "reasoning": 0, "cache": {"read": 0, "write": 0}}}},
    ]


def test_an_opencode_call_becomes_steps_and_tools_timed_by_opencode(exporter):
    config = _traced_config()
    ctx = _ctx(config)

    with tracing.span("capability:generate_change", target=tracing.target_for(config)) as capability:
        record_spans(ctx, "ari-fix", _opencode_events(), 900_000_000, 3_000_000_000, None)

    spans = exporter.get_finished_spans()
    for span in spans:
        _assert_routable(span)

    call = _by_name(spans, "opencode:ari-fix")
    assert call.parent.span_id == capability.get_span_context().span_id
    assert (call.start_time, call.end_time) == (900_000_000, 3_000_000_000)
    assert call.attributes["opencode.steps"] == 2
    assert call.attributes["opencode.tool_call_total"] == 2
    assert "chat.prompt_tokens" not in call.attributes  # only per-call spans carry tokens

    steps = sorted((s for s in spans if s.name == "llm:anthropic/claude-x"), key=lambda s: s.start_time)
    assert len(steps) == 2
    first = steps[0]
    assert first.parent.span_id == call.context.span_id
    assert (first.start_time, first.end_time) == (1_000_000_000, 2_000_000_000)
    assert first.attributes["chat.prompt_tokens"] == 1210  # input + cache read + cache write
    assert first.attributes["chat.response_tokens"] == 35  # output + reasoning
    assert first.attributes["gen_ai.completion.0.content"] == "Reading the file."

    read = _by_name(spans, "tool:read")
    assert read.parent.span_id == first.context.span_id
    assert (read.start_time, read.end_time) == (1_200_000_000, 1_500_000_000)
    assert read.attributes["tool.status"] == "completed"
    assert read.attributes["tool.args"] == '{"filePath": "a.py"}'
    assert read.attributes["gen_ai.operation.name"] == "execute_tool"
    assert read.attributes["tool.result_size"] == len("print(1)")
    assert "tool.result" not in read.attributes  # repo contents never leave the agent
    assert len([s for s in spans if s.name == "tool:read"]) == 1

    bash = _by_name(spans, "tool:bash")
    assert bash.status.status_code.name == "ERROR"
    assert bash.attributes["error.message"] == "exit 1"


def test_tool_arguments_name_locations_but_never_carry_content(exporter):
    config = _traced_config()
    events = [
        {"type": "step_start", "timestamp": 1_000, "part": {"type": "step-start"}},
        {"type": "tool_use", "timestamp": 1_200, "part": {
            "type": "tool", "callID": "w1", "tool": "write",
            "state": {"status": "completed", "output": "",
                      "input": {"filePath": "src/app.py", "content": "SECRET = 'repo-contents'"},
                      "time": {"start": 1_100, "end": 1_200}}}},
        {"type": "tool_use", "timestamp": 1_400, "part": {
            "type": "tool", "callID": "b1", "tool": "bash",
            "state": {"status": "completed", "output": "ok",
                      "input": {"command": "curl -H 'Authorization: token ghp_x' ..."},
                      "time": {"start": 1_300, "end": 1_400}}}},
    ]
    with tracing.span("capability:generate_change", target=tracing.target_for(config)):
        record_spans(_ctx(config), "ari-change", events, 1, 2_000_000_000, None)

    spans = exporter.get_finished_spans()
    write = _by_name(spans, "tool:write")
    bash = _by_name(spans, "tool:bash")
    assert write.attributes["tool.args"] == '{"filePath": "src/app.py", "content": "<24 chars>"}'
    assert "ghp_x" not in bash.attributes["tool.args"]
    assert "repo-contents" not in str([dict(s.attributes) for s in spans])


def test_malformed_events_still_end_every_span(exporter):
    # A step-finish with an unexpected shape must neither raise nor strand its step span.
    config = _traced_config()
    events = [
        {"type": "step_start", "timestamp": 1_000, "part": {"type": "step-start"}},
        {"type": "step_finish", "timestamp": 2_000, "part": {
            "type": "step-finish", "cost": "free", "tokens": {"input": "many", "cache": ["x"]}}},
        {"type": "step_start", "timestamp": 2_100, "part": {"type": "step-start"}},
    ]
    with tracing.span("capability:verify", target=tracing.target_for(config)):
        record_spans(_ctx(config), "ari-verify", events, 1, 3_000_000_000, None)

    steps = [s for s in exporter.get_finished_spans() if s.name == "llm:anthropic/claude-x"]
    assert len(steps) == 2
    assert _by_name(exporter.get_finished_spans(), "opencode:ari-verify").attributes["opencode.steps"] == 2


def test_an_error_mid_step_still_ends_the_open_step(exporter, monkeypatch):
    config = _traced_config()
    events = [
        {"type": "step_start", "timestamp": 1_000, "part": {"type": "step-start"}},
        {"type": "step_finish", "timestamp": 2_000, "part": {"type": "step-finish", "tokens": {}}},
    ]
    monkeypatch.setattr(tracing, "set_token_attributes", MagicMock(side_effect=RuntimeError("boom")))
    with tracing.span("capability:verify", target=tracing.target_for(config)):
        record_spans(_ctx(config), "ari-verify", events, 1, 3_000_000_000, None)

    step = _by_name(exporter.get_finished_spans(), "llm:anthropic/claude-x")
    assert step.end_time == 3_000_000_000


@pytest.mark.asyncio
async def test_a_timed_out_opencode_call_still_leaves_a_failed_span(exporter, monkeypatch, tmp_path):
    from common import opencode_runtime

    class _Proc:
        returncode = None
        pid = 1

    async def spawn(*args, **kwargs):
        return _Proc()

    async def timeout(proc, agent, deadline):
        raise RuntimeError(f"opencode ({agent}) timed out after 1s (pid 1) and was killed")

    monkeypatch.setattr(opencode_runtime.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(opencode_runtime, "_communicate", timeout)
    config = _traced_config()
    ctx = StepContext(request_id="req-1", workspace=str(tmp_path), prompt="fix it", config=config,
                      capability="verify", params={}, results={})

    with pytest.raises(RuntimeError, match="timed out"):
        with tracing.span("capability:verify", target=tracing.target_for(config)):
            await opencode_runtime.run_opencode(ctx, "verify it", "ari-verify")

    call = _by_name(exporter.get_finished_spans(), "opencode:ari-verify")
    assert call.status.status_code.name == "ERROR"
    assert "timed out" in call.attributes["error.message"]


def test_a_failed_opencode_call_marks_its_span(exporter):
    config = _traced_config()
    with tracing.span("capability:verify", target=tracing.target_for(config)):
        record_spans(_ctx(config), "ari-verify", [], 1, 2, "Invalid API key")

    call = _by_name(exporter.get_finished_spans(), "opencode:ari-verify")
    assert call.status.status_code.name == "ERROR"


def test_a_tool_error_keeps_only_its_first_line(exporter):
    # The rest of a tool's error can quote the file or output it worked on.
    config = _traced_config()
    events = [
        {"type": "step_start", "timestamp": 1_000, "part": {"type": "step-start"}},
        {"type": "tool_use", "timestamp": 1_400, "part": {
            "type": "tool", "callID": "e1", "tool": "edit",
            "state": {"status": "error", "error": "oldString not found in a.py\nSECRET = 'repo-contents'",
                      "input": {"filePath": "a.py"}, "time": {"start": 1_300, "end": 1_400}}}},
    ]
    with tracing.span("capability:generate_change", target=tracing.target_for(config)):
        record_spans(_ctx(config), "ari-change", events, 1, 2_000_000_000, None)

    edit = _by_name(exporter.get_finished_spans(), "tool:edit")
    assert edit.attributes["error.message"] == "oldString not found in a.py"


def test_a_step_without_a_timestamp_stays_inside_its_call(exporter):
    config = _traced_config()
    events = [{"type": "step_start", "part": {"type": "step-start"}}]
    with tracing.span("capability:verify", target=tracing.target_for(config)):
        record_spans(_ctx(config), "ari-verify", events, 1_000, 3_000, None)

    step = _by_name(exporter.get_finished_spans(), "llm:anthropic/claude-x")
    assert (step.start_time, step.end_time) == (1_000, 3_000)


def test_failure_text_is_redacted_before_it_reaches_a_span(exporter):
    config = _traced_config()
    failure = ("Incorrect API key provided: sk-proj-****abcd; "
               "fatal: could not read from https://x-access-token:ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/a/b")
    with tracing.span("capability:verify", target=tracing.target_for(config)):
        record_spans(_ctx(config), "ari-verify", [], 1, 2, failure)

    message = _by_name(exporter.get_finished_spans(), "opencode:ari-verify").attributes["error.message"]
    assert "sk-proj" not in message and "ghp_" not in message and "x-access-token" not in message
    assert "https://[REDACTED]@github.com/a/b" in message


def test_a_malformed_usage_never_fails_the_call(exporter):
    with tracing.span("llm", target=tracing.target_for(_traced_config())) as span:
        tracing.set_token_attributes(span, "many", object())


def test_opencode_spans_need_an_enclosing_traced_step(exporter):
    record_spans(_ctx(_traced_config()), "ari-fix", _opencode_events(), 1, 2, None)
    assert exporter.get_finished_spans() == ()


# ─── Activities ─────────────────────────────────────────────────────────


class _Info:
    activity_type = "execute_step"
    attempt = 2
    workflow_id = "task-1"
    start_to_close_timeout = timedelta(seconds=60)


@pytest.mark.asyncio
async def test_each_step_attempt_is_a_span_under_the_task_span(exporter, monkeypatch, tmp_path):
    monkeypatch.setattr(activities.activity, "info", lambda: _Info())
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)

    async def run(name, ctx):
        return {"status": "failed", "failure": "tests failed"}

    monkeypatch.setattr(activities, "run_capability", run)
    payload = {"request_id": "task-1", "workspace": str(tmp_path), "prompt": "fix it",
               "config": _traced_config(), "capability": "verify", "params": {}, "results": {}}

    await activities.execute_step(payload)

    [span] = exporter.get_finished_spans()
    assert span.name == "capability:verify"
    _assert_routable(span)
    assert format(span.parent.span_id, "016x") == CALLER_SPAN_ID
    assert span.attributes["temporal.attempt"] == 2
    assert span.attributes["step.status"] == "failed"
    assert span.status.status_code.name == "ERROR"


@pytest.mark.asyncio
async def test_the_respond_step_records_the_outcome_under_the_task_span(exporter, monkeypatch, tmp_path):
    # Recorded by the worker, so a task the reconciler collects after an API
    # pod restart still gets its outcome.
    monkeypatch.setattr(activities.activity, "info", lambda: _Info())
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)

    async def run(name, ctx):
        return {"message": "Opened PR #7."}

    monkeypatch.setattr(activities, "run_capability", run)
    results = {"publish_pr": {"pr": {"url": "https://github.com/acme/app/pull/7", "branch": "ari/fix"}}}
    payload = {"request_id": "task-1", "workspace": str(tmp_path), "prompt": "fix it",
               "config": _traced_config(), "capability": "respond",
               "params": {"pipeline_status": "completed", "failure_reason": None},
               "results": results}

    await activities.execute_step(payload)

    result = _by_name(exporter.get_finished_spans(), "a2a-task-result")
    _assert_routable(result)
    assert format(result.parent.span_id, "016x") == CALLER_SPAN_ID
    assert result.attributes["agent.status"] == "completed"
    assert result.attributes["agent.result"] == "Opened PR #7."
    assert result.attributes["agent.artifact.pull_request.url"] == "https://github.com/acme/app/pull/7"


@pytest.mark.asyncio
async def test_a_failed_run_marks_its_outcome_failed(exporter, monkeypatch, tmp_path):
    monkeypatch.setattr(activities.activity, "info", lambda: _Info())
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)
    monkeypatch.setattr(activities, "run_capability", AsyncMock(return_value={"message": "It failed."}))
    payload = {"request_id": "task-1", "workspace": str(tmp_path), "prompt": "fix it",
               "config": _traced_config(), "capability": "respond",
               "params": {"pipeline_status": "failed", "failure_reason": "tests failed"}, "results": {}}

    await activities.execute_step(payload)

    result = _by_name(exporter.get_finished_spans(), "a2a-task-result")
    assert result.status.status_code.name == "ERROR"
    assert result.attributes["error.message"] == "tests failed"


class _LastRespondAttempt(_Info):
    retry_policy = SimpleNamespace(maximum_attempts=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("info, recorded", [(_LastRespondAttempt, True), (_Info, False)])
async def test_a_respond_step_out_of_attempts_still_records_the_outcome(
        exporter, monkeypatch, tmp_path, info, recorded):
    # The workflow then completes with its canned reply; nothing else would
    # record the outcome. An earlier attempt leaves it to the retry.
    monkeypatch.setattr(activities.activity, "info", lambda: info())
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)
    monkeypatch.setattr(activities, "run_capability",
                        AsyncMock(side_effect=RuntimeError("anthropic returned HTTP 529: overloaded")))
    payload = {"request_id": "task-1", "workspace": str(tmp_path), "prompt": "fix it",
               "config": _traced_config(), "capability": "respond",
               "params": {"pipeline_status": "completed", "failure_reason": None}, "results": {}}

    with pytest.raises(RuntimeError):
        await activities.execute_step(payload)

    results = [s for s in exporter.get_finished_spans() if s.name == "a2a-task-result"]
    if not recorded:
        assert results == []
        return
    [result] = results
    _assert_routable(result)
    assert result.attributes["agent.status"] == "completed"
    assert "HTTP 529" in result.attributes["agent.respond_error"]


@pytest.mark.asyncio
async def test_plan_task_records_what_was_planned(exporter, monkeypatch):
    monkeypatch.setattr(activities.activity, "info", lambda: _Info())
    plan = Plan(steps=[Step(capability="clone_repo"), Step(capability="generate_change")], rationale="fix it")
    monkeypatch.setattr(activities, "create_plan", AsyncMock(return_value=plan))

    await activities.plan_task({"prompt": "fix it", "config": _traced_config(), "history": []})

    [span] = exporter.get_finished_spans()
    assert span.name == "plan_task"
    assert span.attributes["plan.steps"] == "clone_repo,generate_change"


# ─── Executor ───────────────────────────────────────────────────────────


def _request_context(config):
    value = Value()
    json_format.ParseDict(config, value)
    ctx = MagicMock()
    ctx.message = Message(message_id="m1", role=Role.ROLE_USER, parts=[Part(text="fix it"), Part(data=value)])
    ctx.current_task = None
    ctx.task_id = "task-1"
    ctx.context_id = "ctx-1"
    ctx.get_user_input.return_value = "fix it"
    return ctx


def _fake_temporal(monkeypatch, result=None, error=None):
    handle = SimpleNamespace(result=AsyncMock(return_value=result, side_effect=error))
    client = SimpleNamespace(start_workflow=AsyncMock(return_value=handle))
    monkeypatch.setattr(executor, "get_temporal_client", AsyncMock(return_value=client))
    return client


@pytest.mark.asyncio
async def test_the_executor_roots_the_run_under_the_caller_and_hands_its_own_span_down(exporter, monkeypatch):
    client = _fake_temporal(monkeypatch, result={"status": "completed", "message": "Opened a PR."})

    await executor.AriAgentExecutor().execute(_request_context(_traced_config()), AsyncMock())

    # The outcome is the worker's to record (respond step), not the executor's.
    [task] = exporter.get_finished_spans()
    assert task.name == "a2a-task:oncall"
    _assert_routable(task)
    assert format(task.parent.span_id, "016x") == CALLER_SPAN_ID

    # Activities re-root on the task span, so that is what the workflow carries.
    workflow_input = client.start_workflow.await_args.args[1]
    assert workflow_input["config"]["trace"]["traceparent"] == tracing.traceparent_of(
        SimpleNamespace(get_span_context=lambda: task.context)
    )


@pytest.mark.asyncio
async def test_an_internal_error_is_recorded_on_the_result_span(exporter, monkeypatch):
    _fake_temporal(monkeypatch, error=RuntimeError("worker gone"))

    await executor.AriAgentExecutor().execute(_request_context(_traced_config()), AsyncMock())

    task = _by_name(exporter.get_finished_spans(), "a2a-task:oncall")
    result = _by_name(exporter.get_finished_spans(), "a2a-task-result")
    assert result.parent.span_id == task.context.span_id
    assert result.attributes["agent.status"] == "failed"
    assert result.status.status_code.name == "ERROR"
    assert "worker gone" in result.attributes["error.message"]


@pytest.mark.asyncio
async def test_a_tracing_failure_never_stops_the_task_being_marked_failed(exporter, monkeypatch):
    _fake_temporal(monkeypatch, error=RuntimeError("worker gone"))
    monkeypatch.setattr(tracing, "mark_failed", MagicMock(side_effect=RuntimeError("tracing broke")))
    queue = AsyncMock()

    await executor.AriAgentExecutor().execute(_request_context(_traced_config()), queue)

    final = queue.enqueue_event.await_args_list[-1].args[0]
    assert final.status.state == TaskState.TASK_STATE_FAILED


@pytest.mark.asyncio
async def test_an_untraced_request_reaches_the_workflow_unchanged(exporter, monkeypatch):
    client = _fake_temporal(monkeypatch, result={"status": "no_action", "message": "Nothing to do."})
    config = {"llm": {"provider": "openai", "api_key": "k", "model": "m"}, "repo": {"url": "https://x/y"}}

    await executor.AriAgentExecutor().execute(_request_context(config), AsyncMock())

    assert client.start_workflow.await_args.args[1]["config"] == config
    assert exporter.get_finished_spans() == ()


# ─── Worker ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sigterm_stops_the_worker_and_flushes_queued_spans(monkeypatch):
    # Python's default SIGTERM ends the process before the `finally` that flushes.
    import os
    import signal

    import common.orchestration.worker as worker_module

    calls = []
    built = []

    running = asyncio.Event()

    class _Worker:
        def __init__(self, *args, **kwargs):
            self.stopped = asyncio.Event()
            built.append(self)

        async def run(self):
            # Signal handlers are installed before run(), so a SIGTERM from
            # here on reaches them rather than ending pytest.
            running.set()
            await self.stopped.wait()

        async def shutdown(self):
            calls.append("shutdown")
            self.stopped.set()

    monkeypatch.setattr(worker_module, "Worker", _Worker)
    monkeypatch.setattr(worker_module, "get_temporal_client", AsyncMock(return_value=None))
    monkeypatch.setattr(worker_module.tracing, "shutdown_tracing", lambda: calls.append("flush"))

    task = asyncio.create_task(worker_module.main())
    try:
        await asyncio.wait_for(running.wait(), 5)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)
    finally:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
        asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)

    # Every lane (default and interactive) stops before the flush.
    assert len(built) == 2
    assert calls == ["shutdown", "shutdown", "flush"]
