import re
from urllib.parse import urlsplit

from .base import StepContext

_PR_URL_RE = re.compile(r"https://\S*/pull/\d+")


def _repo_slug(url: str) -> str:
    """`https://github.com/owner/repo(.git)` -> `owner/repo`. Live testing
    found opencode won't infer this from the working tree's git remote on
    its own — asked for clarification instead of just running `git remote
    -v` — so publish_pr has to state it explicitly rather than assume."""
    path = urlsplit(url).path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path


def _extract_pr(result: dict) -> dict | None:
    """The github MCP tool call (agent=ari-publish) is the authoritative
    source for the PR url/branch — pull it from the tool_calls the agent
    made rather than trusting free-text. Falls back to regex-scanning the
    response text if the tool-call shape doesn't match what we expect
    (see tools/opencode_cli.py's parsing caveat)."""
    for call in result.get("tool_calls", []):
        if call.get("tool", "").startswith("github_") and "pull_request" in call.get("tool", ""):
            output = call.get("output")
            if isinstance(output, dict) and output.get("html_url"):
                return {"url": output["html_url"], "branch": (call.get("input") or {}).get("head", "")}
            if isinstance(output, str):
                match = _PR_URL_RE.search(output)
                if match:
                    return {"url": match.group(0), "branch": (call.get("input") or {}).get("head", "")}

    match = _PR_URL_RE.search(result.get("text", ""))
    if match:
        return {"url": match.group(0), "branch": ""}
    return None


async def run(ctx: StepContext) -> dict:
    opencode = ctx.tool("opencode")
    fix = ctx.results.get("generate_fix", {})

    title = ctx.params.get("title") or f"ARI fix: {(fix.get('summary') or ctx.prompt)[:72]}"
    changed_files = fix.get("changed_files") or []
    files_clause = (
        f"Stage and commit exactly these files (nothing else — do not use `git add -A` or `git add .`, "
        f"other tools may have left unrelated scratch files in the working tree): {', '.join(changed_files)}.\n\n"
        if changed_files
        else ""
    )
    repo_url = ctx.config["repo"]["url"]
    # The planner can override the PR's target branch independently of
    # whatever branch clone_repo actually checked out — "raise a PR
    # against staging" while working from dev, say. Falls back to the
    # same default clone_repo would use.
    base_ref = ctx.params.get("base_ref") or ctx.config["repo"].get("ref", "main")
    instruction = ctx.params.get("instruction") or (
        f"Repository: {_repo_slug(repo_url)} ({repo_url}).\n\n"
        f"{files_clause}"
        f"Push the committed changes to a new branch (e.g. ari/fix-<short-slug>) and open a "
        f"pull request in that repository against {base_ref} "
        f"using the github MCP tools — do not ask for the repository, it is given above. "
        f"Title: {title!r}. Body: summarize the fix below.\n\n{fix.get('summary', '')}"
    )
    result = await opencode(ctx, instruction)
    pr = _extract_pr(result)
    if not pr:
        raise RuntimeError(f"publish_pr: could not find a PR url in opencode's response: {result['text'][:500]}")
    return {"pr": pr}
