"""The only capability allowed to trigger or stop a real Jenkins build.
Two independent layers make this safe, not one:

1. Structural, in the tool registry / opencode permissions: only this
   capability runs as "ari-jenkins-act" (tools/opencode_cli.py), the only
   opencode agent with mutating `jenkins_*` MCP tools enabled at all.
2. Structural, in this module's own code (`_require_confirmed_action`
   below): refuses to run at all unless `ctx.params` already carries a
   concrete, non-stale confirmed action — never inferred from a bare
   "yes". The planner is responsible for carrying a prior propose_action's
   exact `pending_action` fields into these params from conversation
   history (common/planner.py's jenkins domain guidance says so
   explicitly) — if there's nothing concrete there, this capability
   should never have been scheduled, and it fails loudly rather than
   guessing.

Stopping a build goes through a direct REST call (tools/jenkins_rest.py),
not opencode+MCP — the Jenkins MCP plugin's own documented tool list
doesn't clearly include a stop/cancel tool, so this is deliberately not
left to an LLM to discover live. Triggering goes through opencode+MCP,
which is clearly documented and supported.

Mutations are never retried automatically by the engine (see each
capability's `retry.max_attempts: 1` in configs/jenkins/capabilities.yaml)
— a timed-out trigger may well have already succeeded, and a duplicate
build is far worse than surfacing the timeout to the user.
"""

from datetime import datetime, timezone

from common.capabilities_base import StepContext

_MAX_PROPOSAL_AGE_SECONDS = 30 * 60  # "yes" long after the discussion moved on re-proposes, doesn't fire


def _require_confirmed_action(ctx: StepContext) -> dict:
    action = ctx.params.get("action")
    job = ctx.params.get("job")
    if not action or not job:
        raise RuntimeError(
            "execute_action: no confirmed pending_action in this request's params. A bare "
            "confirmation with nothing concrete behind it is not an instruction to act — the "
            "planner must carry the exact action/job/parameters from a prior "
            "propose_action's pending_action (in conversation history) into this step's "
            "params. If there's nothing concrete to confirm, this step should never have "
            "been scheduled."
        )
    created_at = ctx.params.get("created_at")
    if created_at:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(created_at)
        if age.total_seconds() > _MAX_PROPOSAL_AGE_SECONDS:
            raise RuntimeError(
                f"execute_action: the confirmed proposal is {age} old (created {created_at}) "
                "— too stale to act on without re-proposing. Ask the user to request the "
                "action again."
            )
    return {
        "action": action,
        "job": job,
        "parameters": ctx.params.get("parameters") or {},
        "build_number": ctx.params.get("build_number"),
    }


async def run(ctx: StepContext) -> dict:
    confirmed = _require_confirmed_action(ctx)
    jenkins = ctx.config["jenkins"]

    if confirmed["action"] == "stop_build":
        if confirmed["build_number"] is None:
            raise RuntimeError("execute_action: a confirmed stop_build requires a build_number")
        stop_build = ctx.tool("jenkins_stop_build")
        await stop_build(jenkins, confirmed["job"], confirmed["build_number"])
        return {"result": {"action": "stop_build", "job": confirmed["job"], "build_number": confirmed["build_number"]}}

    if confirmed["action"] == "trigger_build":
        write_config = ctx.tool("opencode_config")
        opencode = ctx.tool("opencode")
        write_config(ctx.workspace, ctx.config["llm"], jenkins)

        instruction = ctx.params.get("instruction") or (
            "Re-validate first (the job still exists, these parameters are still valid, no "
            "identical build is already queued or running) using the Jenkins MCP tools, "
            "then trigger exactly this build — do not change any parameter from what's "
            f"given here.\n\nJob: {confirmed['job']}\nParameters: {confirmed['parameters']}"
        )
        result = await opencode(ctx, instruction)
        return {
            "result": {"action": "trigger_build", "job": confirmed["job"], "parameters": confirmed["parameters"]},
            "trace": result["trace"],
        }

    raise RuntimeError(f"execute_action: unknown confirmed action {confirmed['action']!r}")
