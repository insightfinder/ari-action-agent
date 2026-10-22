import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_oncall_agent.capabilities import (  # noqa: E402
    clone_repo,
    generate_change as opencode_change,
    propose_pr,
    publish_pr,
    verify,
)
from common.capabilities_base import StepContext  # noqa: E402

# Stub `run_opencode()`'s trace field (tools/opencode_cli.py) — every
# capability reads `result["trace"]` unconditionally now, so any mocked
# "opencode" tool return value needs one present, even when a test has
# nothing to assert about it.
_STUB_TRACE = {"elapsed_s": 0.0, "tool_call_counts": {}, "tool_call_total": 0, "slowest_gaps": []}


def _change_tools(opencode, diff: str, changed: list[str], untracked: list[list[str]] | None = None):
    """The tool set generate_change asks for. `untracked` is the sequence of
    `git_untracked_files` results — it is called once before opencode and
    once after, so a two-element list expresses "this appeared during the
    call" (see git_ops.intent_to_add for why that difference matters)."""
    return {
        "opencode": opencode,
        "git_diff": AsyncMock(return_value=diff),
        "git_changed_files": AsyncMock(return_value=changed),
        "git_untracked_files": AsyncMock(side_effect=list(untracked or [[], []])),
        "git_intent_to_add": AsyncMock(return_value=None),
    }


_CLONE_REPO_RESULT = {"cloned_path": "/tmp/ws/repo", "ref": "main"}


