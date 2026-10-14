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

from .settings import get_settings

logger = structlog.get_logger()


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


async def run_opencode(
    ctx, instruction: str, agent: str, *, continue_session: bool | None = None, extra_env: dict[str, str] | None = None
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
        "--dir", ctx.repo_dir,
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
    base_env = {
        **os.environ,
        "XDG_DATA_HOME": os.path.join(opencode_state_dir, "data"),
        "XDG_CONFIG_HOME": os.path.join(opencode_state_dir, "config"),
        "XDG_CACHE_HOME": os.path.join(opencode_state_dir, "cache"),
        "XDG_STATE_HOME": os.path.join(opencode_state_dir, "state"),
    }
    env = {**base_env, **extra_env} if extra_env else base_env

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
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
    if proc.returncode != 0:
        # stderr is reliably empty in production (confirmed live — even a
        # hard 401 produces nothing on stderr); the real message is in the
        # stdout event stream, so surface that first and stderr only as a
        # fallback in case this was a crash before opencode emitted anything.
        detail = result.get("error") or stderr.decode(errors="replace") or "(no output on stdout or stderr)"
        raise RuntimeError(f"opencode ({agent}) failed (exit {proc.returncode}): {detail}")

    return result
