import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common import respond  # noqa: E402
from common.capabilities_base import StepContext  # noqa: E402
from common.capabilities_registry import catalog  # noqa: E402


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
    assert {"clone_repo", "generate_change", "verify", "publish_pr"} <= names
    # "analyze" was merged into "generate_change" (see generate_change.py's
    # own docstring) — must not reappear as a separate planner-selectable
    # capability.
    assert "analyze" not in names


def test_verify_description_tells_the_planner_it_is_opt_in():
    # verify is the pipeline's slowest, least predictable step (cold
    # dependency downloads, unpredictable per-repo build tooling) — must
    # not be part of the default pipeline any more, only run when the
    # request explicitly asks for it. This can't be tested by actually
    # invoking the LLM planner, so assert the instruction it's given says
    # so unambiguously.
    verify_entry = next(c for c in catalog() if c["name"] == "verify")
    description = verify_entry["description"].lower()
    assert "opt-in" in description
    assert "not part of the default pipeline" in description


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
            "generate_change": {
                "root_cause": {"summary": "race condition", "evidence": []},
                "summary": "added a mutex",
                "diff": "...",
            },
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
    assert "NEVER end with a question" in respond._SYSTEM_PROMPT_TEMPLATE
    assert "plain recommendation" in respond._SYSTEM_PROMPT_TEMPLATE


# --------------------------------------------------------------------------
# Whether an action ran is stated by the pipeline, not inferred by the model.
#
# Regression tests for 2026-10-05: an action-agent run whose plan was only
# `resolve_target` answered "The remediation action ... has been identified and
# executed. **Action executed:** Trigger Buggy Deployment". Nothing had been
# scheduled and nothing was sent anywhere. "Never invent facts" did not stop
# it, because the absence of a step is the hard thing for a model to notice.
# --------------------------------------------------------------------------

def _meta(confirm_capability):
    return SimpleNamespace(confirm_capability=confirm_capability)


class TestExecutionFact:
    def test_a_missing_execute_step_is_stated_as_nothing_executed(self):
        fact = respond.execution_fact(_meta("execute_action"), {"resolve_target": {"x": 1}})
        assert "NOTHING WAS EXECUTED" in fact
        assert "execute_action" in fact

    def test_it_names_the_wording_the_model_must_not_use(self):
        fact = respond.execution_fact(_meta("execute_action"), {})
        for word in ("executed", "triggered", "run", "started", "stopped", "applied"):
            assert word in fact

    def test_a_proposal_alone_is_still_nothing_executed(self):
        # The exact shape of the live failure: a pending_action in the
        # results and no execute step.
        results = {"propose_action": {"pending_action": {"id": "pa-1"}, "requires_approval": True}}
        assert "NOTHING WAS EXECUTED" in respond.execution_fact(_meta("execute_action"), results)

    def test_a_step_that_ran_is_reported_as_having_run(self):
        results = {"execute_action": {"result": {"ran": True, "status_code": 200}}}
        fact = respond.execution_fact(_meta("execute_action"), results)
        assert "NOTHING WAS EXECUTED" not in fact
        assert "ran in this request" in fact

    def test_a_failed_step_is_not_an_execution(self):
        results = {"execute_action": {"status": "failed", "failure": "refused"}}
        assert "NOTHING WAS EXECUTED" in respond.execution_fact(_meta("execute_action"), results)

    def test_an_agent_kind_with_no_confirm_capability_says_nothing(self):
        # oncall has no single step meaning "something outside changed",
        # so inventing a claim about one would be worse than silence.
        assert respond.execution_fact(_meta(None), {"generate_change": {}}) == ""


class TestTheFactReachesThePrompt:
    @pytest.mark.asyncio
    async def test_it_is_included_when_nothing_executed(self, monkeypatch):
        monkeypatch.setattr(respond, "get_agent_meta", lambda: SimpleNamespace(
            confirm_capability="execute_action",
            planner_description="agent that runs actions",
            respond_domain_note="what was resolved",
        ))
        llm_complete = AsyncMock(return_value="ok")
        ctx = _ctx(
            prompt="roll it back",
            results={"propose_action": {"pending_action": {"id": "pa-1"}}},
            params={"pipeline_status": "needs_approval"},
            tools={"llm_complete": llm_complete},
        )

        await respond.run(ctx)

        assert "NOTHING WAS EXECUTED" in llm_complete.await_args.args[2]

    @pytest.mark.asyncio
    async def test_the_system_prompt_makes_fact_lines_binding(self):
        assert 'beginning "FACT:"' in respond._SYSTEM_PROMPT_TEMPLATE
        assert "never state or imply that" in respond._SYSTEM_PROMPT_TEMPLATE
