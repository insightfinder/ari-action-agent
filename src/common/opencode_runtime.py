"""Generic opencode process/event-parsing/tracing mechanics — shared by
every agent kind's own (thin) tools/opencode_cli.py, which supplies only
what's actually agent-specific: the capability→agent name mapping, the
scoped `_AGENT_DEFS` permission blocks, and `write_config`'s MCP wiring.

Design, from opencode's own docs/source (docs/cli, docs/mcp-servers,
docs/agents, and packages/opencode/src/cli/cmd/run.ts +
src/session/message-v2.ts as of the `dev` branch):

- **`opencode run --dir <workdir> --agent <name> --format json --auto`**,
  one ephemeral process per capability step. No long-lived `opencode
  serve` — a Temporal activity should be a self-contained unit of work,
  and a subprocess kept alive across separate activity invocations (which
  Temporal doesn't guarantee land on the same worker) is exactly the kind
  of hidden cross-activity state that breaks that model. Continuity across
  steps instead comes from opencode's own `--continue` flag, which resumes
  the most recent session for that `--dir` from disk — no session ID
  needs to be threaded through Temporal payloads.
- `--auto` (auto-approve anything not explicitly denied) is a safety net,
  not the primary control — each agent kind's own agent permission blocks
  only ever use `allow`/`deny` (never `ask`), because there is no human
  present to answer a prompt in this pipeline.

Verified 2026-09-15 against a real `opencode` binary (v1.18.31, auth
deliberately left invalid — no live model call, but this confirmed real
CLI/config behavior, not just docs):

- `--format json` is confirmed NDJSON (one JSON object per stdout line).
- **`agent.<name>.mode` must be `"primary"`, not `"subagent"`.** With
  `"subagent"`, `opencode run --agent <name> ...` silently falls back to
  the built-in `build` agent instead of erroring — confirmed via
  `--print-logs --log-level DEBUG` showing `agent "<name>" is a subagent,
  not a primary agent. Falling back to default agent`. Every agent kind's
  own `_AGENT_DEFS` uses `"primary"` for exactly this reason; a subagent
  can only be reached via `@mention` inside an interactive session, never
  via `--agent` on `run`.
- On failure, the error is a top-level `{"type": "error", "error": {"data":
  {"message": ...}}}` event on **stdout**, not stderr — stderr was
  completely empty even on a hard auth failure. `run_opencode` below reads
  the error out of stdout instead of trusting stderr.
- The success-path event shape (text/tool parts while a model actually
  responds) could not be exercised without a real API key; `_parse_events`
  handles that case defensively based on `session/message-v2.ts`'s Part
  schema and keeps `raw_events`/`raw_stdout` so nothing is lost if it
  differs once run against a live model.
"""

import asyncio
import json
import os
import time

import structlog
from opentelemetry.trace import SpanKind

from . import skills, subprocess_watch, tracing
from .settings import get_settings

logger = structlog.get_logger()


