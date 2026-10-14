import asyncio
import sys
from datetime import timedelta
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

        assert result["steps"][0]["timeout_seconds"] == 900  # configs/oncall/capabilities.yaml
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


# --- execute_step: heartbeat and the lost sandbox -----------------------


def _step_payload(tmp_path, results):
    return {
        "request_id": "req-1",
        "workspace": str(tmp_path),
        "prompt": "fix it",
        "config": {},
        "capability": "generate_change",
        "params": {},
        "results": results,
    }


class _Info:
    """Enough of `activity.info()` for execute_step."""

    def __init__(self, start_to_close):
        self.start_to_close_timeout = start_to_close


@pytest.mark.asyncio
async def test_execute_step_heartbeats_while_the_capability_runs(tmp_path, monkeypatch):
    # Without this there is no way for Temporal to notice a dead worker
    # before start_to_close -- 1200s for generate_change. Measured on stg
    # 2026-10-09: one OOMKill cost 4m15s and one pod replacement cost the
    # full 20 minutes, both spent waiting on a worker already gone.
    beats = []
    monkeypatch.setattr(activities.activity, "info", lambda: _Info(timedelta(seconds=1200)))
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: beats.append(1))
    monkeypatch.setattr(activities, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    async def slow(name, ctx):
        await asyncio.sleep(0.1)
        return {"ok": True}

    monkeypatch.setattr(activities, "run_capability", slow)

    assert await activities.execute_step(_step_payload(tmp_path, {})) == {"ok": True}
    assert beats, "the activity never told Temporal it was alive"


@pytest.mark.asyncio
async def test_the_heartbeat_stops_when_the_step_does(tmp_path, monkeypatch):
    # A leaked beat task would outlive its activity and keep reporting a
    # step that is long gone.
    monkeypatch.setattr(activities.activity, "info", lambda: _Info(timedelta(seconds=1200)))
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)
    monkeypatch.setattr(activities, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    spawned = []
    real_create_task = asyncio.create_task

    def capture(coro, **kwargs):
        task = real_create_task(coro, **kwargs)
        spawned.append(task)
        return task

    monkeypatch.setattr(activities.asyncio, "create_task", capture)

    async def slow(name, ctx):
        # Long enough that the beat task is actually scheduled and running
        # by the time the step finishes -- otherwise this would pass on a
        # task that never started, which proves nothing.
        await asyncio.sleep(0.05)
        return {"ok": True}

    monkeypatch.setattr(activities, "run_capability", slow)

    await activities.execute_step(_step_payload(tmp_path, {}))
    await asyncio.sleep(0)

    assert spawned, "no heartbeat task was started"
    assert spawned[0].done(), "the heartbeat outlived the step it was reporting on"


@pytest.mark.asyncio
async def test_the_heartbeat_stops_even_when_the_step_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(activities.activity, "info", lambda: _Info(timedelta(seconds=1200)))
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)
    monkeypatch.setattr(activities, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(activities, "run_capability", AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        await activities.execute_step(_step_payload(tmp_path, {}))


@pytest.mark.asyncio
async def test_the_step_is_told_the_activitys_own_deadline(tmp_path, monkeypatch):
    # opencode_runtime bounds its subprocess below this. It must be the
    # effective start_to_close, which Temporal clamps down to whatever the
    # pipeline budget has left -- not capabilities.yaml's timeout_seconds.
    seen = {}
    monkeypatch.setattr(activities.activity, "info", lambda: _Info(timedelta(seconds=44.86)))
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)

    async def capture(name, ctx):
        seen["deadline"] = ctx.step_deadline_seconds
        return {}

    monkeypatch.setattr(activities, "run_capability", capture)
    await activities.execute_step(_step_payload(tmp_path, {}))

    assert seen["deadline"] == 44.86


@pytest.mark.asyncio
async def test_a_sandbox_lost_with_its_pod_is_recloned_before_the_step_runs(tmp_path, monkeypatch):
    # stg workflow c0296796, 2026-10-09: clone_repo ran on one worker pod,
    # a deployment roll replaced it, and generate_change started on the new
    # pod to find `.../sandbox/<request>/repo` gone. The sandbox is an
    # emptyDir, so it belongs to a pod; a Temporal activity belongs to none.
    monkeypatch.setattr(activities.activity, "info", lambda: _Info(timedelta(seconds=1200)))
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)

    gone = str(tmp_path / "sandbox" / "req-1" / "repo")
    recloned = str(tmp_path / "recloned")
    Path(recloned).mkdir()
    calls = []

    async def fake_run(name, ctx):
        calls.append(name)
        if name == "clone_repo":
            return {"cloned_path": recloned, "ref": "dev"}
        return {"repo_dir_seen": ctx.repo_dir}

    monkeypatch.setattr(activities, "run_capability", fake_run)

    result = await activities.execute_step(
        _step_payload(tmp_path, {"clone_repo": {"cloned_path": gone, "ref": "dev"}})
    )

    assert calls == ["clone_repo", "generate_change"]
    assert result["repo_dir_seen"] == recloned, "the step must see the fresh checkout, not the lost path"


@pytest.mark.asyncio
async def test_a_checkout_that_is_still_there_is_not_recloned(tmp_path, monkeypatch):
    # The healing must be invisible on the overwhelmingly common path --
    # re-cloning every step would cost a fetch per step for nothing.
    monkeypatch.setattr(activities.activity, "info", lambda: _Info(timedelta(seconds=1200)))
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)

    present = tmp_path / "repo"
    present.mkdir()
    calls = []

    async def fake_run(name, ctx):
        calls.append(name)
        return {}

    monkeypatch.setattr(activities, "run_capability", fake_run)
    await activities.execute_step(
        _step_payload(tmp_path, {"clone_repo": {"cloned_path": str(present)}})
    )

    assert calls == ["generate_change"]


@pytest.mark.asyncio
async def test_a_plan_without_clone_repo_is_left_alone(tmp_path, monkeypatch):
    # A repo-less chat prompt, or an agent kind with no clone_repo at all.
    # Inventing one here would turn "no repo needed" into a hard failure.
    monkeypatch.setattr(activities.activity, "info", lambda: _Info(timedelta(seconds=1200)))
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)
    calls = []

    async def fake_run(name, ctx):
        calls.append(name)
        return {}

    monkeypatch.setattr(activities, "run_capability", fake_run)
    await activities.execute_step(_step_payload(tmp_path, {}))

    assert calls == ["generate_change"]


@pytest.mark.asyncio
async def test_respond_does_not_reclone_after_cleanup_removed_the_sandbox(tmp_path, monkeypatch):
    # workflow.py runs `respond` after `cleanup_workspace`, on purpose and on
    # every ending, so the checkout is always gone by then -- deleted
    # deliberately, not lost with a pod. Healing it would clone the whole
    # repository again to write a summary built entirely from `results`.
    # Observed on stg 2026-10-10 (dbc75291): `recloning_lost_sandbox
    # capability=respond`, two seconds after cleanup, on a healthy run.
    monkeypatch.setattr(activities.activity, "info", lambda: _Info(timedelta(seconds=120)))
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *a: None)
    calls = []

    async def fake_run(name, ctx):
        calls.append(name)
        return {}

    monkeypatch.setattr(activities, "run_capability", fake_run)
    payload = _step_payload(tmp_path, {"clone_repo": {"cloned_path": str(tmp_path / "gone")}})
    payload["capability"] = "respond"

    await activities.execute_step(payload)

    assert calls == ["respond"]
