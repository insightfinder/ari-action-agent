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


@pytest.mark.asyncio
async def test_plan_task_applies_oncalls_default_step_policy(monkeypatch):
    # oncall's own default_step_policy (ari_oncall_agent/meta.py) appends
    # publish_pr whenever generate_change ran and the planner's own step
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

        assert [s["capability"] for s in result["steps"]] == ["clone_repo", "generate_change", "publish_pr"]
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
