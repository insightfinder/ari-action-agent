import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import common.capabilities_registry as capabilities_registry  # noqa: E402
import common.orchestration.activities as activities  # noqa: E402
from common.agent_meta import get_agent_meta  # noqa: E402
from common.models.plan import Plan, Step  # noqa: E402
from common.settings import get_settings  # noqa: E402


@pytest.fixture
def jenkins_agent_kind(monkeypatch):
    # plan_task's bypass branch reads get_agent_meta().confirm_capability,
    # which is agent-kind-scoped -- flip the whole process to "jenkins" for
    # the duration of the test, same pattern as test_agent_meta.py.
    monkeypatch.setenv("AGENT_KIND", "jenkins")
    get_settings.cache_clear()
    get_agent_meta.cache_clear()
    capabilities_registry._load_specs.cache_clear()
    yield
    get_settings.cache_clear()
    get_agent_meta.cache_clear()
    capabilities_registry._load_specs.cache_clear()


@pytest.mark.asyncio
async def test_plan_task_bypass_branch_skips_the_llm_planner_entirely(jenkins_agent_kind, monkeypatch):
    create_plan = AsyncMock()
    monkeypatch.setattr(activities, "create_plan", create_plan)

    confirmed_action = {"action": "trigger_build", "job": "deploy/checkout-service", "id": "b2c1"}
    result = await activities.plan_task(
        {"prompt": "yes", "config": {}, "history": [], "confirmed_action": confirmed_action}
    )

    create_plan.assert_not_awaited()
    assert len(result["steps"]) == 1
    step = result["steps"][0]
    assert step["capability"] == "execute_action"
    assert step["params"] == confirmed_action
    assert step["confirmed"] is True
    assert step["retry"]["max_attempts"] == 1  # configs/jenkins/capabilities.yaml's execute_action policy


@pytest.mark.asyncio
async def test_plan_task_normal_branch_marks_every_planner_step_unconfirmed(jenkins_agent_kind, monkeypatch):
    create_plan = AsyncMock(return_value=Plan(steps=[Step(capability="resolve_target", params={})], rationale="r"))
    monkeypatch.setattr(activities, "create_plan", create_plan)

    result = await activities.plan_task({"prompt": "how's the build?", "config": {}, "history": []})

    create_plan.assert_awaited_once()
    assert len(result["steps"]) == 1
    assert result["steps"][0]["confirmed"] is False


@pytest.mark.asyncio
async def test_resolve_artifacts_resolves_declared_artifacts_from_results(jenkins_agent_kind):
    # jenkins' own agent_meta.artifacts: {"pending_action": "propose_action.pending_action", ...}
    results = {"propose_action": {"pending_action": {"id": "abc"}, "trace": {}}}

    found = await activities.resolve_artifacts(results)

    assert found == {"pending_action": {"id": "abc"}}


@pytest.mark.asyncio
async def test_resolve_artifacts_omits_artifacts_whose_capability_never_ran(jenkins_agent_kind):
    assert await activities.resolve_artifacts({}) == {}


@pytest.mark.asyncio
async def test_plan_task_raises_clearly_when_agent_kind_has_no_confirm_capability(monkeypatch):
    # oncall's AgentMeta.confirm_capability is None (it never pauses for
    # human-in-the-loop confirmation) -- a confirmed_action reaching
    # plan_task for that agent kind is a bug, not something to silently
    # fall through to the LLM planner for.
    monkeypatch.setenv("AGENT_KIND", "oncall")
    get_settings.cache_clear()
    get_agent_meta.cache_clear()
    capabilities_registry._load_specs.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="no confirm_capability"):
            await activities.plan_task(
                {"prompt": "yes", "config": {}, "history": [], "confirmed_action": {"action": "trigger_build"}}
            )
    finally:
        get_settings.cache_clear()
        get_agent_meta.cache_clear()
        capabilities_registry._load_specs.cache_clear()