# opencode's own subprocess wait, bounded — the same guard git_ops has had
# since 2026-10-05, in the one module that never got it.
#
# Confirmed live on stg, 2026-10-09 (workflow 5a0f4506, image
# 2610091640-staging): `generate_change`'s opencode call returned normally
# after 328s and logged `opencode_call`, and the activity then sat in
# `proc.communicate()` for a further 14 minutes until Temporal's 1200s
# `start_to_close` fired `CancelledError: Activity cancelled`. Inspected
# while it was stuck: no opencode process anywhere in the container (only
# pid 1 tini and pid 7 the worker), all eight threads in
# `futex_wait`/`ep_poll`, CPU 1m, memory flat at 315Mi. The child was gone
# and asyncio was never told. Attempt 2 ran the identical step on the
# identical image in 93s.
#
# That is the same race git_ops.py's own comment describes against `git
# diff`, symptom for symptom, which is the reason this is a deadlock guard
# and not an attempt at a root cause: the cause is still unproven, and a
# bound is what turns 20 minutes of silence into a fast, named failure the
# existing retry can act on.
#
# Why the guard did not already cover this: the regression test for it,
# `test_every_git_call_goes_through_the_deadline_helper`, scans
# `inspect.getsource(git_ops)` — one module. It could never see this file.
# That test now scans the whole package; see tests/test_git_ops.py.
#
# The deadline is derived per call, not fixed, because the two callers are
# an order of magnitude apart: a legitimate `generate_change` turn has been
# measured at 328s, while `verify`'s whole step budget is 480s. A single
# constant generous enough for the first would never fire inside the
# second. `ctx.step_deadline_seconds` carries the activity's own effective
# `start_to_close` (set in orchestration/activities.py, where temporalio is
# already imported), and the margin leaves room for this function's own
# post-processing to still raise a named error *before* Temporal kills the
# attempt blindly.
_OPENCODE_TIMEOUT_SECONDS = float(os.getenv("OPENCODE_SUBPROCESS_TIMEOUT_SECONDS", "1200"))
_OPENCODE_DEADLINE_MARGIN_SECONDS = float(os.getenv("OPENCODE_DEADLINE_MARGIN_SECONDS", "45"))
# Never let the derived value collapse to something that would fire on
# healthy work: a step whose remaining budget is already under this has
# bigger problems than a stalled pipe, and Temporal will end it anyway.
_OPENCODE_MIN_TIMEOUT_SECONDS = 60.0


def _subprocess_deadline(ctx) -> float:
    """How long this opencode call may sit in `communicate()` before it is
    treated as deadlocked and killed."""
    step_deadline = getattr(ctx, "step_deadline_seconds", None)
    if not step_deadline:
        return _OPENCODE_TIMEOUT_SECONDS
    return max(_OPENCODE_MIN_TIMEOUT_SECONDS, step_deadline - _OPENCODE_DEADLINE_MARGIN_SECONDS)


async def _communicate(proc, agent: str, timeout: float):
    """`proc.communicate()` with a deadline; kills the child and raises on
    expiry. Mirrors git_ops._communicate, deliberately — same failure, same
    shape of guard, so neither one reads as the odd case."""
    try:
        return await asyncio.wait_for(subprocess_watch.communicate(proc), timeout=timeout)
    except asyncio.TimeoutError:
        # Don't leave the child holding the sandbox: every attempt of one
        # request shares a workspace keyed by request_id alone, so a
        # survivor could still be writing the tree the retry is about to
        # read.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError(
            f"opencode ({agent}) timed out after {timeout:g}s (pid {proc.pid}) and was killed"
        ) from None


def _iter_json_lines(raw: bytes):
    for line in raw.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _extract_error(events: list[dict]) -> str | None:
    """Confirmed live: a fatal error (e.g. bad API key) arrives as a
    top-level `{"type": "error", "error": {"data": {"message": ...}}}`
    event on stdout — stderr is empty even on a hard failure. Checked
    before anything else in `_parse_events` since these events don't carry
    a "part" and would otherwise be silently skipped."""
    for event in events:
        if isinstance(event, dict) and event.get("type") == "error":
            err = event.get("error") or {}
            data = err.get("data") or {}
            return data.get("message") or err.get("name") or json.dumps(err)
    return None


def _parse_events(raw: bytes) -> dict:
    """Best-effort extraction of the final text and any tool calls from a
    `--format json` event stream. See module docstring for the caveat on
    the exact wire schema."""
    text_chunks: list[str] = []
    tool_calls: list[dict] = []
    events = list(_iter_json_lines(raw))

    for event in events:
        # Events may be a bare Part, or wrap one under "part"/"info".
        part = event.get("part", event) if isinstance(event, dict) else None
        if not isinstance(part, dict):
            continue

        if part.get("type") == "text" and part.get("text"):
            text_chunks.append(part["text"])

        if part.get("type") == "tool" or str(part.get("type", "")).startswith("tool-"):
            state = part.get("state") or {}
            tool_calls.append(
                {
                    "tool": part.get("tool") or str(part.get("type", "")).removeprefix("tool-"),
                    "status": state.get("status") or part.get("state"),
                    "input": state.get("input"),
                    "output": state.get("output"),
                    "error": state.get("error"),
                }
            )

    return {
        "text": "\n".join(text_chunks).strip(),
        "tool_calls": tool_calls,
        "error": _extract_error(events),
        "raw_events": events,
        "raw_stdout": raw.decode(errors="replace"),
    }


