"""Nodes are only read: `find_workload` resolves a node the request names when no workload matches,
`check_health` looks at one, and `propose_change` refuses to change one (cordon, uncordon and drain were built
and then declined, 2026-11-25).

Uses the cluster of test_k8s_change_actions.py: node-a runs checkout-1, worker-1, db-0 and DaemonSet pods.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
from test_k8s_capabilities import ScriptedLLM, _change, _ctx  # noqa: E402
from test_k8s_change_actions import _obj, build_cluster  # noqa: E402

from k8s_agent.capabilities import check_health, find_workload, propose_change  # noqa: E402

pytestmark = pytest.mark.asyncio
_NODE = {"find_workload": {"resolution": {"scope": "node", "kind": "Node", "namespace": None, "name": "node-a"}}}


@pytest.fixture
def cluster(monkeypatch):
    fake = build_cluster(monkeypatch)
    for pod in fake.objects["Pod"]:
        pod["spec"].setdefault("nodeName", pod["spec"].get("nodeName"))
    return fake


def _on(cluster, node):
    return sorted(p["metadata"]["name"] for p in cluster.objects["Pod"] if p["spec"].get("nodeName") == node)


async def test_a_node_quoted_beside_a_workload_does_not_take_over(cluster):
    result = await find_workload.run(_ctx("find_workload", "restart checkout, its pod on node-a is stuck", llm=ScriptedLLM()))
    assert result["resolution"]["name"] == "checkout"


async def test_a_node_quoted_while_the_model_resolves_a_workload_does_not_take_over(cluster):
    llm = ScriptedLLM('RESOLUTION_JSON: {"scope": "deployment", "namespace": "shop", "name": "worker", "confident": true}')
    result = await find_workload.run(_ctx("find_workload", "scale the payment workers to 5, node node-a is full", llm=llm))
    assert result["resolution"] == {"scope": "deployment", "namespace": "shop", "name": "worker"}


async def test_a_question_about_a_node_resolves_to_it_when_no_workload_matches(cluster):
    llm = ScriptedLLM('RESOLUTION_JSON: {"scope": "cluster", "confident": true}')
    result = await find_workload.run(_ctx("find_workload", "is node node-a healthy?", llm=llm))
    assert result["resolution"] == {"scope": "node", "kind": "Node", "namespace": None, "name": "node-a"}


async def test_find_workload_takes_the_short_form_of_an_eks_node_name(cluster):
    cluster.objects["Node"].append(fx.node("ip-10-1-21-112.us-west-1.compute.internal"))
    llm = ScriptedLLM('RESOLUTION_JSON: {"scope": "cluster", "confident": true}')
    result = await find_workload.run(_ctx("find_workload", "what runs on ip-10-1-21-112?", llm=llm))
    assert result["resolution"]["name"] == "ip-10-1-21-112.us-west-1.compute.internal"
    llm = ScriptedLLM('RESOLUTION_JSON: {"scope": "cluster", "confident": true}')
    result = await find_workload.run(_ctx("find_workload", "what runs on ip-10-1-21-11?", llm=llm))
    assert result["resolution"]["scope"] == "cluster"


async def test_check_health_of_a_node_covers_its_pods_only(cluster):
    _obj(cluster, "Node", "node-a")["spec"]["unschedulable"] = True
    result = await check_health.run(_ctx("check_health", "check node-a", results=_NODE, options={"include_logs": False}))
    report = result["report"]
    assert report["scope"]["type"] == "node" and report["scope"]["name"] == "node-a"
    assert report["summary"]["pods"] == len(_on(cluster, "node-a"))
    assert any(f["kind"] == "Node" and f["reason"] == "Cordoned" for f in report["findings"])


async def test_a_node_that_is_gone_is_a_gap_not_a_healthy_node(cluster):
    cluster.objects["Node"] = [n for n in cluster.objects["Node"] if n["metadata"]["name"] != "node-a"]
    report = (await check_health.run(_ctx("check_health", "check node-a", results=_NODE, options={"include_logs": False})))["report"]
    assert report["summary"]["pods"] == 0 and any("no longer exists" in g for g in report["gaps"])


async def test_a_change_to_a_node_is_refused(cluster):
    llm = ScriptedLLM(_change(action="cordon_node"))
    result = await propose_change.run(_ctx("propose_change", "drain node node-a", results=_NODE, llm=llm))
    assert "does not change nodes" in result["refusal"]["reason"] and not cluster.patches and llm.prompts == []


async def test_nodes_are_not_listed_when_a_workload_is_named(cluster, monkeypatch):
    listed = []
    original = cluster.list

    async def recording(kind, *args, **kwargs):
        listed.append(kind)
        return await original(kind, *args, **kwargs)

    monkeypatch.setattr(cluster, "list", recording)
    result = await find_workload.run(_ctx("find_workload", "scale checkout to 3", llm=ScriptedLLM()))
    assert result["resolution"]["name"] == "checkout" and "Node" not in listed


async def test_a_failed_node_list_does_not_block_the_resolution(cluster):
    cluster.forbidden.add("Node")
    llm = ScriptedLLM('RESOLUTION_JSON: {"scope": "deployment", "namespace": "shop", "name": "checkout", "confident": true}')
    result = await find_workload.run(_ctx("find_workload", "why is the shop front slow", llm=llm, options={"namespace": "shop"}))
    assert result["resolution"]["name"] == "checkout"
