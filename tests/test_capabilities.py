import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_agent.capabilities import clone_repo, publish_pr  # noqa: E402
from ari_agent.capabilities.base import StepContext  # noqa: E402


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
    ]})
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
    ]})
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
    ]})
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
    ]})
    ctx = _ctx(results={"generate_change": {"summary": "fixed it", "changed_files": ["a.py"]}}, tools={"opencode": opencode})

    await publish_pr.run(ctx)

    instruction = opencode.await_args.args[1]
    assert "against main" in instruction


def _clone_repo_tools(**overrides):
    tools = {
        "repo_cache": AsyncMock(return_value="/cache/repo-x"),
        "git_clone": AsyncMock(return_value="/tmp/ws/repo"),
        "git_set_origin": AsyncMock(),
        "opencode_config": Mock(),  # called synchronously (not awaited) in clone_repo.py
        "git_default_branch": AsyncMock(return_value="master"),
    }
    tools.update(overrides)
    return tools


@pytest.mark.asyncio
async def test_clone_repo_ref_param_overrides_repo_default():
    tools = _clone_repo_tools()
    ctx = _ctx(results={}, tools=tools, params={"ref": "dev"}, capability="clone_repo")

    result = await clone_repo.run(ctx)

    tools["git_clone"].assert_awaited_once_with({"url": "/cache/repo-x", "ref": "dev"}, "/tmp/ws")
    tools["git_set_origin"].assert_awaited_once()
    tools["git_default_branch"].assert_not_awaited()
    assert result == {"cloned_path": "/tmp/ws/repo", "ref": "dev"}


@pytest.mark.asyncio
async def test_clone_repo_ref_defaults_to_repo_config_when_unset():
    tools = _clone_repo_tools()
    ctx = _ctx(results={}, tools=tools, capability="clone_repo")

    result = await clone_repo.run(ctx)

    tools["git_clone"].assert_awaited_once_with({"url": "/cache/repo-x", "ref": "main"}, "/tmp/ws")
    tools["git_default_branch"].assert_not_awaited()
    assert result["ref"] == "main"


@pytest.mark.asyncio
async def test_clone_repo_ref_resolves_from_remote_default_when_nothing_named():
    # Neither the planner nor the request config named a branch — must not
    # assume "main"; must ask the remote (via the local mirror cache) what
    # its actual default branch is.
    tools = _clone_repo_tools()
    ctx = _ctx(results={}, tools=tools, capability="clone_repo")
    ctx.config["repo"] = {"url": "https://github.com/x/y"}  # no "ref" key at all

    result = await clone_repo.run(ctx)

    tools["git_default_branch"].assert_awaited_once_with("/cache/repo-x")
    tools["git_clone"].assert_awaited_once_with({"url": "/cache/repo-x", "ref": "master"}, "/tmp/ws")
    assert result["ref"] == "master"