def _ctx(
    results: dict,
    tools: dict,
    params: dict | None = None,
    capability: str = "propose_pr",
    include_clone_repo: bool = True,
    confirmed: bool = False,
) -> StepContext:
    # generate_change/verify/publish_pr all now hard-require clone_repo to
    # have run first (StepContext.require_cloned_repo) -- included by
    # default so every existing test doesn't have to spell this out, same
    # as before that guard existed. clone_repo's own tests (which
    # legitimately have no prior clone_repo result) pass
    # include_clone_repo=False; so does anything specifically testing the
    # guard itself.
    if include_clone_repo and capability != "clone_repo" and "clone_repo" not in results:
        results = {**results, "clone_repo": _CLONE_REPO_RESULT}
    return StepContext(
        request_id="req-1",
        workspace="/tmp/ws",
        prompt="fix the bug",
        config={
            "repo": {"url": "https://github.com/x/y", "ref": "main"},
            "llm": {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"},
        },
        capability=capability,
        params=params or {},
        results=results,
        tools=tools,
        confirmed=confirmed,
    )


def _publish_pr_tools(**overrides):
    tools = {
        "git_add": AsyncMock(),
        "git_commit": AsyncMock(),
        "git_push": AsyncMock(),
        "github_create_pr": AsyncMock(return_value={"url": "https://github.com/x/y/pull/1", "branch": "ari/x"}),
        "opencode": AsyncMock(return_value={"text": "not reached", "tool_calls": []}),
    }
    tools.update(overrides)
    return tools


def test_repo_slug_strips_dot_git_suffix():
    assert publish_pr._repo_slug("https://github.com/neyaadeez/go-script") == "neyaadeez/go-script"
    assert publish_pr._repo_slug("https://github.com/neyaadeez/go-script.git") == "neyaadeez/go-script"


def test_branch_name_is_deterministic_and_collision_resistant():
    # No LLM on the primary path to invent this -- same title + request id
    # must always produce the same branch, and different requests against
    # the same repo must never collide.
    name = publish_pr._branch_name("ARI: fix the cart bug", "req-12345678")
    assert name == publish_pr._branch_name("ARI: fix the cart bug", "req-12345678")
    assert name.startswith("ari/")
    assert "req-1234" in name  # first 8 chars of the request id
    assert " " not in name


def _approved_proposal(**overrides) -> dict:
    """What plan_task puts in the step's params when a caller confirms a
    propose_pr proposal: the artifact itself, verbatim."""
    proposal = {
        "id": "11111111-2222-3333-4444-555555555555",
        "action": "open_pull_request",
        "repo": "x/y",
        "repo_url": "https://github.com/x/y",
        "branch": "ari/fix-the-cart-bug-req-1234",
        "base_ref": "main",
        "title": "ARI: fix the cart bug",
        "body": "the cart was empty",
        "changed_files": ["a.py"],
    }
    proposal.update(overrides)
    return proposal


@pytest.mark.asyncio
async def test_publish_pr_refuses_a_step_that_was_not_confirmed():
    # The gate itself. `confirmed` is set only by plan_task's confirmation
    # bypass and lives outside `params`, so a planner that scheduled this
    # step anyway -- or a future default_step_policy appending it by
    # mistake -- must not reach GitHub.
    tools = _publish_pr_tools()
    ctx = _ctx(results={}, tools=tools, params=_approved_proposal(), capability="publish_pr")

    with pytest.raises(RuntimeError, match="was not confirmed"):
        await publish_pr.run(ctx)

    tools["github_create_pr"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"action": "trigger_build", "job": "deploy"},
        _approved_proposal(branch=""),
        _approved_proposal(title=""),
    ],
    ids=["empty", "another-agents-proposal", "no-branch", "no-title"],
)
async def test_publish_pr_refuses_a_confirmation_that_is_not_one_of_its_own_proposals(params):
    # Being confirmed is not enough: the resolved artifact has to be an
    # open_pull_request proposal with everything a pull request needs.
    # A stale one, or one matched against some other artifact, fails here
    # rather than opening half a pull request.
    tools = _publish_pr_tools()
    ctx = _ctx(results={}, tools=tools, params=params, capability="publish_pr", confirmed=True)

    with pytest.raises(RuntimeError, match="not an open_pull_request proposal"):
        await publish_pr.run(ctx)

    tools["github_create_pr"].assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_pr_opens_the_pr_from_the_confirmed_proposal_verbatim():
    # What a person approved is what gets opened. Nothing is recomputed --
    # `_branch_name` is derived from the request id and the resume runs as
    # a different workflow execution, so recomputing would open a pull
    # request for a branch nobody approved and nothing pushed.
    tools = _publish_pr_tools()
    proposal = _approved_proposal()
    ctx = _ctx(results={}, tools=tools, params=proposal, capability="publish_pr", confirmed=True)

    result = await publish_pr.run(ctx)

    assert result == {"pr": {"url": "https://github.com/x/y/pull/1", "branch": "ari/x"}}
    args = tools["github_create_pr"].await_args.args
    assert args[1:] == (
        "x/y",
        "ARI: fix the cart bug",
        "the cart was empty",
        "ari/fix-the-cart-bug-req-1234",
        "main",
    )


@pytest.mark.asyncio
async def test_publish_pr_needs_no_repo_working_tree():
    # The point of splitting publish_pr out of propose_pr. The resume runs
    # after `cleanup_workspace` removed the sandbox, in a workflow whose
    # plan is this one step -- there is no clone_repo result and no
    # checked-out repo. If this step ever regained a require_cloned_repo()
    # or a git call, every approval would fail here instead.
    tools = _publish_pr_tools()
    ctx = _ctx(
        results={},
        tools=tools,
        params=_approved_proposal(),
        capability="publish_pr",
        include_clone_repo=False,
        confirmed=True,
    )

    await publish_pr.run(ctx)

    tools["github_create_pr"].assert_awaited_once()
    tools["git_add"].assert_not_awaited()
    tools["git_push"].assert_not_awaited()


def _clone_repo_tools(**overrides):
    tools = {
        # (cloned_path, resolved_ref) — ensure_cache_and_clone's actual
        # return shape (see tools/repo_cache.py). A single atomic tool now,
        # not separate repo_cache/git_default_branch/git_clone calls — see
        # the module docstring for why (a real cache-corruption race).
        "repo_cache_and_clone": AsyncMock(return_value=("/tmp/ws/repo", "main")),
        "git_set_origin": AsyncMock(),
        "opencode_config": Mock(),  # called synchronously (not awaited) in clone_repo.py
    }
    tools.update(overrides)
    return tools


