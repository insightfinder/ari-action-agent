from common.capabilities_base import StepContext


async def run(ctx: StepContext) -> dict:
    ensure_cache_and_clone = ctx.tool("repo_cache_and_clone")
    set_origin = ctx.tool("git_set_origin")
    write_config = ctx.tool("opencode_config")

    repo = ctx.config["repo"]

    # The planner can override which branch to actually work from —
    # "check the repo in dev branch" → params.ref = "dev" — falling back to
    # the request's configured default when the prompt didn't name one, and
    # finally to whatever the remote itself reports as its default branch
    # (main/master/anything else) rather than assuming a fixed name. Refresh,
    # ref resolution and the local clone all happen atomically under one
    # held cache lock — see tools/repo_cache.py for why: a concurrent
    # request's cache refresh could otherwise delete/repack a pack file
    # this clone is still reading from.
    ref = ctx.params.get("ref") or repo.get("ref")
    path, ref = await ensure_cache_and_clone(repo, ref, ctx.workspace)

    # `origin` right after that clone points at the local cache dir, not
    # the real repo — fix that up so anything inspecting `git remote -v`
    # (a tool, or opencode itself) sees the actual upstream, not an
    # internal implementation detail of the caching layer.
    await set_origin(path, repo)

    # Every later opencode call (analyze/generate_change/verify, plus
    # publish_pr only if it falls back to opencode+MCP — see
    # tools/opencode_cli.py) runs with --dir <path>, so the per-request
    # opencode.json (LLM key, GitHub MCP server + PAT, scoped agents) has
    # to exist here before any of them run.
    write_config(path, ctx.config["llm"], repo)

    return {"cloned_path": path, "ref": ref}
