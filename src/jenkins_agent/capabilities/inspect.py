"""Gathers whatever the question needs about the resolved target — status,
parameters, history, queue position, node — using the Jenkins MCP tools.
LLM-driven tool use, not a fixed fetch list, same philosophy as the oncall
agent's own `generate_change` (investigates as needed, not a fixed
read list).

`write_config` is called again here (not just once by resolve_target) as
a belt-and-suspenders guard: this agent has no state between requests, so
if the planner ever schedules `inspect` without `resolve_target` having
run first in the same plan, there would otherwise be no opencode.json on
disk yet at all."""

from common.capabilities_base import StepContext


async def run(ctx: StepContext) -> dict:
    write_config = ctx.tool("opencode_config")
    opencode = ctx.tool("opencode")

    write_config(ctx.workspace, ctx.config["llm"], ctx.config["jenkins"])
    target = ctx.results.get("resolve_target", {}).get("resolution")

    instruction = ctx.params.get("instruction") or (
        "Gather whatever this question needs about the resolved Jenkins target, using "
        "the Jenkins MCP tools — status, parameters, build history, queue position, which "
        "node it ran on, whatever is actually relevant to the question. Report findings "
        "plainly; don't just restate raw tool output verbatim.\n\n"
        + (f"Resolved target: {target}\n\n" if target else "")
        + f"Request: {ctx.prompt}"
    )
    result = await opencode(ctx, instruction)
    return {"findings": result["text"], "trace": result["trace"]}
