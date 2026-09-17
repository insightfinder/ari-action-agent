from .base import StepContext


async def run(ctx: StepContext) -> dict:
    ensure_cache = ctx.tool("repo_cache")
    clone = ctx.tool("git_clone")
    set_origin = ctx.tool("git_set_origin")
    write_config = ctx.tool("opencode_config")
    get_default_branch = ctx.tool("git_default_branch")

    repo = ctx.config["repo"]
    cache_dir = await ensure_cache(repo)

    # The planner can override which branch to actually work from —
    # "check the repo in dev branch" → params.ref = "dev" — falling back to
    # the request's configured default when the prompt didn't name one, and
    # finally to whatever the remote itself reports as its default branch
    # (main/master/anything else) rather than assuming a fixed name.
    ref = ctx.params.get("ref") or repo.get("ref") or await get_default_branch(cache_dir)

    # Per-request working copy: a fast local clone FROM the shared,
    # already-fresh cache — no credentials needed for this step (it's a
    # plain filesystem clone), and fully independent of every other
    # request sharing the same cache, so concurrent edits never collide.
    path = await clone({"url": cache_dir, "ref": ref}, ctx.workspace)

    # `origin` right after that clone points at the local cache dir, not
    # the real repo — fix that up so anything inspecting `git remote -v`
    # (a tool, or opencode itself) sees the actual upstream, not an
    # internal implementation detail of the caching layer.
    await set_origin(path, repo)

    # Every later opencode call (analyze/generate_fix/verify/publish_pr)
    # runs with --dir <path>, so the per-request opencode.json (LLM key,
    # GitHub MCP server + PAT, scoped agents) has to exist here before any
    # of them run.
    write_config(path, ctx.config["llm"], repo)

    return {"cloned_path": path, "ref": ref}
