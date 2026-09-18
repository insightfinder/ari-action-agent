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
    change = ctx.results.get("generate_change", {})

    title = ctx.params.get("title") or f"ARI: {(change.get('summary') or ctx.prompt)[:72]}"
    changed_files = change.get("changed_files") or []
    files_clause = (
        f"Stage and commit exactly these files (nothing else — do not use `git add -A` or `git add .`, "
        f"other tools may have left unrelated scratch files in the working tree): {', '.join(changed_files)}.\n\n"
        if changed_files
        else ""
    )
    repo_url = ctx.config["repo"]["url"]
    # The planner can override the PR's target branch independently of
    # whatever branch clone_repo actually checked out — "raise a PR
    # against staging" while working from dev, say. Falls back to
    # whichever branch clone_repo actually resolved and cloned (itself
    # already resolved dynamically against the remote when nothing named
    # one), not a re-derived guess.
    base_ref = ctx.params.get("base_ref") or ctx.results.get("clone_repo", {}).get("ref") or "main"
    instruction = ctx.params.get("instruction") or (
        f"Repository: {_repo_slug(repo_url)} ({repo_url}).\n\n"
        f"{files_clause}"
        f"The change is already made and verified — do not re-read, re-inspect, or "
        f"re-diff these files yourself; the summary and file list given here are "
        f"already everything you need. Go straight to staging exactly the listed "
        f"files, committing, pushing to a new branch (e.g. ari/change-<short-slug>), "
        f"and opening a pull request in that repository against {base_ref} using the "
        f"github MCP tools — do not ask for the repository, it is given above. "
        f"Title: {title!r}. Body: summarize the change below.\n\n{change.get('summary', '')}"
    )
    result = await opencode(ctx, instruction)
    pr = _extract_pr(result)
    if not pr:
        # This message becomes the failure detail `respond` synthesizes the
        # final explanation from (see orchestration/workflow.py) — opencode's
        # own explanation of *why* (e.g. a specific 403 and whose token/scope
        # caused it) is exactly what the caller needs, so keep enough of it
        # to be useful rather than cutting it off mid-sentence at an
        # arbitrary length.
        raise RuntimeError(f"publish_pr: could not find a PR url in opencode's response: {result['text'][:4000]}")
    return {"pr": pr, "trace": result["trace"]}