@pytest.mark.asyncio
async def test_clone_repo_ref_param_overrides_repo_default():
    tools = _clone_repo_tools(repo_cache_and_clone=AsyncMock(return_value=("/tmp/ws/repo", "dev")))
    ctx = _ctx(results={}, tools=tools, params={"ref": "dev"}, capability="clone_repo")

    result = await clone_repo.run(ctx)

    tools["repo_cache_and_clone"].assert_awaited_once_with(
        {"url": "https://github.com/x/y", "ref": "main"}, "dev", "/tmp/ws"
    )
    tools["git_set_origin"].assert_awaited_once()
    assert result == {"cloned_path": "/tmp/ws/repo", "ref": "dev"}


@pytest.mark.asyncio
async def test_clone_repo_ref_defaults_to_repo_config_when_unset():
    tools = _clone_repo_tools()
    ctx = _ctx(results={}, tools=tools, capability="clone_repo")

    result = await clone_repo.run(ctx)

    tools["repo_cache_and_clone"].assert_awaited_once_with(
        {"url": "https://github.com/x/y", "ref": "main"}, "main", "/tmp/ws"
    )
    assert result["ref"] == "main"


@pytest.mark.asyncio
async def test_clone_repo_ref_resolves_from_remote_default_when_nothing_named():
    # Neither the planner nor the request config named a branch — must not
    # assume "main"; ensure_cache_and_clone itself resolves the remote's
    # actual default branch when given `ref=None`.
    tools = _clone_repo_tools(repo_cache_and_clone=AsyncMock(return_value=("/tmp/ws/repo", "master")))
    ctx = _ctx(results={}, tools=tools, capability="clone_repo")
    ctx.config["repo"] = {"url": "https://github.com/x/y"}  # no "ref" key at all

    result = await clone_repo.run(ctx)

    tools["repo_cache_and_clone"].assert_awaited_once_with({"url": "https://github.com/x/y"}, None, "/tmp/ws")
    assert result["ref"] == "master"


def test_extract_summary_returns_everything_after_the_last_marker():
    text = "Now let's edit models.go.\nLet's fix gofmt.\nSUMMARY:\nRemoved the Zone field."
    assert opencode_change._extract_summary(text) == "Removed the Zone field."


def test_extract_summary_returns_none_when_marker_absent():
    # None (not the full text) is deliberate: the caller treats this as
    # "issue an enforcement follow-up" rather than silently accepting raw
    # narration as PR-facing text — see opencode_change.run().
    text = "Just did the thing, no marker here."
    assert opencode_change._extract_summary(text) is None


def test_extract_summary_uses_the_last_marker_if_the_word_appears_earlier():
    # Guards against the agent mentioning "summary" in its own narration
    # before actually reaching the real, marked summary.
    text = "I'll write a SUMMARY: section once I'm done.\nSUMMARY:\nReal summary here."
    assert opencode_change._extract_summary(text) == "Real summary here."


@pytest.mark.asyncio
async def test_generate_change_returns_only_the_marked_summary_not_raw_narration():
    # This is the actual bug found live: without extraction, this raw text
    # (narration + tool-use commentary + the real summary all concatenated)
    # became the literal PR title/body via publish_pr.py.
    opencode = AsyncMock(
        return_value={
            "text": "Now let's edit models.go.\nDoing the edit now.\nSUMMARY:\nRemoved the Zone field.",
            "trace": _STUB_TRACE,
        }
    )
    ctx = _ctx(
        results={},
        tools=_change_tools(opencode, "diff --git a/models.go b/models.go", ["models.go"]),
        capability="generate_change",
    )

    result = await opencode_change.run(ctx)

    assert result["summary"] == "Removed the Zone field."


