"""PR publication: a deterministic primary path (git subprocess + a
direct GitHub REST API call — tools/git_ops.py, tools/github_rest.py),
falling back to the original opencode + GitHub MCP flow only if that
fails. Everything the primary path needs (title, body, branch name, file
list) is already known by the time this runs — generate_change already
computed the summary and changed-files list — so there's normally nothing
for an LLM to decide, and no opencode subprocess/MCP round trip to pay
for. Confirmed live that the opencode+MCP route was the dominant cost of
this step.

The fallback is kept, not removed, because the MCP-driven route is more
flexible/comprehensive — an LLM can adapt to an unusual repo state, an
auth quirk, or anything else a fixed sequence of git/REST calls can't —
as a safety net for exactly those cases, not because the primary path is
expected to fail often.
"""

import re
from urllib.parse import urlsplit

from common.capabilities_base import StepContext
from ..protected_branches import is_protected, protected_branches

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_PR_URL_RE = re.compile(r"https://\S*/pull/\d+")


def _repo_slug(url: str) -> str:
    """`https://github.com/owner/repo(.git)` -> `owner/repo`."""
    path = urlsplit(url).path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path


def _branch_name(title: str, request_id: str) -> str:
    """No LLM on the primary path to invent a branch name, so it's
    derived deterministically: the title as a human-readable hint, plus a
    short slice of the request id so two requests against the same repo
    never collide on one branch name."""
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")[:40] or "change"
    return f"ari/{slug}-{request_id[:8]}"


def _extract_pr_from_opencode_result(result: dict) -> dict | None:
    """Same extraction the old opencode-only implementation used — only
    reached on the fallback path now. The github MCP tool call is the
    authoritative source for the PR url/branch; falls back to
    regex-scanning the response text if the tool-call shape doesn't match
    what's expected."""
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


async def _publish_via_opencode(
    ctx: StepContext, title: str, body: str, changed_files: list[str], repo_url: str, base_ref: str, primary_error: str
) -> dict:
    opencode = ctx.tool("opencode")
    files_clause = (
        f"Stage and commit exactly these files (nothing else — do not use `git add -A` or `git add .`, "
        f"other tools may have left unrelated scratch files in the working tree): {', '.join(changed_files)}.\n\n"
        if changed_files
        else ""
    )
    # Belt-and-suspenders alongside the bash-permission denies
    # tools/opencode_cli.py adds for these same branches — named here in
    # plain language too, since an instruction and a permission block
    # catch different failure modes (see protected_branches.py for why
    # neither alone is a hard guarantee for this fallback path).
    protected = protected_branches(repo_url)
    protected_clause = (
        f"Never push directly to {', '.join(sorted(protected))} in this repository under any "
        f"circumstance, no matter what — those branches are off-limits for a direct push. Always push "
        f"to a new branch instead.\n\n"
        if protected
        else ""
    )
    instruction = (
        f"Repository: {_repo_slug(repo_url)} ({repo_url}).\n\n"
        f"An automated attempt to publish this change directly failed ({primary_error}) and may have "
        f"partially completed. Check `git status`/`git log -3`/`git branch` first to see what's already "
        f"done (staged, committed, and/or pushed) and complete only what's still needed — do not redo "
        f"finished steps.\n\n"
        f"{protected_clause}"
        f"{files_clause}"
        f"The change is already made and verified — do not re-read, re-inspect, or re-diff these files "
        f"yourself; the summary given here is already everything you need. Get to a state where exactly "
        f"the listed files are committed, pushed to a new branch (e.g. ari/change-<short-slug>), and a "
        f"pull request is open in that repository against {base_ref} using the github MCP tools — do not "
        f"ask for the repository, it is given above. Title: {title!r}. Body: summarize the change "
        f"below.\n\n{body}"
    )
    result = await opencode(ctx, instruction)
    pr = _extract_pr_from_opencode_result(result)
    if not pr:
        # This message becomes the failure detail `respond` synthesizes the
        # final explanation from (see orchestration/workflow.py) — both the
        # primary path's error and opencode's own explanation of why the
        # fallback also couldn't find a PR url are worth keeping, not
        # cutting off mid-sentence.
        raise RuntimeError(
            f"publish_pr: primary path failed ({primary_error}); fallback also could not find a PR url: "
            f"{result['text'][:4000]}"
        )
    return {"pr": pr}


async def run(ctx: StepContext) -> dict:
    # Belt-and-suspenders alongside meta.py's default_step_policy (which
    # normally wouldn't even schedule this step when skip_pr is set) — in
    # case the planner includes it anyway despite an explicit opt-out.
    if ctx.config.get("options", {}).get("skip_pr"):
        return {"status": "skipped"}

    # Fails fast and clearly if the planner's step list omitted clone_repo
    # — see StepContext.require_cloned_repo's own docstring.
    repo_dir = ctx.require_cloned_repo()

    git_add = ctx.tool("git_add")
    git_commit = ctx.tool("git_commit")
    git_push = ctx.tool("git_push")
    create_pr = ctx.tool("github_create_pr")

    change = ctx.results.get("generate_change", {})
    changed_files = change.get("changed_files") or []
    if not changed_files:
        raise RuntimeError("publish_pr: generate_change reported no changed files to publish")

    # `change["title"]` is generate_change's own purpose-built title (a
    # short, complete phrase, never a truncated slice of the body) —
    # guaranteed present whenever changed_files is (see that capability's
    # own fallback). Confirmed live as a real bug this replaced: title and
    # body used to come from the exact same summary text at two different
    # lengths, so the title read as a sentence cut off mid-word (e.g.
    # insightfinder/insightfinder-mcp-server#55).
    title = ctx.params.get("title") or f"ARI: {change.get('title') or 'automated change'}"
    body = change.get("summary") or ctx.prompt
    repo = ctx.config["repo"]
    # Same fallback chain either way: the planner can override the PR's
    # target branch independently of whatever branch clone_repo actually
    # checked out ("check the repo in dev branch... raise a PR against
    # staging"), falling back to whichever branch clone_repo resolved.
    base_ref = ctx.params.get("base_ref") or ctx.results.get("clone_repo", {}).get("ref") or "main"
    branch = _branch_name(title, ctx.request_id)

    # Hard veto, checked before anything touches the remote — never push
    # directly to one of this repo's protected branches (see
    # protected_branches.py). Unreachable in practice today (`branch`
    # above is always a freshly generated `ari/...` name, never one of
    # these) — kept as defense-in-depth against a future change letting a
    # caller-supplied branch name reach this point. A hard stop, not
    # routed through the opencode fallback below: if the target branch
    # itself is the problem, a different mechanism must not be allowed to
    # "work around" that by trying anyway.
    if is_protected(repo["url"], branch):
        raise RuntimeError(
            f"publish_pr: refusing to push protected branch {branch!r} for {repo['url']} — see "
            f"configs/oncall/protected_branches.yaml"
        )

    try:
        await git_add(repo_dir, changed_files)
        await git_commit(repo_dir, title, body)
        await git_push(repo_dir, repo, branch)
        pr = await create_pr(repo, _repo_slug(repo["url"]), title, body, branch, base_ref)
        return {"pr": pr}
    except Exception as exc:
        return await _publish_via_opencode(ctx, title, body, changed_files, repo["url"], base_ref, str(exc))
