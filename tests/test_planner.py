"""How `create_plan` handles what the model actually returns.

The degraded path here is not hypothetical. On 2026-10-05 a live stg
investigation produced a confident "Action executed: ..." for an action
that was never scheduled, because the planner's response was not accepted
as a plan, the run silently continued on the fallback, and the raw
response was thrown away so nobody could see what the model had said.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common import planner  # noqa: E402

_FALLBACK = [{"capability": "resolve_target"}]


@pytest.fixture
def fake(monkeypatch):
    """A planner call with the model, the catalog and the agent kind all
    stubbed, so each test is only about what came back from the model."""
    meta = SimpleNamespace(
        planner_description="agent that does things",
        planner_domain_guidance="",
        planner_fallback_steps=_FALLBACK,
    )
    monkeypatch.setattr(planner, "get_agent_meta", lambda: meta)
    monkeypatch.setattr(planner, "catalog", lambda: [])
    monkeypatch.setattr(planner, "logger", Mock())

    def _call(raw):
        monkeypatch.setattr(planner, "complete", AsyncMock(return_value=raw))
        return planner.create_plan("do the thing", {"llm": {"provider": "openai"}})

    return SimpleNamespace(call=_call, logger=lambda: planner.logger)


class TestWhatCountsAsAPlan:
    @pytest.mark.asyncio
    async def test_bare_json(self, fake):
        plan = await fake.call('{"steps": [{"capability": "propose_action"}], "rationale": "r"}')
        assert [s.capability for s in plan.steps] == ["propose_action"]

    @pytest.mark.asyncio
    async def test_a_fenced_object_is_a_plan_too(self, fake):
        # The failure that started this: a model that answers correctly
        # but wraps it in a ```json fence used to be treated as garbage.
        plan = await fake.call('```json\n{"steps": [{"capability": "propose_action"}]}\n```')
        assert [s.capability for s in plan.steps] == ["propose_action"]
        fake.logger().warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_object_wrapped_in_prose_is_a_plan_too(self, fake):
        plan = await fake.call('Sure. Here is the plan:\n{"steps": []}\nHope that helps.')
        assert plan.steps == []

    @pytest.mark.asyncio
    async def test_an_empty_step_list_is_a_real_answer_not_a_failure(self, fake):
        # A plain question plans no steps; that must not look like a
        # parse failure and must not trigger the fallback.
        plan = await fake.call('{"steps": [], "rationale": "nothing to do"}')
        assert plan.steps == []
        assert plan.rationale == "nothing to do"
        fake.logger().warning.assert_not_called()


class TestTheFallback:
    @pytest.mark.asyncio
    async def test_prose_with_no_object_falls_back(self, fake):
        plan = await fake.call("I cannot plan this request.")
        assert [s.capability for s in plan.steps] == ["resolve_target"]

    @pytest.mark.asyncio
    async def test_well_formed_json_of_the_wrong_shape_falls_back(self, fake):
        # Used to raise ValidationError straight out of create_plan and
        # fail the whole activity, which is a worse outcome than the
        # fallback it already had for the neighbouring case.
        plan = await fake.call('{"plan": "clone the repo and fix it"}')
        assert [s.capability for s in plan.steps] == ["resolve_target"]

    @pytest.mark.asyncio
    async def test_the_raw_response_is_logged_so_the_next_one_is_diagnosable(self, fake):
        await fake.call("I cannot plan this request.")

        fake.logger().warning.assert_called_once()
        event, kwargs = fake.logger().warning.call_args
        assert event[0] == "planner_output_unusable"
        assert "I cannot plan this request." in kwargs["raw"]
        assert kwargs["fallback"] == ["resolve_target"]

    @pytest.mark.asyncio
    async def test_a_very_long_response_is_truncated_in_the_log(self, fake):
        await fake.call("x" * 10_000)
        assert len(fake.logger().warning.call_args[1]["raw"]) == 4000

    @pytest.mark.asyncio
    async def test_the_rationale_says_the_plan_is_not_the_models(self, fake):
        plan = await fake.call("nope")
        assert "default pipeline" in plan.rationale