@pytest.mark.asyncio
async def test_generate_change_also_returns_root_cause_from_the_same_summary():
    # analyze used to be a separate capability/opencode session that
    # produced this artifact; now generate_change investigates and edits
    # in one call, so it has to produce root_cause itself too (see
    # agent_meta.artifacts: "root_cause": "generate_change.root_cause").
    opencode = AsyncMock(
        return_value={"text": "Found it in models.go.\nSUMMARY:\nZone field was never cleared.", "trace": _STUB_TRACE}
    )
    ctx = _ctx(
        results={},
        tools=_change_tools(opencode, "diff", ["models.go"]),
        capability="generate_change",
    )

    result = await opencode_change.run(ctx)

    assert result["root_cause"] == {"summary": "Zone field was never cleared.", "evidence": []}
    assert result["summary"] == "Zone field was never cleared."


@pytest.mark.asyncio
async def test_generate_change_raises_clearly_when_clone_repo_never_ran():
    # Regression test for a live failure: a planner (a smaller/cheaper
    # model) produced a step list with generate_change but no clone_repo.
    # Must fail immediately and clearly, not hand opencode an empty,
    # non-git --dir and get an opaque crash three layers down.
    opencode = AsyncMock()
    ctx = _ctx(results={}, tools={"opencode": opencode}, capability="generate_change", include_clone_repo=False)

    with pytest.raises(RuntimeError, match="clone_repo must run before this step"):
        await opencode_change.run(ctx)

    opencode.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_raises_clearly_when_clone_repo_never_ran():
    opencode = AsyncMock()
    ctx = _ctx(results={}, tools={"opencode": opencode}, capability="verify", include_clone_repo=False)

    with pytest.raises(RuntimeError, match="clone_repo must run before this step"):
        await verify.run(ctx)

    opencode.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, status",
    [
        ("All 42 tests passed.\nRESULT: PASS", "passed"),
        ("ok\nRESULT:PASS", "passed"),
        ("ok\n**RESULT: PASS**", "passed"),
        ("ok\nresult: pass", "passed"),
        ("Asked to end with RESULT: PASS if green.\n2 tests failed.\nRESULT: FAIL\nAssertionError", "failed"),
        ("mvn test exited 1\nRESULT: FAIL", "failed"),
        ("I ran the build but it timed out before finishing.", "inconclusive"),
    ],
)
async def test_verify_reads_only_the_final_verdict(text, status):
    opencode = AsyncMock(return_value={"text": text, "trace": _STUB_TRACE})
    ctx = _ctx(results={}, tools={"opencode": opencode}, capability="verify")

    result = await verify.run(ctx)

    assert result["status"] == status
    if status != "passed":
        assert result["failure"] == text


@pytest.mark.asyncio
async def test_propose_pr_raises_clearly_when_clone_repo_never_ran():
    # The repo guard moved here with the push itself. publish_pr has the
    # opposite requirement now -- see
    # test_publish_pr_needs_no_repo_working_tree.
    tools = _publish_pr_tools()
    ctx = _ctx(
        results={"generate_change": {"summary": "fixed it", "changed_files": ["a.py"]}},
        tools=tools,
        include_clone_repo=False,
    )

    with pytest.raises(RuntimeError, match="clone_repo must run before this step"):
        await propose_pr.run(ctx)

    tools["git_add"].assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_change_issues_enforcement_followup_when_marker_missing():
    # Real bug found live (against positron-agent): opencode doesn't always
    # comply with the marker instruction. Falling back to the raw
    # narration would silently reintroduce the exact bug the marker exists
    # to fix — a follow-up call must be made instead.
    opencode = AsyncMock(
        side_effect=[
            {
                "text": "Now let's edit models.go.\nDoing the edit now.\nAll done, changes look good.",
                "trace": _STUB_TRACE,
            },
            {"text": "Removed the Zone field and hardcoded the component name.", "trace": _STUB_TRACE},
        ]
    )
    ctx = _ctx(
        results={},
        tools=_change_tools(opencode, "diff --git a/models.go b/models.go", ["models.go"]),
        capability="generate_change",
    )

    result = await opencode_change.run(ctx)

    assert result["summary"] == "Removed the Zone field and hardcoded the component name."
    assert opencode.await_count == 2
    # The follow-up must continue the same session (it needs to know what
    # was just changed) — from ctx.results' point of view this capability
    # hasn't "finished" yet, so the automatic --continue detection alone
    # would otherwise start a fresh, context-less session.
    _, followup_kwargs = opencode.await_args_list[1]
    assert followup_kwargs == {"continue_session": True}


