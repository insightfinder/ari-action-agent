"""Protected namespaces and workloads (`tools/protection.py`): refused at proposal and again at approval, before
the model is asked or the cluster is touched; the cluster's own ValidatingAdmissionPolicy answer is a refusal too.

Uses the cluster of test_k8s_change_actions.py.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
from test_k8s_capabilities import ScriptedLLM, _change, _ctx, _resolved  # noqa: E402
from test_k8s_change_actions import _apply, _approved, build_cluster  # noqa: E402

from k8s_agent.capabilities import propose_change  # noqa: E402
from k8s_agent.tools import protection  # noqa: E402

_SCALE = _change(action="scale_replicas", replicas=4, replicas_quote="4")


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.delenv("K8S_AGENT_PROTECTED_NAMESPACES", raising=False)
    monkeypatch.delenv("K8S_AGENT_PROTECTED_WORKLOADS", raising=False)
    return build_cluster(monkeypatch)


async def _propose_with(llm, prompt, results):
    return await propose_change.run(_ctx("propose_change", prompt, results=results, llm=llm))


@pytest.mark.asyncio
async def test_a_change_in_a_system_namespace_is_refused_without_asking_the_model(cluster):
    llm = ScriptedLLM(_SCALE)
    result = await _propose_with(llm, "scale coredns to 4", _resolved("coredns", namespace="kube-system"))
    assert "Namespace kube-system is protected" in result["refusal"]["reason"] and result["refusal"]["protected"] is True
    assert llm.prompts == [] and not cluster.patches


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace,name", [("ingress-nginx", "ingress-nginx-controller"), ("istio-system", "istiod")])
async def test_the_ingress_controller_and_the_mesh_are_refused(cluster, namespace, name):
    llm = ScriptedLLM(_SCALE)
    result = await _propose_with(llm, f"scale {name} to 4", _resolved(name, namespace=namespace))
    assert f"Namespace {namespace} is protected" in result["refusal"]["reason"]
    assert llm.prompts == [] and not cluster.patches


@pytest.mark.asyncio
async def test_a_protected_workload_is_refused(cluster, monkeypatch):
    monkeypatch.setenv("K8S_AGENT_PROTECTED_WORKLOADS", "shop/check*, other/ari-k8s-worker")
    llm = ScriptedLLM(_SCALE)
    result = await _propose_with(llm, "scale checkout to 4", _resolved("checkout"))
    assert "shop/checkout is protected" in result["refusal"]["reason"] and "shop/check*" in result["refusal"]["reason"]
    assert llm.prompts == [] and not cluster.patches


@pytest.mark.asyncio
async def test_a_pod_of_a_protected_workload_is_refused(cluster, monkeypatch):
    monkeypatch.setenv("K8S_AGENT_PROTECTED_WORKLOADS", "shop/checkout")
    result = await _propose_with(ScriptedLLM(_change(action="delete_pod", pod="checkout-1")), "delete pod checkout-1", _resolved("checkout"))
    assert "protected" in result["refusal"]["reason"] and not cluster.deletes


@pytest.mark.asyncio
async def test_an_approval_after_the_workload_became_protected_is_not_applied(cluster, monkeypatch):
    pa = await _approved(cluster, "scale checkout to 4", _SCALE)
    monkeypatch.setenv("K8S_AGENT_PROTECTED_WORKLOADS", "shop/checkout")
    with pytest.raises(RuntimeError, match="shop/checkout is protected"):
        await _apply(pa)
    assert not cluster.patches


@pytest.mark.asyncio
async def test_a_pod_delete_approved_before_its_workload_became_protected_is_not_applied(cluster, monkeypatch):
    pa = await _approved(cluster, "delete pod checkout-1", _change(action="delete_pod", pod="checkout-1"))
    monkeypatch.setenv("K8S_AGENT_PROTECTED_WORKLOADS", "shop/checkout")
    with pytest.raises(RuntimeError, match="protected"):
        await _apply(pa)
    assert not cluster.deletes


@pytest.mark.asyncio
async def test_the_clusters_admission_policy_answer_is_a_refusal(cluster, monkeypatch):
    async def denied(kind, name, namespace, body, content_type, dry_run, subresource=None):
        raise fx.api_error(
            403,
            'deployments.apps "checkout" is forbidden: ValidatingAdmissionPolicy \'ari-k8s-agent-protected\' with '
            "binding 'ari-k8s-agent-protected' denied request: protected workload",
            "Forbidden",
        )

    monkeypatch.setattr(cluster, "patch", denied)
    result = await _propose_with(ScriptedLLM(_SCALE), "scale checkout to 4", _resolved("checkout"))
    assert "protection policy denies" in result["refusal"]["reason"] and result["refusal"]["protected"] is True


def test_the_lists_come_from_the_environment(monkeypatch):
    monkeypatch.delenv("K8S_AGENT_PROTECTED_NAMESPACES", raising=False)
    monkeypatch.delenv("K8S_AGENT_PROTECTED_WORKLOADS", raising=False)
    assert "kube-system" in protection.protected_namespaces() and "monitoring" not in protection.protected_namespaces()
    # The ingress controllers and the service mesh carry the stg/prod clusters' traffic (2026-11-26).
    assert {"traefik", "ingress-nginx", "istio-system"} <= set(protection.protected_namespaces())
    assert protection.protected_workloads() == ()
    monkeypatch.setenv("K8S_AGENT_PROTECTED_NAMESPACES", "")
    assert protection.why_protected("kube-system", "coredns") is None
    monkeypatch.setenv("K8S_AGENT_PROTECTED_NAMESPACES", " vault , keda ")
    assert protection.protected_namespaces() == ("vault", "keda")


@pytest.mark.parametrize(
    "pattern,namespace,name,protected",
    [
        ("insightfinder/temporal-*", "insightfinder", "temporal-history", True),
        ("insightfinder/temporal-*", "other", "temporal-history", False),
        ("insightfinder/ari-k8s-worker", "insightfinder", "ari-k8s-worker", True),
        ("insightfinder/ari-k8s-worker", "insightfinder", "ari-k8s-worker-2", False),
        ("insightfinder/", "insightfinder", "anything", False),
    ],
)
def test_workload_patterns(monkeypatch, pattern, namespace, name, protected):
    monkeypatch.setenv("K8S_AGENT_PROTECTED_NAMESPACES", "")
    monkeypatch.setenv("K8S_AGENT_PROTECTED_WORKLOADS", pattern)
    assert (protection.why_protected(namespace, name) is not None) is protected
