"""The k8s kind through the real workflow and real activities.

Temporal's time-skipping environment runs `AriAgentWorkflow` with the
production activities (`plan_task`, `execute_step`, `resolve_artifacts`)
and AGENT_KIND=k8s, so registry loading, the planner, the step policy, the
confirmation bypass, payload round-trips and artifact resolution are all
the real code. Only the LLM and the Kubernetes API are fakes.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

# The k8s kind's worker extra (pip install -e ".[k8s]"); skipped without it, like the CLI tests.
pytest.importorskip("kubernetes")
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
import test_k8s_capabilities as k8s_tests  # noqa: E402

_TASK_QUEUE = "test-k8s-queue"


@pytest.fixture
def k8s_kind(monkeypatch, tmp_path):
    import common.agent_meta as agent_meta
    import common.capabilities_registry as capabilities_registry
    import common.settings as settings
    import common.tools_registry as tools_registry

    def clear():
        settings.get_settings.cache_clear()
        capabilities_registry._load_specs.cache_clear()
        tools_registry._load_specs.cache_clear()
        agent_meta.get_agent_meta.cache_clear()

    monkeypatch.setenv("AGENT_KIND", "k8s")
    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path))
    clear()
    yield
    monkeypatch.delenv("AGENT_KIND")
    clear()


class _LLM:
    """Answers the planner, propose_change and respond by system prompt."""

    def __init__(self, plan: dict, change: dict):
        self.plan, self.change = plan, change
        self.prompts: list[str] = []

    async def __call__(self, llm, system, user):
        self.prompts.append(system + user)
        if "planning stage" in system:
            return json.dumps(self.plan)
        if "report exactly what it asks for" in system:
            return "CHANGE_JSON: " + json.dumps(self.change)
        return "Final answer."


async def _run(request: dict) -> dict:
    from common.orchestration.activities import (
        cleanup_workspace,
        execute_step,
        plan_task,
        prepare_workspace,
        resolve_artifacts,
    )
    from common.orchestration.workflow import AriAgentWorkflow

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[AriAgentWorkflow],
            activities=[prepare_workspace, plan_task, execute_step, cleanup_workspace, resolve_artifacts],
        ):
            return await env.client.execute_workflow(
                AriAgentWorkflow.run, request, id=f"k8s-{uuid.uuid4()}", task_queue=_TASK_QUEUE
            )


@pytest.mark.asyncio
async def test_propose_pauses_then_the_confirmed_proposal_is_applied(k8s_kind, monkeypatch):
    import common.llm.client as llm_client
    import common.planner as planner
    from k8s_agent.tools import read, rollout, write

    fake = fx.FakeClient(k8s_tests._cluster())
    fake.auto_rollout = True
    monkeypatch.setattr(read, "get_client", lambda: fake)
    monkeypatch.setattr(write, "get_client", lambda: fake)
    monkeypatch.setattr(rollout, "POLL_SECONDS", 0)
    monkeypatch.setattr(rollout, "STABILITY_SECONDS", 0)

    # The planner tries to sneak in the mutating step; the step policy must drop it.
    llm = _LLM(
        plan={"steps": [{"capability": "propose_change"}, {"capability": "apply_approved_change"}], "rationale": "scale"},
        change={"action": "scale_replicas", "replicas": 5, "replicas_quote": "to 5 replicas"},
    )
    monkeypatch.setattr(planner, "complete", llm)
    monkeypatch.setattr(llm_client, "complete", llm)

    config = {"llm": {"provider": "openai", "api_key": "sk-test", "model": "gpt-x"}, "options": {}}
    first = await _run({"prompt": "scale checkout to 5 replicas", "history": [], "config": config, "confirmed_action": None})

    assert first["status"] == "needs_approval"
    assert list(first["data"]) == ["find_workload", "propose_change"]
    pending = first["pending_action"]
    assert pending["action"] == "scale_replicas" and pending["replicas"] == 5
    assert [p["dry_run"] for p in fake.patches] == [True]

    # What the A2A executor hands back after the artifact round-trip.
    confirmed = fx.a2a_round_trip(pending)
    second = await _run({"prompt": "approved", "history": [], "config": config, "confirmed_action": confirmed})

    assert second["status"] == "completed"
    assert list(second["data"]) == ["apply_approved_change"]
    assert second["action_result"]["status"] == "succeeded"
    fx.a2a_round_trip(second["action_result"])
    assert [p["dry_run"] for p in fake.patches] == [True, False]
    dep = next(d for d in fake.objects["Deployment"] if d["metadata"]["name"] == "checkout")
    assert dep["spec"]["replicas"] == 5
    assert fx.CANARY not in json.dumps(first) + json.dumps(second) + "".join(llm.prompts)


@pytest.mark.asyncio
async def test_a_refusal_completes_without_changing_anything(k8s_kind, monkeypatch):
    import common.llm.client as llm_client
    import common.planner as planner
    from k8s_agent.tools import read, write

    fake = fx.FakeClient(k8s_tests._cluster())
    monkeypatch.setattr(read, "get_client", lambda: fake)
    monkeypatch.setattr(write, "get_client", lambda: fake)
    llm = _LLM(
        plan={"steps": [{"capability": "find_workload"}, {"capability": "propose_change"}]},
        change={"action": "scale_replicas", "replicas": 4, "replicas_quote": "to 4"},
    )
    monkeypatch.setattr(planner, "complete", llm)
    monkeypatch.setattr(llm_client, "complete", llm)
    config = {"llm": {"provider": "openai", "api_key": "sk-test", "model": "gpt-x"}, "options": {}}

    result = await _run({"prompt": "scale worker to 4", "history": [], "config": config, "confirmed_action": None})

    assert result["status"] == "completed"
    assert "ScaledObject" in result["refusal"]["reason"]
    fx.a2a_round_trip(result["refusal"])
    assert fake.patches == []
