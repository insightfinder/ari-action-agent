from .base import StepContext


async def run(ctx: StepContext) -> dict:
    opencode = ctx.tool("opencode")
    instruction = ctx.params.get("instruction") or (
        "Investigate this repository for the following request and report a concrete, "
        "concise plan for what needs to change — the root cause and evidence if it's a "
        "bug, or the exact files/functions and approach if it's a feature, edit, or "
        "refactor request. Do not change any code yet.\n\n"
        f"{ctx.prompt}"
    )
    result = await opencode(ctx, instruction)
    return {"root_cause": {"summary": result["text"], "evidence": []}}
