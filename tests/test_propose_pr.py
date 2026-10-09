import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_oncall_agent.capabilities import propose_pr, publish_pr  # noqa: E402
from common.capabilities_base import StepContext  # noqa: E402

_CLONE_REPO_RESULT = {"cloned_path": "/tmp/ws/repo", "ref": "main"}

_DIFF = """diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1,3 +1,3 @@
-old line
+new line
 context
diff --git a/b.py b/b.py
index 3333333..4444444 100644
--- a/b.py
+++ b/b.py
@@ -10,2 +10,3 @@
 keep
+added
"""


def _tools(**overrides):
    tools = {
        "git_add": AsyncMock(),
        "git_commit": AsyncMock(),
        "git_push": AsyncMock(),
        "github_create_pr": AsyncMock(return_value={"url": "https://github.com/x/y/pull/1", "branch": "b"}),
    }
    tools.update(overrides)
    return tools


def _ctx(tools, *, results=None, params=None, options=None, include_clone_repo=True) -> StepContext:
    results = results if results is not None else {
        "generate_change": {
            "title": "fix the cart bug",
            "summary": "the cart was empty",
            "root_cause": "items were never appended",
            "changed_files": ["a.py", "b.py"],
            "diff": _DIFF,
        }
    }
    if include_clone_repo and "clone_repo" not in results:
        results = {**results, "clone_repo": _CLONE_REPO_RESULT}
    return StepContext(
        request_id="req-12345678",
        workspace="/tmp/ws",
        prompt="fix the bug",
        config={
            "repo": {"url": "https://github.com/x/y", "ref": "main"},
            "llm": {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"},
            "options": options or {},
        },
        capability="propose_pr",
        params=params or {},
        results=results,
        tools=tools,
    )


# --- the pause itself ---------------------------------------------------


@pytest.mark.asyncio
async def test_pushes_the_branch_then_pauses_for_approval():
    tools = _tools()
    result = await propose_pr.run(_ctx(tools))

    tools["git_add"].assert_awaited_once()
    tools["git_commit"].assert_awaited_once()
    tools["git_push"].assert_awaited_once()
    assert result["requires_approval"] is True
    assert result["pending_action"]["branch_pushed"] is True


@pytest.mark.asyncio
async def test_never_opens_a_pull_request_itself():
    # The whole point of the gate. Opening the pull request is
    # publish_pr's job and only happens after a person confirms.
    tools = _tools()
    await propose_pr.run(_ctx(tools))
    tools["github_create_pr"].assert_not_awaited()


@pytest.mark.asyncio
async def test_the_branch_is_pushed_before_the_pause_not_after():
    # A regression guard for the design decision, not a style preference:
    # the resume runs in a different workflow execution, after
    # cleanup_workspace removed the sandbox. A version of this that
    # deferred the push to the approval would publish a change that no
    # longer exists on disk.
    tools = _tools()
    result = await propose_pr.run(_ctx(tools))

    pushed_branch = tools["git_push"].await_args.args[2]
    assert pushed_branch == result["pending_action"]["branch"]


# --- what the person is asked to approve --------------------------------


@pytest.mark.asyncio
async def test_proposal_carries_everything_publish_pr_will_need():
    # Ties the two halves together: publish_pr refuses a proposal missing
    # any of these, and it never recomputes them. If propose_pr ever stops
    # emitting one, every approval would fail at the far end of a
    # human-speed round trip -- a slow, confusing way to find out.
    proposal = (await propose_pr.run(_ctx(_tools())))["pending_action"]

    for field in ("id", "action", "repo", "branch", "base_ref", "title", "body"):
        assert proposal.get(field), f"proposal is missing {field!r}"
    assert proposal["action"] == "open_pull_request"


@pytest.mark.asyncio
async def test_proposal_names_the_action_for_the_approval_email():
    # Reported from stg, 2026-10-09: the first live approval email read
    # "Proposed action: Custom action". MCP Service's `approval_labels()`
    # (app/tasks/loop.py, origin/test) scans the pause's artifacts for
    # `data.action_name` and returns CUSTOM_ACTION_LABEL when no artifact
    # names the action -- and this proposal named none.
    #
    # Two conditions there, both asserted here: the name has to be
    # non-empty, and it must not be the agent's own display name, which
    # that function skips ("the agent's name is never sent as the
    # action"). `action` stays the machine identifier publish_pr checks;
    # this is the human label beside it.
    proposal = (await propose_pr.run(_ctx(_tools())))["pending_action"]

    name = proposal["action_name"]
    assert name.strip()
    assert name.strip().lower() != "ari on-call agent"


@pytest.mark.asyncio
async def test_proposal_id_is_unique_per_proposal():
    # executor.py's _find_pending_action and MCP Service's
    # pending_action_id() both resolve a confirmation by this id. Two
    # proposals sharing one would let a stale "yes" open the wrong PR.
    first = (await propose_pr.run(_ctx(_tools())))["pending_action"]["id"]
    second = (await propose_pr.run(_ctx(_tools())))["pending_action"]["id"]
    assert first and second and first != second


@pytest.mark.asyncio
async def test_proposal_includes_the_review_material_the_email_renders():
    proposal = (await propose_pr.run(_ctx(_tools())))["pending_action"]

    assert proposal["changed_files"] == ["a.py", "b.py"]
    assert proposal["root_cause"] == "items were never appended"
    assert "+new line" in proposal["key_hunks"]
    assert proposal["compare_url"] == "https://github.com/x/y/compare/main...{}".format(proposal["branch"])


@pytest.mark.asyncio
async def test_compare_url_drops_a_dot_git_suffix():
    tools = _tools()
    ctx = _ctx(tools)
    ctx.config["repo"]["url"] = "https://github.com/x/y.git"

    proposal = (await propose_pr.run(ctx))["pending_action"]

    assert proposal["compare_url"].startswith("https://github.com/x/y/compare/")


# --- key_hunks ----------------------------------------------------------


def test_key_hunks_keeps_the_edits_and_drops_the_noise():
    excerpt = propose_pr.key_hunks(_DIFF)

    assert "diff --git a/a.py b/a.py" in excerpt
    assert "+new line" in excerpt
    # Blob hashes and the ---/+++ pair repeat what the header already
    # says; dropping them buys room for real edits inside the budget.
    assert "index 1111111" not in excerpt
    assert "--- a/a.py" not in excerpt


def test_key_hunks_bounds_a_long_file():
    long_diff = "diff --git a/big.py b/big.py\n" + "\n".join(f"+line {i}" for i in range(500))
    excerpt = propose_pr.key_hunks(long_diff)

    assert len(excerpt.splitlines()) < 500
    assert "more lines in this file" in excerpt


def test_key_hunks_bounds_the_number_of_files():
    many = "".join(f"diff --git a/f{i}.py b/f{i}.py\n+change {i}\n" for i in range(40))
    excerpt = propose_pr.key_hunks(many)

    assert "more files changed" in excerpt
    assert len(excerpt) <= propose_pr._MAX_EXCERPT_CHARS + 200


def test_key_hunks_of_an_empty_diff_is_empty():
    assert propose_pr.key_hunks("") == ""
    assert propose_pr.key_hunks(None or "") == ""


# --- branches and opt-outs ----------------------------------------------


@pytest.mark.asyncio
async def test_base_ref_param_overrides_the_cloned_branch():
    proposal = (await propose_pr.run(_ctx(_tools(), params={"base_ref": "staging"})))["pending_action"]
    assert proposal["base_ref"] == "staging"


@pytest.mark.asyncio
async def test_base_ref_defaults_to_the_branch_clone_repo_resolved():
    tools = _tools()
    ctx = _ctx(tools)
    ctx.results["clone_repo"] = {"cloned_path": "/tmp/ws/repo", "ref": "develop"}

    proposal = (await propose_pr.run(ctx))["pending_action"]

    assert proposal["base_ref"] == "develop"


@pytest.mark.asyncio
async def test_skips_entirely_when_the_caller_asked_not_to_open_a_pr():
    # Nothing to approve either: pausing the task to ask about a pull
    # request the caller already declined would be worse than doing
    # nothing.
    tools = _tools()
    result = await propose_pr.run(_ctx(tools, options={"skip_pr": True}))

    assert result == {"status": "skipped"}
    assert "requires_approval" not in result
    tools["git_push"].assert_not_awaited()


@pytest.mark.asyncio
async def test_raises_when_generate_change_reported_no_changed_files():
    tools = _tools()
    ctx = _ctx(tools, results={"generate_change": {"summary": "nothing", "changed_files": []}})

    with pytest.raises(RuntimeError, match="no changed files"):
        await propose_pr.run(ctx)

    tools["git_push"].assert_not_awaited()


@pytest.mark.asyncio
async def test_refuses_a_protected_branch_without_touching_git(monkeypatch):
    # Hard veto, inherited from the single-step publish_pr this replaced.
    # Unreachable today (_branch_name always generates a fresh ari/...
    # name) and kept as defence in depth.
    monkeypatch.setattr(publish_pr, "is_protected", lambda repo_url, branch: True)
    tools = _tools()

    with pytest.raises(RuntimeError, match="refusing to push protected branch"):
        await propose_pr.run(_ctx(tools))

    tools["git_add"].assert_not_awaited()
    tools["git_push"].assert_not_awaited()