@pytest.mark.asyncio
async def test_generate_change_refuses_to_report_success_on_an_unmodified_tree():
    # The live failure this guards (stg workflow 9b32a817, 2026-10-08):
    # opencode made one tool call, which was invalid, returned in 20s
    # having edited nothing, and handed back a confident, fabricated
    # summary. Without this guard that empty change flowed on to `verify`,
    # which spent its full 480s building a change that did not exist,
    # looped back here on `on_failure`, and did it all again — 45 minutes
    # to discover nothing happened in the first 20 seconds.
    opencode = AsyncMock(
        return_value={
            "text": "TITLE: Add retry logic\nSUMMARY:\nThe service now includes exponential backoff.",
            "trace": {**_STUB_TRACE, "tool_call_counts": {"invalid": 1}, "tool_call_total": 1},
        }
    )
    ctx = _ctx(
        results={},
        tools=_change_tools(opencode, "", []),
        capability="generate_change",
    )

    with pytest.raises(RuntimeError) as exc_info:
        await opencode_change.run(ctx)

    message = str(exc_info.value)
    assert "the working tree is unmodified" in message
    # The two things a human needs to tell "the model did nothing" apart
    # from "the sandbox is broken": what it actually called, and what it
    # said about it.
    assert "invalid" in message, "must report the tool calls that were made"
    assert "exponential backoff" in message, "must quote the account it could not back up"


@pytest.mark.asyncio
async def test_generate_change_accepts_a_tree_with_changes():
    # Contrast case for the guard above — the normal path must be
    # untouched by it.
    opencode = AsyncMock(
        return_value={"text": "TITLE: Fix it\nSUMMARY:\nFixed the thing.", "trace": _STUB_TRACE}
    )
    ctx = _ctx(
        results={},
        tools=_change_tools(opencode, "diff --git a/models.go b/models.go", ["models.go"]),
        capability="generate_change",
    )

    result = await opencode_change.run(ctx)

    assert result["changed_files"] == ["models.go"]


@pytest.mark.asyncio
async def test_generate_change_still_fails_on_an_empty_tree_after_a_verify_loopback():
    # The on_failure loop re-runs this capability in the *same* working
    # tree, so an earlier attempt's edits are still uncommitted there. A
    # clean tree on the second pass therefore means nothing was ever done,
    # not "nothing new was done" — the guard must not be relaxed for it.
    opencode = AsyncMock(
        return_value={"text": "TITLE: Fix it\nSUMMARY:\nFixed the thing.", "trace": _STUB_TRACE}
    )
    ctx = _ctx(
        results={"verify": {"status": "failed", "failure": "tests failed"}},
        tools=_change_tools(opencode, "", []),
        capability="generate_change",
    )

    with pytest.raises(RuntimeError, match="the working tree is unmodified"):
        await opencode_change.run(ctx)


@pytest.mark.asyncio
async def test_generate_change_stages_a_brand_new_file_so_the_diff_can_see_it():
    # The live bug this closes (test, 2026-10-09, workflow 979a2b0e):
    # asked for a CONTRIBUTING.md the repository did not have, opencode
    # called `write`, the file was created — and because `git diff HEAD`
    # cannot see an untracked path, the pipeline reported the working tree
    # unmodified and failed. Every change whose whole content is a new
    # file was unshippable.
    opencode = AsyncMock(
        return_value={"text": "TITLE: Add guide\nSUMMARY:\nAdded CONTRIBUTING.md.", "trace": _STUB_TRACE}
    )
    tools = _change_tools(
        opencode,
        "diff --git a/CONTRIBUTING.md b/CONTRIBUTING.md",
        ["CONTRIBUTING.md"],
        untracked=[[], ["CONTRIBUTING.md"]],
    )
    ctx = _ctx(results={}, tools=tools, capability="generate_change")

    result = await opencode_change.run(ctx)

    tools["git_intent_to_add"].assert_awaited_once_with(ctx.repo_dir, ["CONTRIBUTING.md"])
    assert result["changed_files"] == ["CONTRIBUTING.md"]
    # Staging has to happen before the diff is taken, or the diff that
    # propose_pr turns into the approval email's key hunks is still blank
    # and a human approves a file they were never shown.
    assert tools["git_intent_to_add"].await_count == 1
    assert tools["git_diff"].await_count == 1