def _build_trace(events: list[dict], wall_elapsed_s: float) -> dict:
    """Per-opencode-call observability: tool-call counts and *where the
    time actually went*, extracted from opencode's own per-event
    timestamps rather than a single start/end wall-clock number.

    This is exactly the kind of thing that used to require a one-off
    manual reproduction to find out — see docs/MEMORY.md, 2026-09-17: the
    only reason "4 delegated `task` calls ate 68% of one run" was ever
    discovered was installing opencode locally, replaying the same
    instruction, and eyeballing a hand-written analysis script's output.
    `slowest_gaps` below is that same "biggest gap before a tool call"
    signal, computed automatically on every real call from now on, so a
    slow step can be diagnosed straight from `data.<capability>.trace` in
    the final response (or the structured log line this feeds) instead of
    needing to reproduce it after the fact.
    """
    counts: dict[str, int] = {}
    gaps: list[dict] = []
    first_ts: int | None = None
    prev_ts: int | None = None

    for event in events:
        if not isinstance(event, dict) or "timestamp" not in event:
            continue
        ts = event["timestamp"]
        if first_ts is None:
            first_ts = ts
        part = event.get("part", event)
        is_tool = isinstance(part, dict) and (
            part.get("type") == "tool" or str(part.get("type", "")).startswith("tool-")
        )
        if is_tool:
            tool = part.get("tool") or str(part.get("type", "")).removeprefix("tool-")
            counts[tool] = counts.get(tool, 0) + 1
            if prev_ts is not None:
                gaps.append(
                    {
                        "tool": tool,
                        "at_s": round((ts - first_ts) / 1000, 2),
                        "preceded_by_gap_s": round((ts - prev_ts) / 1000, 2),
                    }
                )
        prev_ts = ts

    return {
        "elapsed_s": round(wall_elapsed_s, 2),
        # Raw per-event-line counts, not deduplicated per logical tool
        # invocation — a single call can emit multiple state-transition
        # events (e.g. pending -> completed), so this over-counts somewhat.
        # Still a reliable *relative* signal across runs/capabilities.
        "tool_call_counts": counts,
        "tool_call_total": sum(counts.values()),
        "slowest_gaps": sorted(gaps, key=lambda g: -g["preceded_by_gap_s"])[:5],
    }


def merge_traces(*traces: dict) -> dict:
    """Combines multiple `run_opencode()` traces into one — for a
    capability that makes more than one opencode call in its own `run()`
    (e.g. oncall's `generate_change` enforcement follow-up), so the final
    per-capability trace reflects the *whole* step, not just its last
    call."""
    counts: dict[str, int] = {}
    gaps: list[dict] = []
    elapsed = 0.0
    for trace in traces:
        elapsed += trace["elapsed_s"]
        for tool, n in trace["tool_call_counts"].items():
            counts[tool] = counts.get(tool, 0) + n
        gaps.extend(trace["slowest_gaps"])
    return {
        "elapsed_s": round(elapsed, 2),
        "tool_call_counts": counts,
        "tool_call_total": sum(counts.values()),
        "slowest_gaps": sorted(gaps, key=lambda g: -g["preceded_by_gap_s"])[:5],
    }


def _ns(ms) -> int | None:
    return int(ms) * 1_000_000 if isinstance(ms, (int, float)) and ms > 0 else None


