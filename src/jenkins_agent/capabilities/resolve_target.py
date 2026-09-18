"""Turns the user's words into a concrete Jenkins job/build. Always the
first opencode call of a request that touches Jenkins at all, so it's
also the one that writes this request's opencode.json (mirroring
ari_oncall_agent's clone_repo — there's no clone step here, so this is
where that setup naturally lands instead)."""

from common.capabilities_base import StepContext


async def run(ctx: StepContext) -> dict:
    write_config = ctx.tool("opencode_config")
    opencode = ctx.tool("opencode")

    write_config(ctx.workspace, ctx.config["llm"], ctx.config["jenkins"])

    instruction = ctx.params.get("instruction") or (
        "Resolve the following request to a concrete Jenkins job — and, if named or "
        "implied, a specific build — using the Jenkins MCP tools. Walk folders/multibranch "
        "projects as needed. Report the job's full name (including its folder path) and, "
        "if relevant, the build number. If genuinely ambiguous, list the top candidates "
        "ranked by likelihood with why each matches, rather than guessing. Do not trigger "
        "or stop anything.\n\n"
        f"{ctx.prompt}"
    )
    result = await opencode(ctx, instruction)
    return {"resolution": result["text"], "trace": result["trace"]}
