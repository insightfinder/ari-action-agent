import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_agent.capabilities import clone_repo, opencode_change, publish_pr  # noqa: E402
from ari_agent.capabilities.base import StepContext  # noqa: E402

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


def test_extract_pr_from_structured_tool_output():
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
    pr = publish_pr._extract_pr(result)
    assert pr == {"url": "https://github.com/x/y/pull/42", "branch": "ari/fix-1"}


def test_extract_pr_falls_back_to_text_regex():
    result = {"text": "Opened it here: https://github.com/x/y/pull/7 — done.", "tool_calls": []}
    pr = publish_pr._extract_pr(result)
    assert pr["url"] == "https://github.com/x/y/pull/7"


def test_extract_pr_returns_none_when_nothing_found():
    assert publish_pr._extract_pr({"text": "no pr here", "tool_calls": []}) is None


@pytest.mark.asyncio
async def test_publish_pr_instruction_scopes_staging_to_changed_files():
    opencode = AsyncMock(return_value={"text": "ok", "tool_calls": [
        {"tool": "github_create_pull_request", "input": {"head": "b"}, "output": {"html_url": "https://github.com/x/y/pull/1"}}
    ], "trace": _STUB_TRACE})
    ctx = _ctx(
        results={"generate_change": {"summary": "fixed it", "changed_files": ["cart_service.py"]}},
        tools={"opencode": opencode},
    )

    await publish_pr.run(ctx)

    instruction = opencode.await_args.args[1]
    assert "cart_service.py" in instruction
    assert "git add -A" in instruction  # named explicitly as what NOT to do
    assert "do not use" in instruction.lower()


@pytest.mark.asyncio
async def test_publish_pr_raises_clear_error_when_no_pr_found():
    opencode = AsyncMock(return_value={"text": "I could not open a PR", "tool_calls": []})
    ctx = _ctx(results={"generate_change": {"summary": "fixed it", "changed_files": ["a.py"]}}, tools={"opencode": opencode})

    with pytest.raises(RuntimeError, match="could not find a PR url"):
        await publish_pr.run(ctx)


def test_repo_slug_strips_dot_git_suffix():
    assert publish_pr._repo_slug("https://github.com/neyaadeez/go-script") == "neyaadeez/go-script"
    assert publish_pr._repo_slug("https://github.com/neyaadeez/go-script.git") == "neyaadeez/go-script"


@pytest.mark.asyncio
async def test_publish_pr_instruction_states_repo_explicitly():
    # Regression test: live testing against a real repo showed opencode
    # will not infer the target repo from the working tree's git remote on
    # its own — it asked for clarification instead of opening the PR. The
    # instruction must state owner/repo up front.
    opencode = AsyncMock(return_value={"text": "ok", "tool_calls": [
        {"tool": "github_create_pull_request", "input": {"head": "b"}, "output": {"html_url": "https://github.com/n/r/pull/1"}}
    ], "trace": _STUB_TRACE})
    ctx = _ctx(results={"generate_change": {"summary": "fixed it", "changed_files": ["a.go"]}}, tools={"opencode": opencode})
    ctx.config["repo"]["url"] = "https://github.com/neyaadeez/go-script"

    await publish_pr.run(ctx)

    instruction = opencode.await_args.args[1]
    assert "neyaadeez/go-script" in instruction
    assert "do not ask for the repository" in instruction.lower()


@pytest.mark.asyncio
async def test_publish_pr_base_ref_param_overrides_repo_default():
    opencode = AsyncMock(return_value={"text": "ok", "tool_calls": [
        {"tool": "github_create_pull_request", "input": {"head": "b"}, "output": {"html_url": "https://github.com/x/y/pull/1"}}
    ], "trace": _STUB_TRACE})
    ctx = _ctx(
        results={"generate_change": {"summary": "fixed it", "changed_files": ["a.py"]}},
        tools={"opencode": opencode},
        params={"base_ref": "staging"},
    )

    await publish_pr.run(ctx)

    instruction = opencode.await_args.args[1]
    assert "against staging" in instruction
    assert "against main" not in instruction


@pytest.mark.asyncio
async def test_publish_pr_base_ref_defaults_to_repo_ref_when_unset():
    opencode = AsyncMock(return_value={"text": "ok", "tool_calls": [
        {"tool": "github_create_pull_request", "input": {"head": "b"}, "output": {"html_url": "https://github.com/x/y/pull/1"}}
    ], "trace": _STUB_TRACE})
    ctx = _ctx(results={"generate_change": {"summary": "fixed it", "changed_files": ["a.py"]}}, tools={"opencode": opencode})

    await publish_pr.run(ctx)

    instruction = opencode.await_args.args[1]
    assert "against main" in instruction


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
