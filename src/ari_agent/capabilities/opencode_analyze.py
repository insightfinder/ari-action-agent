from .base import StepContext


async def run(ctx: StepContext) -> dict:
    opencode = ctx.tool("opencode")
    instruction = ctx.params.get("instruction") or (
        "Analyze this repository and localize the root cause of the following issue. "
        "Report a concise summary and file/line evidence. Do not change any code yet.\n\n"
        f"{ctx.prompt}"
    )
    result = await opencode(ctx, instruction)
    return {"root_cause": {"summary": result["text"], "evidence": []}}
