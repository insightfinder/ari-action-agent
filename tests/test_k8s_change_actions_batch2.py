"""CronJob resolution and health (read only), and the example requests refusals suggest. Write batch 2
(`set_image`, `pause_rollout` / `resume_rollout`, `suspend_cronjob` / `resume_cronjob`) was removed on
2026-11-26.

Uses the cluster of test_k8s_change_actions.py (checkout with its ReplicaSets, StatefulSet `db`, DaemonSet
`agent`) and adds a CronJob `report` with one past run's pod.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
from test_k8s_capabilities import ScriptedLLM, _change, _ctx, _resolved  # noqa: E402
from test_k8s_change_actions import _obj, _workload, build_cluster  # noqa: E402

from k8s_agent.capabilities import check_health, find_workload, propose_change  # noqa: E402
from k8s_agent.tools import governance  # noqa: E402

pytestmark = pytest.mark.asyncio
_CRON = _workload("CronJob", "report")


@pytest.fixture
def cluster(monkeypatch):
    fake = build_cluster(monkeypatch)
    fake.objects["CronJob"] = [fx.cronjob("report")]
    fake.objects["Job"] = []
    fake.objects["Pod"].append(fx.job_pod("report-29410560-x7k2p", "report-29410560"))
    return fake


async def _propose(prompt, change, results=None):
    return await propose_change.run(_ctx("propose_change", prompt, results=results or _resolved("checkout"), llm=ScriptedLLM(change)))


async def _approved(cluster, prompt, change, results=None):
    result = await _propose(prompt, change, results)
    assert "pending_action" in result, result
    cluster.patches.clear()
    return fx.a2a_round_trip(result["pending_action"])


# ---------------------------------------------------------------- example requests


def test_every_example_request_passes_its_own_gate():
    """What a refusal tells the user to ask, and what the live script asks, must be proposable."""
    import re

    import k8s_agent.actions as registry
    from test_k8s_live_script import live

    for name, spec in registry.REGISTRY.items():
        for kind in spec.workload_kinds:
            text = spec.not_asked.format(kind=kind, name="insightfinder-dailyreportworker", namespace="insightfinder")
            for example in re.findall(r'"([^"]+)"', text):
                assert spec.accepts(example), (name, example)
    ns = live.NAMESPACE
    for name, prompt in [
        ("delete_pod", f"delete pod e2e-plain-abc12 in namespace {ns}"),
        ("restart_rollout", f"restart the deployment e2e-plain in namespace {ns}"),
    ]:
        assert registry.get(name).accepts(prompt), prompt


# ---------------------------------------------------------------- CronJob resolution and health


async def test_find_workload_resolves_a_cronjob_by_name(cluster):
    llm = ScriptedLLM()
    result = await find_workload.run(_ctx("find_workload", "suspend cronjob report", llm=llm))
    assert result["resolution"] == {"scope": "workload", "kind": "CronJob", "namespace": "shop", "name": "report"}
    assert llm.prompts == []


async def test_find_workload_resolves_a_scheduled_run_pod_to_its_cronjob(cluster):
    result = await find_workload.run(_ctx("find_workload", "why did report-29410560-x7k2p fail?", llm=ScriptedLLM()))
    assert result["resolution"]["kind"] == "CronJob" and result["resolution"]["name"] == "report"


async def test_check_health_of_a_cronjob_looks_at_its_runs_pods(cluster):
    _obj(cluster, "CronJob", "report")["spec"]["suspend"] = True
    result = await check_health.run(_ctx("check_health", "check report", results=_CRON, options={"include_logs": False}))
    report = result["report"]
    assert report["summary"]["pods"] == 1
    assert any(f["kind"] == "CronJob" and f["reason"] == "Suspended" and f["severity"] == "info" for f in report["findings"])


def test_a_cronjob_owns_its_scheduled_runs_only():
    assert governance.is_cronjob_job("report-29410560", "report")
    assert not governance.is_cronjob_job("report-v2-29410560", "report")
    assert not governance.is_cronjob_job("reporter-29410560", "report")
    assert not governance.is_cronjob_job("report-manual-x2k4q", "report")


async def test_a_change_to_a_cronjob_is_refused_without_asking_the_model(cluster):
    llm = ScriptedLLM(_change(action="restart_rollout"))
    result = await propose_change.run(_ctx("propose_change", "suspend cronjob report", results=_CRON, llm=llm))
    assert "changes only Deployments" in result["refusal"]["reason"] and llm.prompts == [] and not cluster.patches