def record_spans(ctx, agent: str, events: list[dict], started_ns: int, ended_ns: int,
                 failure: str | None, *, continued_session: bool = False) -> None:
    """One `opencode:<agent>` span for this call, with opencode's own steps
    and tool calls under it, timed by opencode's own timestamps.

    The event shapes are opencode 1.18.31's `--format json` (pinned in
    Dockerfile.oncall; checked against its run.ts and v1 session schema, and
    against a live run on test): a `step_start` / `step_finish` pair per model
    call, the latter carrying `part.tokens` {input, output, reasoning, cache:
    {read, write}} and `part.cost`; `tool_use` once per call when it completes
    or fails, with `part.state.time` {start, end} in epoch milliseconds. A
    no-op when the request is untraced; never raises, and never leaves a
    started span unended."""
    if not tracing.active():
        return
    outer = tracing.start_span(
        f"opencode:{agent}",
        start_time=started_ns,
        attributes={
            "trace.entity": "opencode-call",
            "opencode.agent": agent,
            "opencode.capability": ctx.capability,
            "opencode.continued_session": continued_session,
        },
    )
    if outer is None:
        return
    open_steps: list = []
    try:
        _record_steps(ctx, outer, events, started_ns, ended_ns, open_steps)
        if failure:
            tracing.mark_failed(outer, failure)
    except Exception as exc:  # noqa: BLE001 - tracing must never fail a step
        logger.warning("opencode_trace_failed", request_id=ctx.request_id,
                       error=f"{type(exc).__name__}: {exc}")
    finally:
        for step in open_steps:
            step.end(end_time=ended_ns)
        outer.end(end_time=ended_ns)


def _number(value) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _record_steps(ctx, outer, events: list[dict], started_ns: int, ended_ns: int, open_steps: list) -> None:
    """`open_steps` holds the step span still open, so the caller can end it
    whatever happens here."""
    llm = ctx.config["llm"]
    provider, model = llm["provider"].lower(), llm["model"]
    step_text: list[str] = []
    steps, tool_calls, cost = 0, 0, 0.0
    seen_calls: set[str] = set()
    last_ns = started_ns

    def finish_step(end_ns):
        if open_steps:
            step = open_steps.pop()
            tracing.set_attributes(step, {
                "gen_ai.completion.0.role": "assistant",
                "gen_ai.completion.0.content": tracing.truncate("\n".join(step_text)),
            })
            step.end(end_time=end_ns)

    for event in events:
        if not isinstance(event, dict):
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        kind = part.get("type")
        # An event without a time keeps its predecessor's, inside this call.
        timestamp = _ns(event.get("timestamp")) or last_ns
        last_ns = timestamp

        if kind == "step-start":
            finish_step(timestamp)
            steps += 1
            step_text = []
            step = tracing.start_span(
                f"llm:{provider}/{model}",
                parent=outer,
                kind=SpanKind.CLIENT,
                start_time=timestamp,
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.system": provider,
                    "gen_ai.request.model": model,
                    "opencode.step": steps,
                },
            )
            if step is not None:
                open_steps.append(step)
        elif kind == "text" and isinstance(part.get("text"), str) and part["text"]:
            step_text.append(part["text"])
        elif kind == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            call_id = str(part.get("callID") or part.get("id") or "")
            if state.get("status") not in ("completed", "error") or (call_id and call_id in seen_calls):
                continue
            seen_calls.add(call_id)
            tool_calls += 1
            _record_tool(open_steps[-1] if open_steps else outer, part, state, timestamp)
        elif kind == "step-finish":
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            cache_read, cache_write = _number(cache.get("read")), _number(cache.get("write"))
            if open_steps:
                # opencode's `input` excludes the cache and `output` excludes
                # reasoning; the dashboard wants raw input and billed output.
                tracing.set_token_attributes(
                    open_steps[-1],
                    _number(tokens.get("input")) + cache_read + cache_write,
                    _number(tokens.get("output")) + _number(tokens.get("reasoning")),
                    cache_read, cache_write, model,
                )
                tracing.set_attributes(open_steps[-1], {
                    "gen_ai.usage.reasoning_tokens": _number(tokens.get("reasoning")),
                    "opencode.finish_reason": part.get("reason"),
                    "opencode.cost_usd": _number(part.get("cost")),
                })
            cost += _number(part.get("cost"))
            finish_step(timestamp)

    finish_step(ended_ns)
    tracing.set_attributes(outer, {
        "opencode.steps": steps,
        "opencode.tool_call_total": tool_calls,
        "opencode.cost_usd": round(cost, 6),
    })


