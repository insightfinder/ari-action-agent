import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_agent.capabilities import respond  # noqa: E402
from ari_agent.capabilities.base import StepContext  # noqa: E402
from ari_agent.capabilities.registry import catalog  # noqa: E402


def _ctx(prompt, results, params, history=None, tools=None):
    return StepContext(
        request_id="req-1",
        workspace="/tmp/ws",
        prompt=prompt,
        config={"llm": {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"}},
        capability="respond",
        params=params,
        results=results,
        history=history or [],
        tools=tools or {},
    )


def test_respond_is_excluded_from_planner_catalog():
    names = {c["name"] for c in catalog()}
    assert "respond" not in names
    assert {"clone_repo", "analyze", "generate_change", "verify", "publish_pr"} <= names


@pytest.mark.asyncio
async def test_respond_answers_directly_when_no_steps_ran():
    llm_complete = AsyncMock(return_value="A NullPointerException usually means...")
    ctx = _ctx(
        prompt="What causes a NullPointerException?",
        results={},
        params={"pipeline_status": "no_action"},
        tools={"llm_complete": llm_complete},
    )

    result = await respond.run(ctx)

    assert result["message"] == "A NullPointerException usually means..."
    user_prompt = llm_complete.await_args.args[2]
    assert "direct question" in user_prompt.lower()
    assert "What causes a NullPointerException?" in user_prompt


@pytest.mark.asyncio
async def test_respond_synthesizes_full_pipeline_results():
    llm_complete = AsyncMock(return_value="Fixed a race condition and opened PR #1.")
    ctx = _ctx(
        prompt="fix the bug",
        results={
            "analyze": {"root_cause": {"summary": "race condition", "evidence": []}},
            "generate_change": {"summary": "added a mutex", "diff": "..."},
            "verify": {"status": "passed"},
            "publish_pr": {"pr": {"url": "https://github.com/x/y/pull/1", "branch": "b"}},
        },
        params={"pipeline_status": "completed"},
        tools={"llm_complete": llm_complete},
    )

    result = await respond.run(ctx)

    assert result["message"] == "Fixed a race condition and opened PR #1."
    user_prompt = llm_complete.await_args.args[2]
    assert "race condition" in user_prompt
    assert "pull/1" in user_prompt


@pytest.mark.asyncio
async def test_respond_includes_history_and_failure_reason():
    llm_complete = AsyncMock(return_value="ok")
    ctx = _ctx(
        prompt="try again",
        results={"verify": {"status": "failed", "failure": "tests still red"}},
        params={"pipeline_status": "failed", "failure_reason": "tests still red"},
        history=[{"role": "user", "content": "please fix the flaky test"}],
        tools={"llm_complete": llm_complete},
    )

    await respond.run(ctx)

    user_prompt = llm_complete.await_args.args[2]
    assert "please fix the flaky test" in user_prompt
    assert "tests still red" in user_prompt


def test_system_prompt_forbids_dangling_follow_up_questions():
    # Regression test: a real response once ended with "Which should I do
    # next?" — a dangling question with no guaranteed reply channel, since
    # this agent doesn't implement a paused "waiting on you" task state.
    # This rule must stay in the prompt regardless of how the caller is
    # waiting for the result (blocking, polling, or a webhook).
    assert "NEVER end with a question" in respond._SYSTEM_PROMPT
    assert "plain recommendation" in respond._SYSTEM_PROMPT
