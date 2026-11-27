"""tools/ownership: who owns a field in managedFields, and the fake cluster's model of it."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import k8s_fixtures as fx  # noqa: E402

from k8s_agent.tools import ownership  # noqa: E402

# As the API server recorded them on test (2026-11-27): kubectl's resize of a pod, and this agent's scale.
_POD = [
    {"manager": "kubelet", "operation": "Update", "subresource": "status", "fieldsV1": {"f:status": {"f:phase": {}}}},
    {
        "manager": "kubectl-patch",
        "operation": "Update",
        "subresource": "resize",
        "fieldsV1": {"f:spec": {"f:containers": {'k:{"name":"app"}': {"f:resources": {"f:limits": {"f:cpu": {}}}}}}},
    },
]
_DEPLOYMENT = [
    {"manager": "ari-k8s-agent", "operation": "Update", "subresource": "scale", "fieldsV1": {"f:spec": {"f:replicas": {}}}},
    {"manager": "kube-controller-manager", "operation": "Update", "subresource": "status", "fieldsV1": {"f:status": {"f:replicas": {}}}},
]


def test_a_field_is_owned_by_the_manager_that_set_it():
    cpu = ["f:spec", "f:containers", ownership.keyed("app"), "f:resources", "f:limits", "f:cpu"]
    assert ownership.owners(_POD, cpu) == ["kubectl-patch"]
    assert ownership.owners(_DEPLOYMENT, ["f:spec", "f:replicas"]) == ["ari-k8s-agent"]


def test_a_status_field_of_the_same_name_is_not_the_spec_field():
    assert ownership.owners(_DEPLOYMENT, ["f:status", "f:replicas"]) == ["kube-controller-manager"]
    assert ownership.owners(_POD, ["f:spec", "f:containers", ownership.keyed("app"), "f:resources", "f:limits", "f:memory"]) == []


def test_a_keyed_item_matches_however_its_json_is_spaced():
    managed = [{"manager": "m", "fieldsV1": {"f:spec": {"f:containers": {'k:{"name": "app"}': {"f:image": {}}}}}}]
    assert ownership.owners(managed, ["f:spec", "f:containers", ownership.keyed("app"), "f:image"]) == ["m"]
    assert ownership.owners(managed, ["f:spec", "f:containers", ownership.keyed("sidecar"), "f:image"]) == []


def test_shared_ownership_lists_every_manager():
    managed = [*_DEPLOYMENT, {"manager": "flux", "operation": "Apply", "fieldsV1": {"f:spec": {"f:replicas": {}}}}]
    assert ownership.owners(managed, ["f:spec", "f:replicas"]) == ["ari-k8s-agent", "flux"]


def test_nothing_on_record_owns_nothing():
    assert ownership.owners(None, ["f:spec", "f:replicas"]) == []


@pytest.mark.asyncio
async def test_the_fake_cluster_moves_ownership_only_when_a_value_changes():
    """As checked on test: a write of the value already there leaves the field with its owner."""
    cluster = fx.FakeClient({"Deployment": [fx.deployment()]})
    merge = "application/merge-patch+json"
    await cluster.patch("Deployment", "checkout", "shop", {"spec": {"replicas": 5}}, merge, False, "scale")
    await cluster.patch("Deployment", "checkout", "shop", {"spec": {"replicas": 5}}, merge, False, field_manager="kubectl-patch")
    managed = cluster.objects["Deployment"][0]["metadata"]["managedFields"]
    assert ownership.owners(managed, ["f:spec", "f:replicas"]) == ["ari-k8s-agent"]
    await cluster.patch("Deployment", "checkout", "shop", {"spec": {"replicas": 2}}, merge, False, field_manager="kubectl-patch")
    managed = cluster.objects["Deployment"][0]["metadata"]["managedFields"]
    assert ownership.owners(managed, ["f:spec", "f:replicas"]) == ["kubectl-patch"]