@pytest.mark.asyncio
async def test_generate_change_does_not_stage_files_it_did_not_create():
    # A file already untracked before opencode ran is not part of this
    # change. On an on_failure loop back from `verify` that is exactly what
    # verify's own scratch files are, and sweeping them in would put a
    # throwaway test script into the pull request.
    opencode = AsyncMock(
        return_value={"text": "TITLE: Fix it\nSUMMARY:\nFixed it.", "trace": _STUB_TRACE}
    )
    tools = _change_tools(
        opencode,
        "diff --git a/app.py b/app.py",
        ["app.py"],
        untracked=[["verify_scratch.py"], ["verify_scratch.py"]],
    )
    ctx = _ctx(
        results={"verify": {"status": "failed", "failure": "tests failed"}},
        tools=tools,
        capability="generate_change",
    )

    result = await opencode_change.run(ctx)

    tools["git_intent_to_add"].assert_awaited_once_with(ctx.repo_dir, [])
    assert result["changed_files"] == ["app.py"]


@pytest.mark.asyncio
async def test_generate_change_stages_only_the_newly_created_file_among_untracked_ones():
    # The discriminating case for the before/after snapshot: one scratch
    # file was already there, one real file appeared during this call.
    opencode = AsyncMock(
        return_value={"text": "TITLE: Add test\nSUMMARY:\nAdded a test.", "trace": _STUB_TRACE}
    )
    tools = _change_tools(
        opencode,
        "diff --git a/tests/test_new.py b/tests/test_new.py",
        ["tests/test_new.py"],
        untracked=[["verify_scratch.py"], ["verify_scratch.py", "tests/test_new.py"]],
    )
    ctx = _ctx(results={}, tools=tools, capability="generate_change")

    await opencode_change.run(ctx)

    tools["git_intent_to_add"].assert_awaited_once_with(ctx.repo_dir, ["tests/test_new.py"])


@pytest.mark.asyncio
async def test_the_empty_tree_message_does_not_accuse_an_honest_model_of_lying():
    # Two different things reach this guard and the message has to be true
    # of both. Seen live on stg (2026-10-09) one hour after the fabricated
    # case shipped: opencode read the repo, correctly concluded nothing
    # needed changing, and said so plainly. The first wording opened with
    # "opencode reported a change but left the working tree unmodified",
    # which is simply false here and sends whoever reads the failure
    # hunting for a lying model that is not there.
    #
    # Failing is still right -- telling the two apart would mean trusting
    # the model's account of itself, which is what this guard exists
    # because we cannot do -- but the message must only state what was
    # observed.
    honest = "Verified that a README.md file exists in the repository root. No changes were required."
    opencode = AsyncMock(
        return_value={
            "text": "TITLE: No change needed\nSUMMARY:\n" + honest,
            "trace": {**_STUB_TRACE, "tool_call_counts": {"read": 1}, "tool_call_total": 1},
        }
    )
    ctx = _ctx(results={}, tools=_change_tools(opencode, "", []), capability="generate_change")

    with pytest.raises(RuntimeError) as exc_info:
        await opencode_change.run(ctx)

    message = str(exc_info.value)
    assert "the working tree is unmodified" in message
    assert honest in message, "must quote the account verbatim, whatever it says"
    for accusation in ("reported a change", "claimed"):
        assert accusation not in message, (
            f"message asserts {accusation!r}, which is false when the model correctly "
            f"declined to edit anything"
        )
