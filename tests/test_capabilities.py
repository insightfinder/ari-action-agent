import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_oncall_agent.capabilities import clone_repo, generate_change as opencode_change, publish_pr  # noqa: E402
from common.capabilities_base import StepContext  # noqa: E402

# Stub `run_opencode()`'s trace field (tools/opencode_cli.py) — every
# capability reads `result["trace"]` unconditionally now, so any mocked
# "opencode" tool return value needs one present, even when a test has
# nothing to assert about it.
_STUB_TRACE = {"elapsed_s": 0.0, "tool_call_counts": {}, "tool_call_total": 0, "slowest_gaps": []}


def _ctx(results: dict, tools: dict, params: dict | None = None, capability: str = "publish_pr") -> StepContext:
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


def test_extract_pr_from_opencode_result_uses_structured_tool_output():
    result = {
        "text": "done",
        "tool_calls": [
            {
                "tool": "github_create_pull_request",
                "input": {"head": "ari/fix-1"},
                "output": {"html_url": "https://github.com/x/y/pull/42"},
            }
        ],
    }
    pr = publish_pr._extract_pr_from_opencode_result(result)
    assert pr == {"url": "https://github.com/x/y/pull/42", "branch": "ari/fix-1"}


def test_extract_pr_from_opencode_result_falls_back_to_text_regex():
    result = {"text": "Opened it here: https://github.com/x/y/pull/7 — done.", "tool_calls": []}
    pr = publish_pr._extract_pr_from_opencode_result(result)
    assert pr["url"] == "https://github.com/x/y/pull/7"


def test_extract_pr_from_opencode_result_returns_none_when_nothing_found():
    assert publish_pr._extract_pr_from_opencode_result({"text": "no pr here", "tool_calls": []}) is None


@pytest.mark.asyncio
async def test_publish_pr_primary_path_stages_commits_pushes_and_opens_pr():
    tools = _publish_pr_tools()
    ctx = _ctx(
        results={"generate_change": {"summary": "fixed it", "changed_files": ["cart_service.py"]}},
        tools=tools,
    )

    result = await publish_pr.run(ctx)

    tools["git_add"].assert_awaited_once_with("/tmp/ws", ["cart_service.py"])
    tools["git_commit"].assert_awaited_once()
    tools["git_push"].assert_awaited_once()
    tools["github_create_pr"].assert_awaited_once()
    tools["opencode"].assert_not_awaited()  # primary path succeeded -- no fallback
    assert result == {"pr": {"url": "https://github.com/x/y/pull/1", "branch": "ari/x"}}


@pytest.mark.asyncio
async def test_publish_pr_create_pr_call_uses_repo_slug_title_body_branch_and_base():
    tools = _publish_pr_tools()
    ctx = _ctx(
        results={"generate_change": {"summary": "fixed the bug", "changed_files": ["a.go"]}},
        tools=tools,
    )
    ctx.config["repo"]["url"] = "https://github.com/neyaadeez/go-script"

    await publish_pr.run(ctx)

    args = tools["github_create_pr"].await_args.args
    repo_config, owner_repo, title, body, head, base = args
    assert owner_repo == "neyaadeez/go-script"
    assert "fixed the bug" in title or "fixed the bug" in body
    assert head.startswith("ari/")
    assert base == "main"


@pytest.mark.asyncio
async def test_publish_pr_base_ref_param_overrides_repo_default():
    tools = _publish_pr_tools()
    ctx = _ctx(
        results={"generate_change": {"summary": "fixed it", "changed_files": ["a.py"]}},
        tools=tools,
        params={"base_ref": "staging"},
    )

    await publish_pr.run(ctx)

    assert tools["github_create_pr"].await_args.args[5] == "staging"


@pytest.mark.asyncio
async def test_publish_pr_base_ref_defaults_to_clone_repo_ref_when_unset():
    tools = _publish_pr_tools()
    ctx = _ctx(
        results={
            "clone_repo": {"cloned_path": "/tmp/ws/repo", "ref": "dev"},
            "generate_change": {"summary": "fixed it", "changed_files": ["a.py"]},
        },
        tools=tools,
    )

    await publish_pr.run(ctx)

    assert tools["github_create_pr"].await_args.args[5] == "dev"


@pytest.mark.asyncio
async def test_publish_pr_raises_when_generate_change_reports_no_changed_files():
    tools = _publish_pr_tools()
    ctx = _ctx(results={"generate_change": {"summary": "fixed it", "changed_files": []}}, tools=tools)

    with pytest.raises(RuntimeError, match="no changed files"):
        await publish_pr.run(ctx)

    tools["git_add"].assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_pr_falls_back_to_opencode_when_primary_path_fails():
    # The safety net this whole design keeps: an unusual repo/auth state a
    # fixed git/REST sequence can't handle should still get a real
    # attempt via the more flexible opencode+MCP path, not just fail.
    tools = _publish_pr_tools(
        git_push=AsyncMock(side_effect=RuntimeError("git push failed: permission denied")),
        opencode=AsyncMock(
            return_value={
                "text": "ok",
                "tool_calls": [
                    {
                        "tool": "github_create_pull_request",
                        "input": {"head": "b"},
                        "output": {"html_url": "https://github.com/x/y/pull/9"},
                    }
                ],
            }
        ),
    )
    ctx = _ctx(results={"generate_change": {"summary": "fixed it", "changed_files": ["a.py"]}}, tools=tools)

    result = await publish_pr.run(ctx)

    tools["opencode"].assert_awaited_once()
    instruction = tools["opencode"].await_args.args[1]
    assert "permission denied" in instruction
    assert "a.py" in instruction
    assert result == {"pr": {"url": "https://github.com/x/y/pull/9", "branch": "b"}}


@pytest.mark.asyncio
async def test_publish_pr_raises_clearly_when_both_primary_and_fallback_fail():
    tools = _publish_pr_tools(
        git_push=AsyncMock(side_effect=RuntimeError("git push failed: permission denied")),
        opencode=AsyncMock(return_value={"text": "I could not open a PR either", "tool_calls": []}),
    )
    ctx = _ctx(results={"generate_change": {"summary": "fixed it", "changed_files": ["a.py"]}}, tools=tools)

    with pytest.raises(RuntimeError, match="primary path failed"):
        await publish_pr.run(ctx)


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
        tools={
            "opencode": opencode,
            "git_diff": AsyncMock(return_value="diff --git a/models.go b/models.go"),
            "git_changed_files": AsyncMock(return_value=["models.go"]),
        },
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
        tools={
            "opencode": opencode,
            "git_diff": AsyncMock(return_value="diff"),
            "git_changed_files": AsyncMock(return_value=["models.go"]),
        },
        capability="generate_change",
    )

    result = await opencode_change.run(ctx)

    assert result["root_cause"] == {"summary": "Zone field was never cleared.", "evidence": []}
    assert result["summary"] == "Zone field was never cleared."


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
        tools={
            "opencode": opencode,
            "git_diff": AsyncMock(return_value="diff --git a/models.go b/models.go"),
            "git_changed_files": AsyncMock(return_value=["models.go"]),
        },
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
