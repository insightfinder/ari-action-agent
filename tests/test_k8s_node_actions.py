"""Write batch 3 — `cordon_node`, `uncordon_node`, `drain_node` — end to end against the fake API, plus node
resolution and a node's health.

Uses the cluster of test_k8s_change_actions.py. node-a runs checkout-1, worker-1 (worker's only pod), db-0
(StatefulSet db), and the DaemonSet pods ds-a and agent-x7k2p.
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
from test_k8s_capabilities import ScriptedLLM, _change, _ctx  # noqa: E402
from test_k8s_change_actions import _apply, _obj, build_cluster  # noqa: E402

from k8s_agent.capabilities import check_health, find_workload, propose_change  # noqa: E402
from k8s_agent.tools import policy, read, rollout, write  # noqa: E402

pytestmark = pytest.mark.asyncio
_NODE = {"find_workload": {"resolution": {"scope": "node", "kind": "Node", "namespace": None, "name": "node-a"}}}


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.delenv("HOSTNAME", raising=False)
    fake = build_cluster(monkeypatch)
    for pod in fake.objects["Pod"]:
        pod["spec"].setdefault("nodeName", pod["spec"].get("nodeName"))
    return fake


def _on(cluster, node):
    return sorted(p["metadata"]["name"] for p in cluster.objects["Pod"] if p["spec"].get("nodeName") == node)


async def _propose(prompt, change, results=None):
    return await propose_change.run(_ctx("propose_change", prompt, results=results or _NODE, llm=ScriptedLLM(change)))


async def _approved(cluster, prompt, change):
    result = await _propose(prompt, change)
    assert "pending_action" in result, result
    cluster.patches.clear()
    cluster.evictions.clear()
    return fx.a2a_round_trip(result["pending_action"])


# ---------------------------------------------------------------- intent gates


@pytest.mark.parametrize(
    "gate,prompt",
    [
        ("asks_cordon", "cordon node ip-10-1-21-112"),
        ("asks_cordon", "封锁节点 ip-10-1-21-112"),
        ("asks_uncordon", "uncordon node ip-10-1-21-112"),
        ("asks_uncordon", "解除封锁 ip-10-1-21-112"),
        ("asks_drain", "drain node ip-10-1-21-112"),
        ("asks_drain", "排空节点 ip-10-1-21-112"),
    ],
)
def test_the_request_asks_for_it(gate, prompt):
    assert getattr(policy, gate)(prompt)


@pytest.mark.parametrize(
    "gate,prompt",
    [
        ("asks_cordon", "uncordon node ip-10-1-21-112"),
        ("asks_cordon", "node ip-10-1-21-112 is cordoned, why?"),
        ("asks_uncordon", "cordon ip-10-1-21-112"),
        ("asks_uncordon", "mark ip-10-1-21-112 unschedulable"),
        ("asks_drain", "the node was drained yesterday"),
        ("asks_drain", "we tried to drain ip-10-1-21-112 but it hung"),
        ("asks_drain", "节点已排空"),
    ],
)
def test_the_request_does_not_ask_for_it(gate, prompt):
    assert not getattr(policy, gate)(prompt)


# ---------------------------------------------------------------- resolution


async def test_find_workload_resolves_a_node_named_in_the_request(cluster):
    llm = ScriptedLLM()
    result = await find_workload.run(_ctx("find_workload", "cordon node node-a", llm=llm))
    assert result["resolution"] == {"scope": "node", "kind": "Node", "namespace": None, "name": "node-a"} and llm.prompts == []


async def test_a_node_quoted_beside_a_workload_does_not_take_over(cluster):
    result = await find_workload.run(_ctx("find_workload", "restart checkout, its pod on node-a is stuck", llm=ScriptedLLM()))
    assert result["resolution"]["name"] == "checkout"
    result = await find_workload.run(_ctx("find_workload", "drain node-a, checkout-1 there is stuck", llm=ScriptedLLM()))
    assert result["resolution"]["kind"] == "Node"


async def test_a_node_quoted_while_the_model_resolves_a_workload_does_not_take_over(cluster):
    llm = ScriptedLLM('RESOLUTION_JSON: {"scope": "deployment", "namespace": "shop", "name": "worker", "confident": true}')
    result = await find_workload.run(_ctx("find_workload", "scale the payment workers to 5, node node-a is full", llm=llm))
    assert result["resolution"] == {"scope": "deployment", "namespace": "shop", "name": "worker"}


async def test_a_node_verb_about_something_else_does_not_pick_the_node(cluster):
    result = await find_workload.run(_ctx("find_workload", "drain the queue, then restart checkout; its pod is on node-a", llm=ScriptedLLM()))
    assert result["resolution"]["name"] == "checkout"


async def test_a_question_about_a_node_resolves_to_it_when_no_workload_matches(cluster):
    llm = ScriptedLLM('RESOLUTION_JSON: {"scope": "cluster", "confident": true}')
    result = await find_workload.run(_ctx("find_workload", "is node node-a healthy?", llm=llm))
    assert result["resolution"]["kind"] == "Node" and result["resolution"]["name"] == "node-a"


async def test_find_workload_takes_the_short_form_of_an_eks_node_name(cluster):
    cluster.objects["Node"].append(fx.node("ip-10-1-21-112.us-west-1.compute.internal"))
    result = await find_workload.run(_ctx("find_workload", "drain ip-10-1-21-112 please", llm=ScriptedLLM()))
    assert result["resolution"]["name"] == "ip-10-1-21-112.us-west-1.compute.internal"
    result = await find_workload.run(_ctx("find_workload", "drain ip-10-1-21-11", llm=ScriptedLLM()))
    assert (result["resolution"] or {}).get("kind") != "Node"


async def test_check_health_of_a_node_covers_its_pods_only(cluster):
    _obj(cluster, "Node", "node-a")["spec"]["unschedulable"] = True
    result = await check_health.run(_ctx("check_health", "check node-a", results=_NODE, options={"include_logs": False}))
    report = result["report"]
    assert report["scope"]["type"] == "node" and report["scope"]["name"] == "node-a"
    assert report["summary"]["pods"] == len(_on(cluster, "node-a"))
    assert any(f["kind"] == "Node" and f["reason"] == "Cordoned" for f in report["findings"])


# ---------------------------------------------------------------- cordon / uncordon


async def test_cordon_is_proposed(cluster):
    result = await _propose("cordon node node-a", _change(action="cordon_node"))
    pa = result["pending_action"]
    assert pa["target"] == {"kind": "Node", "namespace": None, "name": "node-a"} and pa["namespace"] is None
    [dry] = cluster.patches
    assert dry["kind"] == "Node" and dry["body"] == {"spec": {"unschedulable": True}} and dry["dry_run"] is True
    assert pa["risk_tier"] == "medium" and pa["persistence"]["needed"] is False
    assert pa["estimated_seconds_per_pod_to_ready"] is None and any("evicts nothing" in n for n in pa["notes"])


async def test_cordoning_a_cordoned_node_is_refused(cluster):
    _obj(cluster, "Node", "node-a")["spec"]["unschedulable"] = True
    assert "already cordoned" in (await _propose("cordon node node-a", _change(action="cordon_node")))["refusal"]["reason"]


async def test_cordoning_the_last_schedulable_node_is_high_risk(cluster):
    _obj(cluster, "Node", "node-b")["spec"]["unschedulable"] = True
    pa = (await _propose("cordon node node-a", _change(action="cordon_node")))["pending_action"]
    assert pa["risk_tier"] == "high"


async def test_cordon_apply_writes_once_and_verifies(cluster):
    pa = await _approved(cluster, "cordon node node-a", _change(action="cordon_node"))
    result = await _apply(pa)
    assert result["status"] == "succeeded" and _obj(cluster, "Node", "node-a")["spec"]["unschedulable"] is True
    cluster.patches.clear()
    again = await _apply(pa)
    assert again["applied"]["already_at_target"] is True and not cluster.patches


async def test_uncordon_of_a_node_going_away_is_high_risk(cluster):
    node = _obj(cluster, "Node", "node-a")
    node["spec"]["unschedulable"] = True
    node["spec"]["taints"] = [{"key": "ToBeDeletedByClusterAutoscaler", "value": "1", "effect": "NoSchedule"}]
    pa = (await _propose("uncordon node node-a", _change(action="uncordon_node")))["pending_action"]
    assert pa["risk_tier"] == "high" and any("going away" in r["message"] for r in pa["risks"])


async def test_a_node_action_the_request_does_not_ask_for_is_refused(cluster):
    result = await _propose("node-a is cordoned, why?", _change(action="uncordon_node"))
    assert result["refusal"]["asked_for"] == "uncordon_node" and not cluster.patches


async def test_a_node_removed_while_watched_is_node_gone(monkeypatch):
    async def gone(name):
        return None

    monkeypatch.setattr(read, "get_node", gone)
    assert (await rollout.wait_node_schedulable("node-a", "uid-node-a", True, 30))["status"] == "node_gone"


async def test_an_apply_after_the_node_was_replaced_is_refused(cluster):
    pa = await _approved(cluster, "cordon node node-a", _change(action="cordon_node"))
    _obj(cluster, "Node", "node-a")["metadata"]["uid"] = "uid-node-a-2"
    with pytest.raises(RuntimeError, match="replaced by a new node of the same name"):
        await _apply(pa)


# ---------------------------------------------------------------- drain


async def test_drain_is_proposed_with_the_workloads_and_rules(cluster):
    result = await _propose("drain node node-a", _change(action="drain_node"))
    pa = result["pending_action"]
    assert pa["workloads"] == ["Deployment/shop/checkout", "Deployment/shop/worker", "StatefulSet/shop/db"]
    assert pa["risk_tier"] == "high" and pa["persistence"]["needed"] is False
    assert any("Stays: 2 DaemonSet pod(s)" in n for n in pa["notes"])
    # worker's only pod is on node-a.
    assert any("No ready pod of these runs on another node" in r["message"] and "Deployment/shop/worker" in r["message"] for r in pa["risks"])
    # The dry run cordoned and evicted nothing, and asked the API server about every pod it would evict.
    [cordon] = cluster.patches
    assert cordon["dry_run"] is True and cordon["body"] == {"spec": {"unschedulable": True}}
    assert sorted(e["pod"] for e in cluster.evictions) == ["shop/checkout-1", "shop/db-0", "shop/worker-1"]
    assert all(e["dry_run"] and e["body_dry_run"] == ["All"] for e in cluster.evictions)
    assert "checkout-1" in _on(cluster, "node-a") and not _obj(cluster, "Node", "node-a")["spec"].get("unschedulable")
    assert fx.CANARY not in json.dumps(result)


async def test_drain_with_a_pod_no_controller_owns_is_refused(cluster):
    bare = fx.pod("debug", app="debug", node="node-a")
    bare["metadata"]["ownerReferences"] = []
    cluster.objects["Pod"].append(bare)
    result = await _propose("drain node node-a", _change(action="drain_node"))
    assert result["refusal"]["bare_pods"] == ["shop/debug"] and not cluster.patches


async def test_drain_lists_empty_dir_data_and_budgets_that_allow_nothing(cluster):
    checkout = next(p for p in cluster.objects["Pod"] if p["metadata"]["name"] == "checkout-1")
    checkout["spec"]["volumes"] = [{"name": "cache", "emptyDir": {}}]
    cluster.objects["PodDisruptionBudget"][0]["status"]["disruptionsAllowed"] = 0
    pa = (await _propose("drain node node-a", _change(action="drain_node")))["pending_action"]
    assert pa["empty_dir_workloads"] == ["Deployment/shop/checkout"]
    assert any("emptyDir" in r["message"] and "checkout-1 (cache)" in r["message"] for r in pa["risks"])
    assert any("allow no disruption now" in r["message"] and "shop/checkout-pdb" in r["message"] for r in pa["risks"])


async def test_drain_apply_evicts_and_verifies(cluster):
    pa = await _approved(cluster, "drain node node-a", _change(action="drain_node"))
    result = await _apply(pa)
    assert result["status"] == "succeeded", result
    assert sorted(result["applied"]["evicted"]) == ["shop/checkout-1", "shop/db-0", "shop/worker-1"]
    assert _obj(cluster, "Node", "node-a")["spec"]["unschedulable"] is True
    assert _on(cluster, "node-a") == ["agent-x7k2p", "ds-a"]
    # A retried attempt carries on from what is on the node now: nothing is left, so nothing is written again.
    cluster.patches.clear()
    cluster.evictions.clear()
    again = await _apply(pa)
    assert again["status"] == "succeeded" and not cluster.evictions and not cluster.patches


async def test_a_retry_after_a_partial_drain_carries_on(cluster):
    pa = await _approved(cluster, "drain node node-a", _change(action="drain_node"))
    cluster.eviction_answers["checkout-1"] = 429
    await write.patch_workload({**pa, "eviction_seconds": 0})
    cluster.eviction_answers.clear()
    cluster.evictions.clear()
    result = await _apply(pa)
    assert result["status"] == "succeeded" and [e["pod"] for e in cluster.evictions] == ["shop/checkout-1"]


async def test_drain_retries_what_a_budget_holds_and_reports_it(cluster):
    pa = await _approved(cluster, "drain node node-a", _change(action="drain_node"))
    cluster.eviction_answers["checkout-1"] = 429
    action = {**pa, "eviction_seconds": 0}
    summary = await write.patch_workload(action)
    assert summary["blocked"] == ["shop/checkout-1"] and sorted(summary["evicted"]) == ["shop/db-0", "shop/worker-1"]
    # The verifier never evicts: a pod still running there ends it at once, not at the end of a long window.
    verdict = await rollout.wait_drain("node-a", "uid-node-a", 600)
    assert verdict["status"] == "drain_incomplete" and verdict["remaining"] == ["shop/checkout-1"]


async def test_an_eviction_error_after_the_cordon_does_not_fail_the_drain(cluster):
    pa = await _approved(cluster, "drain node node-a", _change(action="drain_node"))
    cluster.eviction_answers["db-0"] = 500
    summary = await write.patch_workload({**pa, "eviction_seconds": 0})
    assert summary["cordoned"] is True and summary["failed"][0]["pod"] == "shop/db-0"


async def test_drain_refuses_a_workload_the_approver_did_not_see(cluster):
    pa = await _approved(cluster, "drain node node-a", _change(action="drain_node"))
    cluster.objects["Pod"].append(fx.pod("web-1", app="web", node="node-a"))
    with pytest.raises(RuntimeError, match=r"workload\(s\) not in the approved drain: Deployment/shop/web"):
        await _apply(pa)
    assert not cluster.patches and not cluster.evictions


async def test_the_agents_own_pod_is_evicted_last(cluster, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "checkout-1")
    pa = await _approved(cluster, "drain node node-a", _change(action="drain_node"))
    assert any("own worker pod" in r["message"] for r in pa["risks"])
    await write.patch_workload({**pa, "eviction_seconds": 0})
    assert cluster.evictions[-1]["pod"] == "shop/checkout-1"


async def test_drain_on_an_older_chart_is_refused_with_the_rule(cluster):
    cluster.eviction_answers["checkout-1"] = 403
    result = await _propose("drain node node-a", _change(action="drain_node"))
    assert 'create on "pods/eviction"' in result["refusal"]["reason"]
