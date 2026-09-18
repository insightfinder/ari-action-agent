from .base import StepContext


async def run(ctx: StepContext) -> dict:
    opencode = ctx.tool("opencode")
    instruction = ctx.params.get("instruction") or (
        "Investigate this repository for the following request and report a concrete, "
        "concise plan for what needs to change — the root cause and evidence if it's a "
        "bug, or the exact files/functions and approach if it's a feature, edit, or "
        "refactor request. This may be a large monorepo with many unrelated modules — "
        "if the request names a specific module/component/directory, scope your search "
        "to it first and only look elsewhere if something it directly imports/references "
        "genuinely requires it. Search efficiently: consolidate related terms into one "
        "regex search rather than issuing several overlapping single-term searches. Do "
        "not change any code yet.\n\n"
        f"{ctx.prompt}"
    )
    result = await opencode(ctx, instruction)
    return {"root_cause": {"summary": result["text"], "evidence": []}}
