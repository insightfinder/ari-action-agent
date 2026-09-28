"""Only for a failure. Reads the console log from the tail backwards (a
full Jenkins console log routinely runs to hundreds of megabytes and is
mostly noise — fetching it whole would be both useless and impossible
within a context window) plus the test report when one exists, and
produces a cause with evidence.

Uses the same "SUMMARY:" marker + enforcement-retry pattern proven for
the oncall agent's `generate_change` (docs/MEMORY.md, 2026-09-18): opencode
doesn't always end its turn with the exact marker asked for, and silently
falling back to the full raw narration would reintroduce a real bug
(a diagnosis buried in step-by-step tool-use commentary instead of a
clean cause)."""

from common.capabilities_base import StepContext
from common.opencode_runtime import merge_traces

_SUMMARY_MARKER = "SUMMARY:"

_ENFORCEMENT_INSTRUCTION = (
    "Reply with ONLY a concise diagnosis: the failing step, the first real error line "
    "(exception, assertion, or test failure), and the cause. No narration, no 'let's do X' "
    "commentary, no markdown headers, no tool calls. Just the diagnosis text."
)


def _extract_summary(text: str) -> str | None:
    idx = text.rfind(_SUMMARY_MARKER)
    if idx == -1:
        return None
    return text[idx + len(_SUMMARY_MARKER) :].strip()


async def run(ctx: StepContext) -> dict:
    write_config = ctx.tool("opencode_config")
    opencode = ctx.tool("opencode")

    write_config(ctx.workspace, ctx.config["llm"], ctx.config["jenkins"])
    target = ctx.results.get("resolve_target", {}).get("resolution")

    instruction = ctx.params.get("instruction") or (
        "Diagnose why the resolved Jenkins build failed, using the Jenkins MCP tools. Read "
        "the console log from the tail backwards first — it holds the failure in the large "
        "majority of cases — and walk backwards or filter further only if that's not "
        "enough; never dump the whole log. Check the test report too when one exists. "
        "Identify the failing step and the first real error line, not just a pass/fail "
        "restatement. End your response with a line reading exactly 'SUMMARY:' followed by "
        "a concise cause with evidence (file/step/line where relevant).\n\n"
        + (f"Resolved target: {target}\n\n" if target else "")
        + f"Request: {ctx.prompt}"
    )
    result = await opencode(ctx, instruction)
    trace = result["trace"]
    cause = _extract_summary(result["text"])
    if cause is None:
        followup = await opencode(ctx, _ENFORCEMENT_INSTRUCTION, continue_session=True)
        cause = followup["text"].strip() or result["text"].strip()
        trace = merge_traces(trace, followup["trace"])
    return {"cause": cause, "trace": trace}
