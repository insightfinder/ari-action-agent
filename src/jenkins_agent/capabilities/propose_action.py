"""For any change (trigger or stop a build). Resolves the job and every
parameter, and stops — never calls a mutating tool. That's enforced
structurally, not by trusting this instruction: this capability's opencode
calls run as "ari-jenkins-read" (see tools/opencode_cli.py), whose
`jenkins_*` MCP tools are all disabled — a mutating call would be denied
by opencode's own permission layer regardless of what the model attempts.

Emits a `pending_action` dict (id, action, job, parameters, reasoning,
jenkins_url, created_at) that becomes an A2A artifact (see
jenkins_agent/meta.py's `artifacts`) and — critically — needs to show up
in this conversation's history so a later "yes, go ahead" turn's planner
can carry its exact fields into `execute_action`'s params. This agent has
no state between requests; the proposal only survives via whatever the
caller (MCP Service/ARI) round-trips back as conversation history, same
mechanism the oncall agent already relies on for "yes, go ahead and fix
it" (see common/planner.py)."""

import json
import uuid
from datetime import datetime, timezone

from common.capabilities_base import StepContext

_PROPOSAL_MARKER = "PROPOSAL_JSON:"


def _extract_proposal(text: str) -> dict | None:
    idx = text.rfind(_PROPOSAL_MARKER)
    if idx == -1:
        return None
    tail = text[idx + len(_PROPOSAL_MARKER) :].strip()
    if tail.startswith("```"):
        tail = tail.strip("`")
        if tail.lower().startswith("json"):
            tail = tail[4:]
    try:
        data = json.loads(tail.strip())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def run(ctx: StepContext) -> dict:
    write_config = ctx.tool("opencode_config")
    opencode = ctx.tool("opencode")

    write_config(ctx.workspace, ctx.config["llm"], ctx.config["jenkins"])
    target = ctx.results.get("resolve_target", {}).get("resolution")

    instruction = ctx.params.get("instruction") or (
        "Prepare (but do NOT execute) the requested Jenkins change, using only read-only "
        "Jenkins MCP tools — a mutating one will be denied outright if you try. Resolve "
        "the exact job and, for a trigger, every parameter it needs (name, chosen value, "
        "and why — job_info's parameter definitions tell you what's required/defaulted). "
        "For a stop, resolve the exact build number to stop.\n\n"
        "End your response with a line reading exactly 'PROPOSAL_JSON:' followed by ONLY a "
        "JSON object (no markdown fence, no other text) with this shape:\n"
        '{"action": "trigger_build" | "stop_build", "job": "<full job name, with folder '
        'path>", "parameters": {"<name>": "<value>", ...}, "build_number": <int, only for '
        'stop_build>, "reasoning": "<one line per parameter/choice made>"}\n\n'
        + (f"Resolved target: {target}\n\n" if target else "")
        + f"Request: {ctx.prompt}"
    )
    result = await opencode(ctx, instruction)
    proposal = _extract_proposal(result["text"])
    if not proposal:
        raise RuntimeError(
            "propose_action: could not extract a PROPOSAL_JSON block from opencode's "
            f"response: {result['text'][:2000]}"
        )

    pending_action = {
        "id": str(uuid.uuid4()),
        "action": proposal.get("action"),
        "job": proposal.get("job"),
        "parameters": proposal.get("parameters") or {},
        "build_number": proposal.get("build_number"),
        "reasoning": proposal.get("reasoning", ""),
        "jenkins_url": ctx.config["jenkins"]["base_url"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"pending_action": pending_action, "trace": result["trace"]}