# Tool arguments that name *where*, not *what*: kept verbatim. Everything
# else — a `write`'s file contents, an `edit`'s old and new text, a `bash`
# command that may carry a token — is recorded as its size only, for the same
# reason a tool's output is: it is the customer's code, and the trace server
# stores whatever it is sent.
_LOCATION_ARGS = frozenset({"filePath", "path", "pattern", "include", "glob", "description"})


def _tool_args(arguments) -> str:
    if not isinstance(arguments, dict):
        return ""
    summary = {
        key: value if key in _LOCATION_ARGS and isinstance(value, str) else f"<{len(str(value))} chars>"
        for key, value in arguments.items()
    }
    return tracing.truncate(json.dumps(summary, default=str), 1000)


def _record_tool(parent, part: dict, state: dict, fallback_ns) -> None:
    times = state.get("time") if isinstance(state.get("time"), dict) else {}
    tool = part.get("tool") or "unknown"
    output = state.get("output") if isinstance(state.get("output"), str) else ""
    span = tracing.start_span(
        f"tool:{tool}",
        parent=parent,
        start_time=_ns(times.get("start")) or fallback_ns,
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "tool.name": tool,
            "tool.status": state.get("status"),
            "tool.args": _tool_args(state.get("input")),
            "tool.result_size": len(output),
        },
    )
    if span is None:
        return
    if state.get("status") == "error":
        # Its first line only: the rest of a tool's error can quote the file
        # or command output it was working on.
        error = str(state.get("error") or "tool failed").strip().splitlines() or ["tool failed"]
        tracing.mark_failed(span, tracing.truncate(error[0], 200))
    span.end(end_time=_ns(times.get("end")) or fallback_ns)


def opencode_config_home(workspace: str) -> str:
    return os.path.join(workspace, ".opencode-state", "config")


