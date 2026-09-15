"""opencode integration.

opencode (https://opencode.ai) does all the actual code analysis/editing/
test-running and, via its built-in MCP client, the GitHub PR operations too
— this module is just the process boundary + config plumbing + output
parsing around `opencode run`.

Design, from opencode's own docs/source (docs/cli, docs/mcp-servers,
docs/agents, and packages/opencode/src/cli/cmd/run.ts +
src/session/message-v2.ts as of the `dev` branch):

- **One opencode.json per request**, written into the cloned repo dir
  (`write_config`) — never global — carrying that request's LLM provider
  API key and, if this request needs to publish, the GitHub MCP server
  pointed at the request's own repo PAT. Nothing here is shared across
  requests or persisted past the sandbox's lifetime.
- **Four scoped agents** (`ari-analyze` / `ari-fix` / `ari-verify` /
  `ari-publish`), one per capability that touches opencode, each declaring
  only the permissions that capability needs (least privilege — e.g.
  `analyze` can't edit files, `publish` can't touch build tooling, only
  `publish` gets the `github_*` MCP tools). This is enforced by opencode
  itself via `agent.<name>.permission` / `tools`, not just prompted for.
- **`opencode run --dir <repo> --agent <name> --format json --auto`**, one
  ephemeral process per capability step. No long-lived `opencode serve` —
  a Temporal activity should be a self-contained unit of work, and a
  subprocess kept alive across separate activity invocations (which
  Temporal doesn't guarantee land on the same worker) is exactly the kind
  of hidden cross-activity state that breaks that model. Continuity across
  steps (analyze → fix → verify → publish, in whatever order the planner
  picked) instead comes from opencode's own `--continue` flag, which
  resumes the most recent session for that `--dir` from disk — no session
  ID needs to be threaded through Temporal payloads.
- `--auto` (auto-approve anything not explicitly denied) is a safety net,
  not the primary control — the agents' permission blocks only ever use
  `allow`/`deny` (never `ask`), because there is no human present to answer
  a prompt in this pipeline.

Verified 2026-09-15 against a real `opencode` binary (v1.18.31, auth
deliberately left invalid — no live model call, but this confirmed real
CLI/config behavior, not just docs):

- `--format json` is confirmed NDJSON (one JSON object per stdout line).
- **`agent.<name>.mode` must be `"primary"`, not `"subagent"`.** With
  `"subagent"`, `opencode run --agent ari-analyze ...` silently falls back
  to the built-in `build` agent instead of erroring — confirmed via
  `--print-logs --log-level DEBUG` showing `agent "ari-analyze" is a
  subagent, not a primary agent. Falling back to default agent`. `_AGENT_DEFS`
  below uses `"primary"` for exactly this reason; a subagent can only be
  reached via `@mention` inside an interactive session, never via `--agent`
  on `run`.
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
from pathlib import Path
from typing import Any

from ..settings import get_settings

# Mirrors configs/capabilities.yaml's capability→agent mapping. Kept here
# (not just in the YAML) because it's opencode-specific wiring, not a fact
# about the capability itself.
CAPABILITY_AGENTS = {
    "analyze": "ari-analyze",
    "generate_fix": "ari-fix",
    "verify": "ari-verify",
    "publish_pr": "ari-publish",
}

_AGENT_DEFS = {
    # Read-only: localize the root cause without touching the tree.
    "ari-analyze": {
        "description": "Analyzes the repo to localize a root cause. Never edits files.",
        "mode": "primary",
        "permission": {
            "edit": "deny",
            "bash": {"git *": "allow", "*": "deny"},
            "webfetch": "deny",
        },
    },
    # Can edit, can inspect git history, cannot run arbitrary build/test
    # commands (that's verify's job) and cannot touch GitHub.
    "ari-fix": {
        "description": "Generates a code fix for the analyzed issue.",
        "mode": "primary",
        "permission": {
            "edit": "allow",
            "bash": {"git *": "allow", "*": "deny"},
            "webfetch": "deny",
        },
    },
    # Needs to actually run the project's build/lint/test tooling, which is
    # unpredictable per-repo — broad bash allow is required here. Isolation
    # comes from the ephemeral per-request sandbox, not command whitelisting.
    # Never edits code — a failure just reports failure, generate_fix retries.
    "ari-verify": {
        "description": "Runs the project's build/lint/test suite and reports pass/fail.",
        "mode": "primary",
        "permission": {
            "edit": "deny",
            "bash": "allow",
            "webfetch": "deny",
        },
    },
    # The only agent with GitHub MCP tools enabled. No build tooling.
    "ari-publish": {
        "description": "Pushes the fix branch and opens a pull request via the GitHub MCP server.",
        "mode": "primary",
        "permission": {
            "edit": "deny",
            "bash": {"git *": "allow", "*": "deny"},
            "webfetch": "deny",
        },
        "tools": {"github_*": True},
    },
}


def write_config(workdir: str, llm: dict, repo: dict) -> str:
    """Writes a per-request opencode.json into the cloned repo dir. Called
    once, right after clone, before any opencode invocation. Torn down with
    the rest of the sandbox in cleanup_workspace — nothing here outlives
    the request.
    """
    settings = get_settings()

    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            # Well-known provider id (e.g. "openai", "anthropic") — opencode
            # already knows its model catalog; we only need to override auth.
            llm["provider"]: {"options": {"apiKey": llm["api_key"]}}
        },
        # Every tool from the github MCP server is off by default; only the
        # ari-publish agent re-enables it (see _AGENT_DEFS). Keeps the
        # analyze/fix/verify agents from ever seeing (or spending context
        # budget on) GitHub tools they have no business calling.
        "tools": {"github_*": False},
        "agent": _AGENT_DEFS,
    }

    if repo.get("pat"):
        config["mcp"] = {
            "github": {
                "type": "remote",
                "url": settings.github_mcp_server_url,
                "enabled": True,
                "oauth": False,
                "headers": {"Authorization": f"Bearer {repo['pat']}"},
            }
        }

    path = Path(workdir) / "opencode.json"
    path.write_text(json.dumps(config, indent=2))

    # Keep it out of `git status`/diffs/the eventual PR without touching the
    # repo's own .gitignore (which is itself something opencode might edit).
    exclude_path = Path(workdir) / ".git" / "info" / "exclude"
    if exclude_path.parent.is_dir():
        with exclude_path.open("a") as f:
            f.write("\nopencode.json\n")

    return str(path)


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


async def run_opencode(ctx, instruction: str) -> dict:
    """Runs one opencode turn for the calling capability. `ctx` is the
    capability's `StepContext` — this function derives the agent, working
    directory, model, and whether to `--continue` a prior session from it,
    so every capability calls it the same way:

        result = await ctx.tool("opencode")(ctx, "<instruction>")
    """
    settings = get_settings()
    llm = ctx.config["llm"]
    agent = CAPABILITY_AGENTS[ctx.capability]

    cmd = [
        settings.opencode_bin,
        "run",
        "--dir", ctx.repo_dir,
        "--agent", agent,
        "--model", f"{llm['provider']}/{llm['model']}",
        "--format", "json",
        "--auto",
    ]
    if ctx.opencode_session_started:
        cmd.append("--continue")
    cmd.append(instruction)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    result = _parse_events(stdout)
    if proc.returncode != 0:
        # stderr is reliably empty in production (confirmed live — even a
        # hard 401 produces nothing on stderr); the real message is in the
        # stdout event stream, so surface that first and stderr only as a
        # fallback in case this was a crash before opencode emitted anything.
        detail = result.get("error") or stderr.decode(errors="replace") or "(no output on stdout or stderr)"
        raise RuntimeError(f"opencode ({agent}) failed (exit {proc.returncode}): {detail}")

    return result
