"""Opening the pull request, once a person has approved it.

This step is the second half of a human-in-the-loop gate. `propose_pr`
decides what the pull request will be, pushes its branch, and pauses the
task; this step opens the pull request from that approved proposal and
nothing else. It is reached only through `plan_task`'s confirmation
bypass (`AgentMeta.confirm_capability`), never from a plan the LLM wrote
— see `docs/design.md` §5b.

**It deliberately recomputes nothing.** Every field comes from
`ctx.params`, which `plan_task` fills with the exact proposal artifact
the caller confirmed. Recomputing would reintroduce the one bug this
shape exists to prevent: `_branch_name` is derived from the request id,
the resume runs as a different workflow execution, and a freshly derived
branch name would not be the branch that was pushed and approved. What
was shown to a person is what gets opened.

**No opencode fallback here, unlike every earlier version of this file.**
The old fallback re-drove commit/push/PR through opencode + the GitHub
MCP server against the sandbox working tree. After the pause there is no
working tree — `workflow.py` cleans the workspace up on the way out of
the pausing run, and the sandbox root is not on a persistent volume — so
that path could not run even if it were still wired. Keeping code that
can never execute would be worse than removing it; the push it used to
guard now happens in `propose_pr`, while the sandbox still exists.
"""

import re
from urllib.parse import urlsplit

from common.capabilities_base import StepContext
from ..protected_branches import is_protected

_SLUG_RE = re.compile(r"[^a-z0-9]+")


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


def plan_pr(ctx: StepContext) -> dict:
    """Exactly the pull request this pipeline will open, decided once.

    Lives here rather than in `propose_pr` because this module owns what
    publication means, but it is called from `propose_pr` — the proposal
    a person approves and the pull request that is opened have to be the
    same thing, and the only way to guarantee that is for one piece of
    code to decide it. The result is carried through the approval in the
    proposal artifact; nothing downstream derives these values again.
    """
    change = ctx.results.get("generate_change", {})
    changed_files = change.get("changed_files") or []
    if not changed_files:
        raise RuntimeError("plan_pr: generate_change reported no changed files to publish")

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
    # The planner can override the PR's target branch independently of
    # whatever branch clone_repo actually checked out ("check the repo in
    # dev branch... raise a PR against staging"), falling back to
    # whichever branch clone_repo resolved.
    base_ref = ctx.params.get("base_ref") or ctx.results.get("clone_repo", {}).get("ref") or "main"
    branch = _branch_name(title, ctx.request_id)

    # Hard veto, checked before anything touches the remote — never push
    # directly to one of this repo's protected branches (see
    # protected_branches.py). Unreachable in practice today (`branch`
    # above is always a freshly generated `ari/...` name, never one of
    # these) — kept as defense-in-depth against a future change letting a
    # caller-supplied branch name reach this point.
    if is_protected(repo["url"], branch):
        raise RuntimeError(
            f"plan_pr: refusing to push protected branch {branch!r} for {repo['url']} — see "
            f"configs/oncall/protected_branches.yaml"
        )

    return {
        "title": title,
        "body": body,
        "branch": branch,
        "base_ref": base_ref,
        "changed_files": changed_files,
        "repo_slug": _repo_slug(repo["url"]),
        "repo_url": repo["url"],
    }


def _require_confirmed_proposal(ctx: StepContext) -> dict:
    """The approved proposal, or a loud failure.

    Two independent checks, for two different ways this could be wrong.
    `ctx.confirmed` is set only by `plan_task`'s bypass branch and lives
    outside `params` entirely, so the planner's own JSON cannot forge it
    (`common/models/plan.py`'s `Step` ignores unknown top-level fields) —
    that is the gate. The field check is about staleness and shape: a
    confirmation resolved against some other artifact, or against one
    from a version of this agent that proposed something different,
    should fail here rather than open a pull request from half a
    proposal.
    """
    if not ctx.confirmed:
        raise RuntimeError(
            "publish_pr: refusing to open a pull request that was not confirmed. This step runs "
            "only from an approved propose_pr proposal (plan_task's confirmation bypass); it is "
            "never something a plan should schedule directly."
        )

    missing = [k for k in ("branch", "base_ref", "title", "repo") if not ctx.params.get(k)]
    if ctx.params.get("action") != "open_pull_request" or missing:
        raise RuntimeError(
            f"publish_pr: the confirmed proposal is not an open_pull_request proposal this agent "
            f"made (action={ctx.params.get('action')!r}, missing fields: {missing or 'none'}). It "
            f"may be stale or from a different conversation — ask again and a fresh one is proposed."
        )
    return ctx.params


async def run(ctx: StepContext) -> dict:
    proposal = _require_confirmed_proposal(ctx)
    create_pr = ctx.tool("github_create_pr")

    # The branch is already on the remote — propose_pr pushed it before
    # pausing. All that is left is the pull request itself, which is one
    # REST call needing no working tree. `github_rest.create_pull_request`
    # is idempotent (it looks up an existing open PR for this head branch
    # before creating, and re-checks on 422), so a retry or a duplicate
    # confirmation returns the same pull request instead of failing.
    pr = await create_pr(
        ctx.config["repo"],
        proposal["repo"],
        proposal["title"],
        proposal["body"],
        proposal["branch"],
        proposal["base_ref"],
    )
    return {"pr": pr}