async def run_opencode(
    ctx,
    instruction: str,
    agent: str,
    *,
    continue_session: bool | None = None,
    extra_env: dict[str, str] | None = None,
    workdir: str | None = None,
    drop_env: tuple[str, ...] = (),
) -> dict:
    """Runs one opencode turn for the calling capability. `ctx` is the
    capability's `StepContext`; `agent` is the specific opencode agent name
    to run as — the caller (each agent kind's own tools/opencode_cli.py)
    resolves this from its own `CAPABILITY_AGENTS` mapping, since that
    mapping is agent-kind-specific and this function deliberately isn't.

        agent = CAPABILITY_AGENTS[ctx.capability]
        result = await run_opencode(ctx, instruction, agent)

    `continue_session` overrides the automatic `ctx.opencode_session_started`
    detection — needed when a capability makes more than one opencode call
    *within its own `run()`* (e.g. a follow-up enforcement call after the
    first response didn't comply with a formatting instruction): from
    `ctx.results`'s point of view that capability hasn't "started" a
    session yet (its own entry is only set by the orchestrator after
    `run()` returns), so the automatic check alone would wrongly start a
    second, context-less session instead of continuing the first.

    `extra_env` is merged over this process's own environment for the
    opencode subprocess only — e.g. Jenkins's per-request CA bundle/TLS
    settings (`NODE_EXTRA_CA_CERTS`/`NODE_TLS_REJECT_UNAUTHORIZED`, opencode
    being a Node CLI), computed by the caller from that request's own
    config. Generic here on purpose: this function has no idea what
    "jenkins" is, only that a capability may need to add env vars for one
    call.
    """
    settings = get_settings()
    llm = ctx.config["llm"]

    cmd = [
        settings.opencode_bin,
        "run",
        "--dir", workdir or ctx.repo_dir,
        "--agent", agent,
        "--model", f"{llm['provider']}/{llm['model']}",
        "--format", "json",
        "--auto",
    ]
    should_continue = ctx.opencode_session_started if continue_session is None else continue_session
    if should_continue:
        cmd.append("--continue")
    cmd.append(instruction)

    # opencode keeps its own local session/auth state under XDG_DATA_HOME
    # (default ~/.local/share/opencode/...), one shared SQLite file
    # regardless of --dir. Without per-request isolation, two capabilities
    # from *different* requests that happen to call opencode concurrently
    # in this same worker process collide on that file — confirmed live as
    # `RuntimeError: opencode (...) failed (exit 1): ... database is
    # locked`, costing a full activity retry (~30-40s) or an outright
    # failure. Only the XDG_* dirs are isolated per request, deliberately
    # NOT the real $HOME: `verify` shells out to the target repo's own
    # build tooling (mvn/go/npm) via opencode's bash tool, which inherits
    # this same env — those tools cache dependencies under the real
    # $HOME (~/.m2, ~/go, ~/.npm), and overriding HOME too would silently
    # force a cold dependency download on every single request, on top of
    # opencode's own state. Isolating XDG_DATA_HOME alone still fully fixes
    # the lock contention (that's the only thing opencode itself reads/
    # writes there) while leaving those build caches shared and warm across
    # requests on this pod. Cleaned up for free by cleanup_workspace tearing
    # down the workspace — nothing new to garbage-collect. Auth/model
    # config isn't affected either way: opencode reads that from the
    # per-request opencode.json in --dir (write_config), never from
    # anything under HOME/XDG_DATA_HOME.
    opencode_state_dir = os.path.join(ctx.workspace, ".opencode-state")
    os.makedirs(opencode_state_dir, exist_ok=True)
    skills.prepare(opencode_config_home(ctx.workspace))
    base_env = {
        **os.environ,
        "XDG_DATA_HOME": os.path.join(opencode_state_dir, "data"),
        "XDG_CONFIG_HOME": opencode_config_home(ctx.workspace),
        "XDG_CACHE_HOME": os.path.join(opencode_state_dir, "cache"),
        "XDG_STATE_HOME": os.path.join(opencode_state_dir, "state"),
    }
    env = {**base_env, **skills.opencode_env(), **(extra_env or {})}
    for name in drop_env:
        env.pop(name, None)

    start = time.monotonic()
    started_ns = time.time_ns()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await _communicate(proc, agent, _subprocess_deadline(ctx))
    except BaseException as exc:
        # A timed-out call is the one most worth seeing in the trace; its
        # output died with the process, so the span carries only the failure.
        record_spans(ctx, agent, [], started_ns, time.time_ns(), f"{type(exc).__name__}: {exc}",
                     continued_session=should_continue)
        raise
    wall_elapsed = time.monotonic() - start
    result = _parse_events(stdout)
    result["trace"] = _build_trace(result["raw_events"], wall_elapsed)
    logger.info(
        "opencode_call",
        request_id=ctx.request_id,
        capability=ctx.capability,
        agent=agent,
        continued_session=should_continue,
        **result["trace"],
    )
    failure = None
    if proc.returncode != 0:
        failure = result.get("error") or stderr.decode(errors="replace") or f"exit {proc.returncode}"
    record_spans(ctx, agent, result["raw_events"], started_ns, time.time_ns(), failure,
                 continued_session=should_continue)
    if proc.returncode != 0:
        # stderr is reliably empty in production (confirmed live — even a
        # hard 401 produces nothing on stderr); the real message is in the
        # stdout event stream, so surface that first and stderr only as a
        # fallback in case this was a crash before opencode emitted anything.
        detail = result.get("error") or stderr.decode(errors="replace") or "(no output on stdout or stderr)"
        raise RuntimeError(f"opencode ({agent}) failed (exit {proc.returncode}): {detail}")

    return result
