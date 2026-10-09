import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import common.capabilities_registry as capabilities_registry  # noqa: E402
import common.orchestration.activities as activities  # noqa: E402
from common.agent_meta import get_agent_meta  # noqa: E402
from common.models.plan import Plan, Step  # noqa: E402
from common.settings import get_settings  # noqa: E402


@pytest.fixture
def oncall_agent_kind(monkeypatch):
    # plan_task's bypass branch reads get_agent_meta().confirm_capability,
    # which is agent-kind-scoped -- flip the whole process to "oncall" for
    # the duration of the test, same pattern as test_agent_meta.py.
    #
    # These two bypass tests used to be written against the jenkins kind,
    # purely because it was the first kind to have a confirm_capability.
    # They were nearly lost when that kind moved to its own branch, which
    # would have left the confirmation bypass -- the mechanism the whole
    # email-approval flow resumes through -- with no coverage at all.
    monkeypatch.setenv("AGENT_KIND", "oncall")
    get_settings.cache_clear()
    get_agent_meta.cache_clear()
    capabilities_registry._load_specs.cache_clear()
    yield
    get_settings.cache_clear()
    get_agent_meta.cache_clear()
    capabilities_registry._load_specs.cache_clear()


@pytest.mark.asyncio
async def test_plan_task_bypass_branch_skips_the_llm_planner_entirely(oncall_agent_kind, monkeypatch):
    # An approved proposal must go straight to the confirm capability. If
    # the planner ran again here it could plan something other than what
    # the person actually approved, which is the entire thing the gate
    # exists to prevent.
    create_plan = AsyncMock()
    monkeypatch.setattr(activities, "create_plan", create_plan)

    confirmed_action = {
        "action": "open_pull_request",
        "repo": "insightfinder/x",
        "branch": "ari/fix-1",
        "id": "b2c1",
    }
    result = await activities.plan_task(
        {"prompt": "approved", "config": {}, "history": [], "confirmed_action": confirmed_action}
    )

    create_plan.assert_not_awaited()
    assert len(result["steps"]) == 1
    step = result["steps"][0]
    assert step["capability"] == "publish_pr"  # oncall's meta.confirm_capability
    assert step["params"] == confirmed_action
    assert step["confirmed"] is True
    assert step["retry"]["max_attempts"] == 3  # configs/oncall/capabilities.yaml's publish_pr policy


@pytest.mark.asyncio
async def test_plan_task_normal_branch_marks_every_planner_step_unconfirmed(oncall_agent_kind, monkeypatch):
    # The contrast case, and the reason StepContext.confirmed cannot be
    # spoofed by a planner: nothing the planner produces is ever born
    # confirmed.
    create_plan = AsyncMock(return_value=Plan(steps=[Step(capability="clone_repo", params={})], rationale="r"))
    monkeypatch.setattr(activities, "create_plan", create_plan)

    result = await activities.plan_task({"prompt": "what changed?", "config": {}, "history": []})

    create_plan.assert_awaited_once()
    assert len(result["steps"]) == 1
    assert result["steps"][0]["confirmed"] is False


@pytest.mark.asyncio
async def test_plan_task_applies_oncalls_default_step_policy(monkeypatch):
    # oncall's own default_step_policy (ari_oncall_agent/meta.py) appends
    # propose_pr whenever generate_change ran and the planner's own step
    # list omitted it -- exercised here through the real plan_task
    # activity, not just the policy function in isolation.
    monkeypatch.setenv("AGENT_KIND", "oncall")
    get_settings.cache_clear()
    get_agent_meta.cache_clear()
    capabilities_registry._load_specs.cache_clear()
    try:
        create_plan = AsyncMock(
            return_value=Plan(steps=[Step(capability="clone_repo"), Step(capability="generate_change")], rationale="r")
        )
        monkeypatch.setattr(activities, "create_plan", create_plan)

        result = await activities.plan_task({"prompt": "fix it", "config": {"options": {}}, "history": []})

        assert [s["capability"] for s in result["steps"]] == ["clone_repo", "generate_change", "propose_pr"]
    finally:
        get_settings.cache_clear()
        get_agent_meta.cache_clear()
        capabilities_registry._load_specs.cache_clear()


@pytest.mark.asyncio
async def test_resolve_artifacts_resolves_declared_artifacts_from_results(monkeypatch):
    # oncall's own agent_meta.artifacts: {"root_cause": "generate_change.root_cause", ...}
    monkeypatch.setenv("AGENT_KIND", "oncall")
    get_settings.cache_clear()
    get_agent_meta.cache_clear()
    try:
        results = {"generate_change": {"root_cause": "off-by-one in the retry loop", "diff": {}}}

        found = await activities.resolve_artifacts(results)

        assert found == {"root_cause": "off-by-one in the retry loop"}
    finally:
        get_settings.cache_clear()
        get_agent_meta.cache_clear()


@pytest.mark.asyncio
async def test_resolve_artifacts_omits_artifacts_whose_capability_never_ran(monkeypatch):
    monkeypatch.setenv("AGENT_KIND", "oncall")
    get_settings.cache_clear()
    get_agent_meta.cache_clear()
    try:
        assert await activities.resolve_artifacts({}) == {}
    finally:
        get_settings.cache_clear()
        get_agent_meta.cache_clear()


@pytest.mark.asyncio
async def test_plan_task_attaches_verifys_timeout_seconds_from_the_capability_registry(monkeypatch):
    # workflow.py reads step["timeout_seconds"] to bound verify's own
    # activity — plan_task is what has to actually put it there, from
    # configs/oncall/capabilities.yaml's verify.timeout_seconds. Regression
    # coverage for the live incident: with no bound here, a hung verify ran
    # unbounded (workflow.py's 20-minute default) until an external
    # caller's own patience timeout killed it instead.
    monkeypatch.setenv("AGENT_KIND", "oncall")
    get_settings.cache_clear()
    get_agent_meta.cache_clear()
    capabilities_registry._load_specs.cache_clear()
    try:
        create_plan = AsyncMock(return_value=Plan(steps=[Step(capability="verify", params={})], rationale="r"))
        monkeypatch.setattr(activities, "create_plan", create_plan)

        result = await activities.plan_task({"prompt": "run tests", "config": {}, "history": []})

        assert result["steps"][0]["timeout_seconds"] == 480  # configs/oncall/capabilities.yaml
        assert result["steps"][0]["on_failure"]["goto"] == "generate_change"
    finally:
        get_settings.cache_clear()
        get_agent_meta.cache_clear()
        capabilities_registry._load_specs.cache_clear()


@pytest.mark.asyncio
async def test_plan_task_raises_clearly_when_agent_kind_has_no_confirm_capability(monkeypatch):
    # A confirmed_action reaching plan_task for an agent kind that has no
    # confirm_capability is a bug, not something to silently fall through
    # to the LLM planner for.
    #
    # Stubbed rather than pointed at a real agent kind: this used to use
    # oncall, which had no confirm_capability until its own PR approval
    # gate was added, and every shipped kind now has one. Pinning the
    # guard to whichever kind happens to lack the field makes the test
    # quietly stop testing anything the moment that changes -- which is
    # exactly what happened here.
    meta = SimpleNamespace(confirm_capability=None)
    monkeypatch.setattr(activities, "get_agent_meta", lambda: meta)

    with pytest.raises(RuntimeError, match="no confirm_capability"):
        await activities.plan_task(
            {"prompt": "yes", "config": {}, "history": [], "confirmed_action": {"action": "trigger_build"}}
        )
