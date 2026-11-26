"""`restart_rollout` and `delete_pod`, end to end against the fake API, plus the StatefulSet and DaemonSet
targets `find_workload` resolves (read only: a change to one is refused; `rollback` and every StatefulSet or
DaemonSet change were removed on 2026-11-26).

Uses the fake cluster of test_k8s_capabilities.py, with checkout's revision history (ReplicaSets for revisions
1-3; revision 3 is the running one), a StatefulSet `db` with pods db-0/db-1 and a DaemonSet `agent`.
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
import test_k8s_capabilities as base  # noqa: E402
from test_k8s_capabilities import ScriptedLLM, _change, _ctx, _resolved  # noqa: E402

from k8s_agent.capabilities import apply_approved_change, check_health, find_workload, propose_change  # noqa: E402
from k8s_agent.tools import policy, read, rollout, write  # noqa: E402

pytestmark = pytest.mark.asyncio
_RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"


@pytest.fixture
def cluster(monkeypatch):
    return build_cluster(monkeypatch)


def build_cluster(monkeypatch):
    fake = fx.FakeClient(base._cluster(), logs={})
    monkeypatch.setattr(read, "get_client", lambda: fake)
    monkeypatch.setattr(write, "get_client", lambda: fake)
    monkeypatch.setattr(rollout, "POLL_SECONDS", 0)
    monkeypatch.setattr(rollout, "STABILITY_SECONDS", 0)
    checkout = next(d for d in fake.objects["Deployment"] if d["metadata"]["name"] == "checkout")
    checkout["metadata"]["uid"] = "uid-checkout"
    checkout["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "3"
    checkout["spec"]["template"]["metadata"]["labels"]["app"] = "checkout"
    fake.objects["ReplicaSet"] = [
        fx.replicaset("checkout", 1, "registry.example/checkout:1.0", "6b8d9f5c4"),
        fx.replicaset("checkout", 2, "registry.example/checkout:1.1", "5c8d7f6b4"),
        fx.replicaset("checkout", 3, "registry.example/checkout:1.2", fx.TEMPLATE_HASH, replicas=3),
        fx.replicaset("other", 7, "registry.example/other:9", "9f8d7c6b5"),
    ]
    fake.objects["StatefulSet"] = [fx.statefulset("db", labels=fx.HELM_LABELS)]
    fake.objects["DaemonSet"] = [fx.daemonset("agent")]
    fake.objects["Pod"] += [
        fx.owned_pod("db-0", "StatefulSet", "db"),
        fx.owned_pod("db-1", "StatefulSet", "db", node="node-b"),
        fx.owned_pod("agent-x7k2p", "DaemonSet", "agent"),
        fx.owned_pod("agent-q9m4z", "DaemonSet", "agent", node="node-b"),
    ]
    return fake


def _workload(kind, name, namespace="shop"):
    return {"find_workload": {"resolution": {"scope": "workload", "kind": kind, "namespace": namespace, "name": name}}}


async def _propose(prompt, change, results=None):
    return await propose_change.run(_ctx("propose_change", prompt, results=results or _resolved("checkout"), llm=ScriptedLLM(change)))


async def _approved(cluster, prompt, change, results=None):
    result = await _propose(prompt, change, results)
    assert "pending_action" in result, result
    cluster.patches.clear()
    cluster.deletes.clear()
    return fx.a2a_round_trip(result["pending_action"])


async def _apply(pa):
    return (await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True)))["action_result"]


def _obj(cluster, kind, name):
    return next(o for o in cluster.objects[kind] if o["metadata"]["name"] == name)


# ---------------------------------------------------------------- intent gates (code, not the model)


@pytest.mark.parametrize(
    "prompt",
    ["restart checkout", "do a rollout restart of deployment checkout", "重启 checkout", "please bounce the checkout pods", "滚动重启 db"],
)
def test_restart_is_asked_for(prompt):
    assert policy.asks_restart(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "checkout-3 restarted 9 times; raise the memory limit to 4Gi",
        "the restart count of checkout is 9, why?",
        "resize pod checkout-1 in place without a restart: cpu limit 2",
        "原地修改，不重启",
        "checkout 重启了 5 次，内存加到 4Gi",
        "don't restart anything, just check checkout",
        "what is the restartPolicy of checkout?",
    ],
)
def test_restart_is_not_asked_for(prompt):
    assert not policy.asks_restart(prompt)


@pytest.mark.parametrize(
    "prompt", ["delete pod checkout-1", "kill the pod checkout-1 in shop", "删除 pod checkout-1", "checkout-1 这个 pod 删掉", "restart pod checkout-1", "重启 pod checkout-1"]
)
def test_delete_pod_is_asked_for(prompt):
    assert policy.asks_delete_pod(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "do not delete the pod checkout-1, check it",
        "pod checkout-1 is crashing",
        "delete nothing",
        "pod checkout-1 restarted 9 times",
        "pod checkout-3 restart count is 9, raise memory to 4Gi",
        "pod checkout-3 重启了 5 次",
        "pod checkout-3 的重启次数是 9",
    ],
)
def test_delete_pod_is_not_asked_for(prompt):
    assert not policy.asks_delete_pod(prompt)


async def test_a_restart_the_request_does_not_ask_for_is_refused(cluster):
    result = await _propose("checkout-3 restarted 9 times, what is wrong?", _change(action="restart_rollout"))
    assert result["refusal"]["asked_for"] == "restart_rollout"
    assert "does not itself ask for a restart" in result["refusal"]["reason"]
    assert not cluster.patches


@pytest.mark.parametrize("kind,name", [("StatefulSet", "db"), ("DaemonSet", "agent")])
async def test_a_change_to_a_statefulset_or_daemonset_is_refused(cluster, kind, name):
    llm = ScriptedLLM(_change(action="restart_rollout"))
    result = await propose_change.run(_ctx("propose_change", f"restart {name}", results=_workload(kind, name), llm=llm))
    assert "changes only Deployments" in result["refusal"]["reason"] and llm.prompts == [] and not cluster.patches


async def test_a_statefulset_restart_proposed_before_the_removal_is_not_applied(cluster):
    pa = await _approved(cluster, "restart checkout", _change(action="restart_rollout"))
    pa.update({"workload_kind": "StatefulSet", "workload": "db", "deployment": None, "target": {"kind": "StatefulSet", "namespace": "shop", "name": "db"}})
    with pytest.raises(RuntimeError, match="no longer changes a StatefulSet"):
        await _apply(pa)
    assert not cluster.patches


# ---------------------------------------------------------------- restart_rollout


async def test_restart_of_a_deployment_is_proposed(cluster):
    result = await _propose("restart checkout", _change(action="restart_rollout"))
    pa = result["pending_action"]
    assert pa["action"] == "restart_rollout" and pa["target"] == {"kind": "Deployment", "namespace": "shop", "name": "checkout"}
    assert pa["deployment"] == "checkout" and pa["workload_kind"] == "Deployment"
    assert pa["restarted_at"].endswith("Z") and pa["after"] == {"restarted_at": pa["restarted_at"]}
    [dry] = cluster.patches
    assert dry["dry_run"] is True and dry["content_type"] == "application/merge-patch+json"
    assert dry["body"] == {"spec": {"template": {"metadata": {"annotations": {_RESTARTED_AT: pa["restarted_at"]}}}}}
    assert pa["risk_tier"] == "medium"
    assert any("replaces every pod of this Deployment (3)" in r["message"] for r in pa["risks"])
    assert pa["persistence"]["needed"] is False and pa["persistence"]["changes"] == []
    assert "nothing to keep in Git" in pa["drift_notice"]
    assert fx.CANARY not in json.dumps(result)


async def test_restart_of_a_paused_deployment_is_refused(cluster):
    _obj(cluster, "Deployment", "checkout")["spec"]["paused"] = True
    result = await _propose("restart checkout", _change(action="restart_rollout"))
    assert "is paused" in result["refusal"]["reason"]


async def test_restart_under_recreate_is_high_risk(cluster):
    _obj(cluster, "Deployment", "checkout")["spec"]["strategy"] = {"type": "Recreate"}
    pa = (await _propose("restart checkout", _change(action="restart_rollout")))["pending_action"]
    assert pa["risk_tier"] == "high" and any("Recreate" in r["message"] for r in pa["risks"])


async def test_restart_apply_writes_once_and_verifies(cluster):
    pa = await _approved(cluster, "restart checkout", _change(action="restart_rollout"))
    cluster.auto_rollout = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and result["target"]["kind"] == "Deployment"
    template = _obj(cluster, "Deployment", "checkout")["spec"]["template"]
    assert template["metadata"]["annotations"][_RESTARTED_AT] == pa["restarted_at"]
    assert result["persistence"]["needed"] is False
    # A retried attempt finds the annotation in place and only verifies.
    cluster.patches.clear()
    again = await _apply(pa)
    assert again["applied"]["already_at_target"] is True and not cluster.patches


async def test_restart_apply_refuses_after_someone_else_restarted(cluster):
    pa = await _approved(cluster, "restart checkout", _change(action="restart_rollout"))
    _obj(cluster, "Deployment", "checkout")["spec"]["template"]["metadata"]["annotations"] = {_RESTARTED_AT: "2026-11-24T00:00:00Z"}
    with pytest.raises(RuntimeError, match="restarted since the proposal"):
        await _apply(pa)
    assert not cluster.patches


# ---------------------------------------------------------------- delete_pod


async def test_delete_of_a_named_pod_is_proposed(cluster):
    result = await _propose("delete pod checkout-1", _change(action="delete_pod", pod="checkout-1"))
    pa = result["pending_action"]
    assert pa["target"] == {"kind": "Pod", "namespace": "shop", "name": "checkout-1"} and pa["pod_uid"] == "uid-checkout-1"
    [dry] = cluster.deletes
    assert dry == {"kind": "Pod", "name": "checkout-1", "namespace": "shop", "dry_run": True, "preconditions": {"uid": "uid-checkout-1"}}
    assert pa["risk_tier"] == "medium"
    assert any("PodDisruptionBudget checkout-pdb" in r["message"] and "does not wait for this budget" in r["message"] for r in pa["risks"])
    assert pa["persistence"]["needed"] is False
    assert fx.CANARY not in json.dumps(result)


async def test_delete_needs_the_pod_named(cluster):
    result = await _propose("delete a checkout pod", _change(action="delete_pod"))
    assert result["refusal"]["pods"] == ["checkout-1", "checkout-2", "checkout-3"]
    assert not cluster.deletes


async def test_delete_of_the_only_ready_pod_is_refused(cluster):
    result = await _propose("delete pod worker-1", _change(action="delete_pod", pod="worker-1"), _resolved("worker"))
    assert "No other pod" in result["refusal"]["reason"] and not cluster.deletes


async def test_delete_is_not_applied_once_the_other_pods_stopped_being_ready(cluster):
    pa = await _approved(cluster, "delete pod checkout-1", _change(action="delete_pod", pod="checkout-1"))
    for name in ("checkout-2", "checkout-3"):
        for status in _obj(cluster, "Pod", name)["status"].get("containerStatuses") or []:
            status["ready"] = False
    with pytest.raises(RuntimeError, match="no other pod of checkout is ready now"):
        await _apply(pa)
    assert not cluster.deletes


async def test_delete_apply_verifies_the_replacement(cluster):
    pa = await _approved(cluster, "delete pod checkout-1", _change(action="delete_pod", pod="checkout-1"))
    cluster.auto_replace = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and result["verification"]["replacement"] == ["checkout-1-r"]
    [write] = cluster.deletes
    assert write["dry_run"] is False and write["preconditions"] == {"uid": "uid-checkout-1"}
    assert result["target"] == {"kind": "Pod", "namespace": "shop", "name": "checkout-1"}


async def test_delete_already_done_is_verified_not_repeated(cluster):
    pa = await _approved(cluster, "delete pod checkout-1", _change(action="delete_pod", pod="checkout-1"))
    cluster.auto_replace = True
    await _apply(pa)
    cluster.deletes.clear()
    again = await _apply(pa)
    assert again["applied"]["already_at_target"] is True and not cluster.deletes and again["status"] == "succeeded"


async def test_a_pod_name_only_inside_a_longer_one_is_not_taken(cluster):
    import copy

    longer = copy.deepcopy(_obj(cluster, "Pod", "checkout-1"))
    longer["metadata"].update({"name": "checkout-10", "uid": "uid-checkout-10"})
    cluster.objects["Pod"].append(longer)
    result = await _propose("delete pod checkout-10", _change(action="delete_pod", pod="checkout-1"))
    assert "not clear" in result["refusal"]["reason"] and not cluster.deletes


async def test_delete_apply_on_an_older_chart_is_refused_with_the_rule(cluster, monkeypatch):
    async def forbidden(*_a, **_k):
        raise fx.api_error(403, 'pods "checkout-1" is forbidden: cannot delete resource "pods"', "Forbidden")

    monkeypatch.setattr(cluster, "delete", forbidden)
    result = await _propose("delete pod checkout-1", _change(action="delete_pod", pod="checkout-1"))
    assert 'lacks delete on "pods"' in result["refusal"]["reason"]


# ---------------------------------------------------------------- find_workload / check_health with other kinds


async def test_find_workload_resolves_a_statefulset_by_name(cluster):
    llm = ScriptedLLM()
    result = await find_workload.run(_ctx("find_workload", "restart statefulset db", llm=llm))
    assert result["resolution"] == {"scope": "workload", "kind": "StatefulSet", "namespace": "shop", "name": "db"}
    assert llm.prompts == []


async def test_find_workload_resolves_statefulset_and_daemonset_pods(cluster):
    result = await find_workload.run(_ctx("find_workload", "delete pod db-1", llm=ScriptedLLM()))
    assert result["resolution"]["kind"] == "StatefulSet"
    result = await find_workload.run(_ctx("find_workload", "delete pod agent-x7k2p", llm=ScriptedLLM()))
    assert result["resolution"]["kind"] == "DaemonSet" and result["resolution"]["name"] == "agent"


async def test_find_workload_takes_a_statefulset_from_the_model(cluster):
    llm = ScriptedLLM('RESOLUTION_JSON: {"scope": "workload", "kind": "StatefulSet", "namespace": "shop", "name": "db", "confident": true}')
    result = await find_workload.run(_ctx("find_workload", "restart the database", llm=llm, options={"namespace": "shop"}))
    assert result["resolution"]["kind"] == "StatefulSet"
    assert '"shop/db (StatefulSet)"' in llm.prompts[0]


async def test_check_health_of_a_statefulset_looks_at_its_own_pods_only(cluster):
    _obj(cluster, "StatefulSet", "db")["status"]["readyReplicas"] = 1
    result = await check_health.run(_ctx("check_health", "check db", results=_workload("StatefulSet", "db"), options={"include_logs": False}))
    report = result["report"]
    assert report["scope"] == {"type": "workload", "namespace": "shop", "deployment": None, "kind": "StatefulSet", "name": "db"}
    assert report["summary"]["pods"] == 2 and report["summary"]["deployments"] == 0
    assert {f["name"] for f in report["findings"]} <= {"db", "db-0", "db-1"}
    assert any(f["kind"] == "StatefulSet" and f["reason"] == "ReplicasUnavailable" and "1/2" in f["evidence"] for f in report["findings"])
    assert list(report["container_resources"]) == ["shop/db"]
