"""The human checkpoint before a pull request is opened.

`generate_change` has already made and captured the change by the time
this runs. This step pushes it to its own branch, assembles the pull
request exactly as `publish_pr` will open it, and then stops: it returns
`requires_approval`, which `orchestration/workflow.py`'s step loop turns
into `status: "needs_approval"` and `a2a_server/executor.py` maps to
`TASK_STATE_INPUT_REQUIRED`. The caller (ARI/MCP Service, which forwards
it to UIE's approval email) shows a person what is about to be opened and
sends back a structured `confirm_pending_action` to resume. See
`docs/INPUT_REQUIRED_INTEGRATION.md` for that wire contract and
`docs/design.md` §5b for the three-layer design it reuses.

**Why the branch is pushed here, before approval, rather than after it.**
The approval is human-speed — minutes to days — and the resume is a
*separate* Temporal workflow execution whose plan is a single
`publish_pr` step (`plan_task`'s bypass branch). Two things are therefore
gone by then: `workflow.py` runs `cleanup_workspace` on the way out of
every run including this pause, and the sandbox root is not on a
persistent volume at all (the chart persists only the repo and build
caches), so a worker restart during the approval window would take the
working tree with it. Anything that still needed `git add`/`commit`/
`push` after the pause would be publishing a change that no longer
exists on disk. Pushing now leaves the approval step as one idempotent
REST call that needs no working tree and survives any restart.

The cost, accepted deliberately: the branch is visible on the target
repository before anyone has approved it. That is far quieter than a
pull request — no review request, no notification to reviewers — but a
repository whose CI builds every pushed branch will build this one
before the approval. Weighed against silently losing the change to a pod
restart mid-approval, and against re-cloning and re-applying a patch
onto a base that may have moved, this was judged the lesser cost.
"""

import uuid
from datetime import datetime, timezone

from common.capabilities_base import StepContext

from .publish_pr import plan_pr

# A proposal carries enough of the change for a person to judge it in an
# email, not the whole diff. Full diffs on a real repo run to thousands of
# lines; the approval mail renders the agent's prose (the artifact itself
# never reaches it — see docs/INPUT_REQUIRED_INTEGRATION.md), so an
# unbounded diff would be both unreadable and, past the mail server's
# limits, undeliverable. The complete change is always one click away on
# the pushed branch, which is why `compare_url` is part of the proposal.
_MAX_FILES_IN_EXCERPT = 10
_MAX_LINES_PER_FILE = 40
_MAX_EXCERPT_CHARS = 12000


def key_hunks(diff: str) -> str:
    """The reviewable core of a unified diff: every file's header, and the
    leading edits under each, bounded per file and overall.

    Deterministic on purpose — no LLM. What a person approves has to be
    the change itself, not a model's description of it, and a summariser
    in this path would be one more thing that can quietly say something
    the diff does not.
    """
    if not diff:
        return ""

    files: list[list[str]] = []
    current: list[str] | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = [line]
            files.append(current)
            continue
        if current is None:
            continue
        # `index`/`---`/`+++` carry blob hashes and paths already visible
        # in the `diff --git` header; dropping them buys room for actual
        # edits within the per-file budget.
        if line.startswith(("index ", "--- ", "+++ ")):
            continue
        current.append(line)

    excerpt: list[str] = []
    for body in files[:_MAX_FILES_IN_EXCERPT]:
        kept = body[:_MAX_LINES_PER_FILE]
        excerpt.extend(kept)
        if len(body) > len(kept):
            excerpt.append(f"... ({len(body) - len(kept)} more lines in this file)")
    if len(files) > _MAX_FILES_IN_EXCERPT:
        excerpt.append(f"... ({len(files) - _MAX_FILES_IN_EXCERPT} more files changed)")

    text = "\n".join(excerpt)
    if len(text) > _MAX_EXCERPT_CHARS:
        text = text[:_MAX_EXCERPT_CHARS] + "\n... (excerpt truncated)"
    return text


def _compare_url(repo_url: str, base_ref: str, branch: str) -> str:
    """GitHub's own diff view for the pushed branch — the full change,
    for a reviewer who wants more than the excerpt."""
    return f"{repo_url.rstrip('/').removesuffix('.git')}/compare/{base_ref}...{branch}"


async def run(ctx: StepContext) -> dict:
    # Mirrors publish_pr's own guard. `skip_pr` means the caller asked for
    # the change without shipping it, and there is then nothing to approve
    # either — pausing the task to ask about a pull request nobody wants
    # would be worse than doing nothing.
    if ctx.config.get("options", {}).get("skip_pr"):
        return {"status": "skipped"}

    repo_dir = ctx.require_cloned_repo()
    plan = plan_pr(ctx)

    git_add = ctx.tool("git_add")
    git_commit = ctx.tool("git_commit")
    git_push = ctx.tool("git_push")

    await git_add(repo_dir, plan["changed_files"])
    await git_commit(repo_dir, plan["title"], plan["body"])
    await git_push(repo_dir, ctx.config["repo"], plan["branch"])

    change = ctx.results.get("generate_change", {})
    # `id` is what the resume confirms against. `executor.py`'s
    # `_find_pending_action` scans this task's published artifacts for a
    # data part carrying this exact value, and MCP Service's own
    # `pending_action_id()` reads "the first artifact that carries an id"
    # — so this must be the only artifact of this run with an `id`, which
    # is why meta.py lists it first.
    pending_action = {
        "id": str(uuid.uuid4()),
        "action": "open_pull_request",
        # What the approver reads in UIE's email under "Proposed action".
        # MCP's `approval_labels()` scans this artifact for `action_name` and
        # falls back to "Custom action" when nothing names the action — which
        # is exactly what the first live approval email said (reported from
        # stg, 2026-10-09). `action` above is the machine identifier
        # `publish_pr` checks; this is the human sentence beside it, the same
        # split the action agent's own `pending_action` uses.
        #
        # Deliberately the kind of action, not the pull request's title: the
        # title, root cause, changed files and key hunks are already in the
        # body of that email, and this is a one-line label in a table.
        "action_name": "Open pull request",
        "repo": plan["repo_slug"],
        "repo_url": plan["repo_url"],
        "branch": plan["branch"],
        "base_ref": plan["base_ref"],
        "title": plan["title"],
        "body": plan["body"],
        "changed_files": plan["changed_files"],
        "root_cause": change.get("root_cause") or "",
        "key_hunks": key_hunks(change.get("diff") or ""),
        "compare_url": _compare_url(plan["repo_url"], plan["base_ref"], plan["branch"]),
        # Said plainly rather than left to be inferred from the state: the
        # branch exists on the remote already, the pull request does not.
        "branch_pushed": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "pending_action": pending_action,
        # Read by workflow.py's step loop to pause the task. Always true
        # here: this capability exists only to ask, and a version of it
        # that sometimes didn't would be a gate with a hole in it.
        "requires_approval": True,
    }
