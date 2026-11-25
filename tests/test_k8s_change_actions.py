"""`restart_rollout`, `rollback` and `delete_pod`, end to end against the fake API, plus the StatefulSet and
DaemonSet targets `find_workload` now resolves.

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


@pytest.mark.parametrize("prompt", ["roll back checkout", "rollback checkout to revision 1", "回滚 checkout", "undo the last rollout of checkout", "revert checkout to the previous version"])
def test_rollback_is_asked_for(prompt):
    assert policy.asks_rollback(prompt)


@pytest.mark.parametrize("prompt", ["don't roll back checkout, check it", "scale checkout to 3", "why did the rollout of checkout stall?"])
def test_rollback_is_not_asked_for(prompt):
    assert not policy.asks_rollback(prompt)


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


async def test_an_action_not_offered_for_the_kind_is_not_proposed(cluster):
    # rollback is a Deployment action; for a StatefulSet the model's pick falls through to "no change".
    result = await _propose("roll back db", _change(action="rollback"), _workload("StatefulSet", "db"))
    assert "does not ask for a concrete change" in result["refusal"]["reason"]


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


async def test_restart_of_a_statefulset(cluster):
    pa = await _approved(cluster, "restart statefulset db", _change(action="restart_rollout"), _workload("StatefulSet", "db"))
    assert pa["target"] == {"kind": "StatefulSet", "namespace": "shop", "name": "db"} and pa["deployment"] is None
    assert any("one at a time" in n for n in pa["notes"])
    cluster.auto_rollout = True
    result = await _apply(pa)
    assert result["status"] == "succeeded"
    [write] = cluster.patches
    assert write["kind"] == "StatefulSet" and write["dry_run"] is False


async def test_restart_of_a_daemonset(cluster):
    pa = await _approved(cluster, "restart the agent daemonset", _change(action="restart_rollout"), _workload("DaemonSet", "agent"))
    assert pa["target"]["kind"] == "DaemonSet" and "node by node" in pa["notes"][0]
    cluster.auto_rollout = True
    assert (await _apply(pa))["status"] == "succeeded"


async def test_restart_under_on_delete_is_refused(cluster):
    _obj(cluster, "StatefulSet", "db")["spec"]["updateStrategy"] = {"type": "OnDelete"}
    result = await _propose("restart db", _change(action="restart_rollout"), _workload("StatefulSet", "db"))
    assert "OnDelete" in result["refusal"]["reason"]


async def test_workload_rollout_waits_for_the_statefulset_revision():
    view = {"kind": "StatefulSet", "generation": 4, "observed_generation": 4, "replicas": 2, "strategy": {"partition": 0},
            "status": {"updated": 2, "ready": 2, "current_revision": "a", "update_revision": "b"}}
    assert not rollout.workload_rolled_out(view)
    view["status"]["current_revision"] = "b"
    assert rollout.workload_rolled_out(view)
    view["observed_generation"] = 3
    assert not rollout.workload_rolled_out(view)


# ---------------------------------------------------------------- rollback


async def test_rollback_to_the_previous_revision_is_proposed_without_the_template(cluster):
    result = await _propose("roll back checkout", _change(action="rollback"))
    pa = result["pending_action"]
    assert pa["from_revision"] == 3 and pa["to_revision"] == 2 and pa["replicaset"] == "checkout-5c8d7f6b4"
    assert pa["images"] == {"app": "registry.example/checkout:1.1"}
    assert pa["persistence"]["changes"] == [
        {"object": "Deployment shop/checkout container app", "field": "image", "from": "registry.example/checkout:1.2", "to": "registry.example/checkout:1.1"}
    ]
    [dry] = cluster.patches
    assert dry["dry_run"] is True and dry["content_type"] == "application/json-patch+json"
    test_op, replace_op = dry["body"]
    assert test_op == {"op": "test", "path": "/metadata/annotations/deployment.kubernetes.io~1revision", "value": "3"}
    assert replace_op["path"] == "/spec/template" and "pod-template-hash" not in replace_op["value"]["metadata"]["labels"]
    # The template (env values included) went to the API only: never into the proposal.
    assert fx.CANARY not in json.dumps(result)
    assert pa["risk_tier"] == "medium"


async def test_rollback_to_a_stated_revision(cluster):
    change = _change(action="rollback", revision=1, revision_quote="revision 1")
    pa = (await _propose("roll back checkout to revision 1", change))["pending_action"]
    assert pa["to_revision"] == 1 and pa["images"] == {"app": "registry.example/checkout:1.0"}


async def test_rollback_revision_not_in_the_request_is_refused(cluster):
    change = _change(action="rollback", revision=1, revision_quote="revision 1")
    result = await _propose("roll back checkout to the good one", change)
    assert "revision to roll back to explicitly" in result["refusal"]["reason"]


async def test_rollback_to_a_revision_no_longer_kept_is_refused(cluster):
    change = _change(action="rollback", revision=9, revision_quote="revision 9")
    result = await _propose("roll back checkout to revision 9", change)
    assert result["refusal"]["revisions"] == [1, 2]


async def test_rollback_without_an_earlier_revision_is_refused(cluster):
    cluster.objects["ReplicaSet"] = [rs for rs in cluster.objects["ReplicaSet"] if rs["metadata"]["annotations"]["deployment.kubernetes.io/revision"] == "3"]
    result = await _propose("roll back checkout", _change(action="rollback"))
    assert "no earlier revision" in result["refusal"]["reason"]


async def test_rollback_apply_patches_the_old_template_and_verifies(cluster):
    pa = await _approved(cluster, "roll back checkout", _change(action="rollback"))
    cluster.auto_rollout = True
    result = await _apply(pa)
    assert result["status"] == "succeeded"
    template = _obj(cluster, "Deployment", "checkout")["spec"]["template"]
    assert template["spec"]["containers"][0]["image"] == "registry.example/checkout:1.1"
    assert result["applied"]["images"] == {"app": "registry.example/checkout:1.1"}
    assert fx.CANARY not in json.dumps(result)
    assert result["persistence"]["changes"][0]["to"] == "registry.example/checkout:1.1"


async def test_rollback_apply_refuses_after_another_rollout(cluster):
    pa = await _approved(cluster, "roll back checkout", _change(action="rollback"))
    _obj(cluster, "Deployment", "checkout")["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "4"
    with pytest.raises(RuntimeError, match=r"rolled out again since the proposal \(revision 3 -> 4\)"):
        await _apply(pa)
    assert not cluster.patches


async def test_rollback_already_done_is_verified_not_repeated(cluster):
    pa = await _approved(cluster, "roll back checkout", _change(action="rollback"))
    # What the controller does after a rollback: revision 2's ReplicaSet becomes revision 4, the current one.
    _obj(cluster, "ReplicaSet", "checkout-5c8d7f6b4")["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "4"
    dep = _obj(cluster, "Deployment", "checkout")
    dep["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "4"
    dep["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.example/checkout:1.1"
    result = await _apply(pa)
    assert result["applied"]["already_at_target"] is True and result["status"] == "succeeded" and not cluster.patches


async def test_rollback_reported_reverted_when_the_newer_image_comes_back():
    dep = {"template": {"containers": [{"name": "app", "image": "registry.example/checkout:1.2"}]}}
    moved = rollout.moved_away(dep, {"kind": "images", "images": {"app": "registry.example/checkout:1.1"}})
    assert "runs image registry.example/checkout:1.2 again" in moved


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


async def test_delete_of_the_only_ready_pod_is_high_risk(cluster):
    result = await _propose("delete pod worker-1", _change(action="delete_pod", pod="worker-1"), _resolved("worker"))
    pa = result["pending_action"]
    assert pa["risk_tier"] == "high" and any("No other pod" in r["message"] for r in pa["risks"])


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


async def test_delete_of_a_statefulset_pod_waits_for_the_same_name_with_a_new_uid(cluster):
    pa = await _approved(cluster, "delete pod db-1", _change(action="delete_pod", pod="db-1"), _workload("StatefulSet", "db"))
    assert any("same name" in n for n in pa["notes"]) and pa["deployment"] is None
    cluster.auto_replace = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and result["verification"]["replacement"] == ["db-1"]


async def test_a_pod_name_only_inside_a_longer_one_is_not_taken(cluster):
    cluster.objects["Pod"].append(fx.owned_pod("db-10", "StatefulSet", "db"))
    result = await _propose("delete pod db-10", _change(action="delete_pod", pod="db-1"), _workload("StatefulSet", "db"))
    assert "not clear" in result["refusal"]["reason"] and not cluster.deletes


async def test_delete_of_a_daemonset_pod_says_the_node_runs_without_it(cluster):
    pa = (await _propose("delete pod agent-x7k2p", _change(action="delete_pod", pod="agent-x7k2p"), _workload("DaemonSet", "agent")))["pending_action"]
    assert any("runs without this DaemonSet's pod" in r["message"] for r in pa["risks"])


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
