from .base import StepContext


async def run(ctx: StepContext) -> dict:
    opencode = ctx.tool("opencode")
    git_diff = ctx.tool("git_diff")

    analysis = ctx.results.get("analyze", {}).get("root_cause", {}).get("summary")
    prior_failure = ctx.results.get("verify", {}).get("failure")

    instruction = ctx.params.get("instruction") or (
        f"Make the following change to this repository — a bug fix, a feature, a "
        f"refactor, or a specific edit, whatever it actually asks for.\n\n"
        f"Original request: {ctx.prompt}\n\n"
        + (f"Prior analysis: {analysis}\n\n" if analysis else "")
        + (f"The previous attempt failed verification with:\n{prior_failure}\n\n" if prior_failure else "")
        + "Make the smallest change that fully satisfies the request and explain what "
          "you changed and why."
    )
    result = await opencode(ctx, instruction)
    diff = await git_diff(ctx.repo_dir)
    changed_files = await ctx.tool("git_changed_files")(ctx.repo_dir)
    return {"summary": result["text"], "diff": diff, "changed_files": changed_files}
