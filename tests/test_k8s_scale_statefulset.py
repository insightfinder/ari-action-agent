"""`scale_replicas` on a StatefulSet, end to end against the fake API, and the Deployment scale it must keep.

Uses the cluster of test_k8s_change_actions.py: StatefulSet `db` (2 replicas, OrderedReady, no volume claim
templates unless a test adds them) with pods db-0/db-1.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
from test_k8s_capabilities import ScriptedLLM, _change, _ctx  # noqa: E402
from test_k8s_change_actions import _apply, _approved, _obj, _propose, _workload, build_cluster  # noqa: E402

from k8s_agent.capabilities import find_workload  # noqa: E402
from k8s_agent.tools import governance, rollout  # noqa: E402

pytestmark = pytest.mark.asyncio
_DB = _workload("StatefulSet", "db")


@pytest.fixture
def cluster(monkeypatch):
    return build_cluster(monkeypatch)


def _scale(n, quote=None):
    return _change(action="scale_replicas", replicas=n, replicas_quote=quote or str(n))


def _with_volumes(cluster, when_scaled=None):
    sts = _obj(cluster, "StatefulSet", "db")
    sts["spec"]["volumeClaimTemplates"] = [
        {"metadata": {"name": "data"}, "spec": {"storageClassName": "gp3", "resources": {"requests": {"storage": "10Gi"}}}}
    ]
    if when_scaled:
        sts["spec"]["persistentVolumeClaimRetentionPolicy"] = {"whenDeleted": "Retain", "whenScaled": when_scaled}
    cluster.objects["PersistentVolumeClaim"] = [fx.pvc("data-db-0"), fx.pvc("data-db-1")]
    return sts


# ---------------------------------------------------------------- propose


async def test_scale_up_of_a_statefulset_is_proposed(cluster):
    pa = await _approved(cluster, "scale statefulset db to 3 replicas", _scale(3), _DB)
    assert pa["target"] == {"kind": "StatefulSet", "namespace": "shop", "name": "db"}
    assert pa["deployment"] is None and pa["workload_kind"] == "StatefulSet" and pa["replicas"] == 3
    assert pa["risk_tier"] == "medium" and pa["action_name"] == "Scale StatefulSet shop/db from 2 to 3 replicas"
    assert any("Adds pod(s) db-2" in n for n in pa["notes"]) and any("OrderedReady" in n for n in pa["notes"])
    assert pa["capacity_outlook"]["new_pods"] == 1
    assert pa["snapshot"]["replicas"] == 2 and pa["snapshot"]["pvc_retention"]["when_scaled"] == "Retain"


async def test_the_dry_run_patches_the_statefulset_itself(cluster):
    await _propose("scale statefulset db to 3", _scale(3), _DB)
    [dry] = cluster.patches
    assert dry["kind"] == "StatefulSet" and dry["dry_run"] is True and dry["body"] == {"spec": {"replicas": 3}}


async def test_scale_down_of_a_statefulset_is_high_risk(cluster):
    pa = await _approved(cluster, "scale db down to 1 replica", _scale(1), _DB)
    assert pa["risk_tier"] == "high"
    assert any("Removes pod(s) db-1" in r["message"] and r["level"] == "high" for r in pa["risks"])


async def test_scale_down_names_the_pvcs_it_keeps(cluster):
    _with_volumes(cluster)
    pa = await _approved(cluster, "scale db to 1", _scale(1), _DB)
    assert any("data-db-1" in n and "kept with their data" in n for n in pa["notes"])


async def test_scale_down_that_would_delete_pvcs_is_refused(cluster):
    _with_volumes(cluster, when_scaled="Delete")
    result = await _propose("scale db to 1", _scale(1), _DB)
    assert "whenScaled: Delete" in result["refusal"]["reason"] and not cluster.patches


async def test_scale_up_under_whenscaled_delete_is_still_proposed(cluster):
    _with_volumes(cluster, when_scaled="Delete")
    assert "pending_action" in await _propose("scale db to 3", _scale(3), _DB)


async def test_scale_up_says_which_pvcs_are_created_and_which_reattach(cluster):
    _with_volumes(cluster)
    cluster.objects["PersistentVolumeClaim"].append(fx.pvc("data-db-2", created="2026-11-01T00:00:00Z"))
    pa = await _approved(cluster, "scale db to 4", _scale(4), _DB)
    [reuse] = [r for r in pa["risks"] if "already exist" in r["message"]]
    assert reuse["level"] == "medium" and "data-db-2" in reuse["message"] and "2026-11-01" in reuse["message"]
    assert any("Creates PersistentVolumeClaim(s) data-db-3 " in n and "10Gi" in n for n in pa["notes"])


async def test_unreadable_pvcs_are_said_not_refused(cluster):
    _with_volumes(cluster)
    cluster.forbidden.add("PersistentVolumeClaim")
    pa = await _approved(cluster, "scale db to 3", _scale(3), _DB)
    assert any("could not be read" in n for n in pa["notes"])


async def test_ordinals_start_names_the_pods(cluster):
    _obj(cluster, "StatefulSet", "db")["spec"]["ordinals"] = {"start": 5}
    pa = await _approved(cluster, "scale db to 3", _scale(3), _DB)
    assert any("Adds pod(s) db-7" in n for n in pa["notes"])


async def test_pods_not_ready_under_ordered_ready_are_a_risk(cluster):
    _obj(cluster, "StatefulSet", "db")["status"]["readyReplicas"] = 1
    pa = await _approved(cluster, "scale db to 3", _scale(3), _DB)
    assert any("1 of 2 pod(s) are not ready" in r["message"] for r in pa["risks"])


async def test_an_autoscaled_statefulset_is_refused(cluster):
    cluster.objects["HorizontalPodAutoscaler"].append(fx.hpa("db-hpa", "db", kind="StatefulSet"))
    result = await _propose("scale db to 3", _scale(3), _DB)
    refusal = result["refusal"]
    assert refusal["autoscaler"]["name"] == "db-hpa" and "where it is defined" in refusal["reason"]
    assert not cluster.patches


async def test_a_statefulset_an_operator_controls_is_refused(cluster):
    _obj(cluster, "StatefulSet", "db")["metadata"]["ownerReferences"] = [
        {"kind": "RabbitmqCluster", "name": "mq", "controller": True}
    ]
    result = await _propose("scale db to 3", _scale(3), _DB)
    assert "RabbitmqCluster mq" in result["refusal"]["reason"] and not cluster.patches


async def test_a_count_the_request_does_not_state_is_refused(cluster):
    result = await _propose("scale db up a bit", _scale(3, quote="3"), _DB)
    assert "explicitly" in result["refusal"]["reason"]


async def test_a_daemonset_is_not_scaled(cluster):
    result = await _propose("scale the agent daemonset to 3", _scale(3), _workload("DaemonSet", "agent"))
    assert "refusal" in result and not cluster.patches


def _forbid_patch(cluster, monkeypatch, kind):
    real_patch = cluster.patch

    async def forbidden(k, name, namespace, body, content_type, dry_run, subresource=None):
        if k == kind:
            raise fx.api_error(403, f'{kind.lower()}s "{name}" is forbidden', "Forbidden")
        return await real_patch(k, name, namespace, body, content_type, dry_run, subresource)

    monkeypatch.setattr(cluster, "patch", forbidden)


async def test_a_403_on_a_statefulset_scale_names_the_missing_rule(cluster, monkeypatch):
    _forbid_patch(cluster, monkeypatch, "StatefulSet")
    result = await _propose("scale db to 3", _scale(3), _DB)
    assert 'patch on "statefulsets"' in result["refusal"]["reason"]


async def test_a_403_on_a_deployment_scale_does_not_blame_the_statefulset_rule(cluster, monkeypatch):
    _forbid_patch(cluster, monkeypatch, "Deployment")
    with pytest.raises(RuntimeError, match="forbidden"):
        await _propose("scale checkout to 4", _scale(4), None)


async def test_a_statefulset_whose_name_starts_another_is_resolved_exactly(cluster):
    """The e2e fixtures e2e-sts and e2e-sts-pvc: neither name takes over the other."""
    cluster.objects["StatefulSet"] += [fx.statefulset("e2e-sts"), fx.statefulset("e2e-sts-pvc", replicas=1)]
    for prompt, name in (("restart statefulset e2e-sts in namespace shop", "e2e-sts"), ("scale statefulset e2e-sts-pvc to 2", "e2e-sts-pvc")):
        result = await find_workload.run(_ctx("find_workload", prompt, llm=ScriptedLLM()))
        assert result["resolution"]["name"] == name, (prompt, result)


# ---------------------------------------------------------------- apply


async def test_apply_scales_the_statefulset_and_verifies(cluster):
    pa = await _approved(cluster, "scale db to 3", _scale(3), _DB)
    cluster.auto_rollout = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and result["target"]["kind"] == "StatefulSet"
    [write] = cluster.patches
    assert write["kind"] == "StatefulSet" and write["dry_run"] is False and write["body"] == {"spec": {"replicas": 3}}
    assert _obj(cluster, "StatefulSet", "db")["spec"]["replicas"] == 3


async def test_apply_refuses_after_the_count_changed(cluster):
    pa = await _approved(cluster, "scale db to 3", _scale(3), _DB)
    _obj(cluster, "StatefulSet", "db")["spec"]["replicas"] = 4
    with pytest.raises(RuntimeError, match="replicas changed from 2 to 4"):
        await _apply(pa)
    assert not cluster.patches


async def test_apply_refuses_after_the_retention_policy_changed(cluster):
    _with_volumes(cluster)
    pa = await _approved(cluster, "scale db to 1", _scale(1), _DB)
    _obj(cluster, "StatefulSet", "db")["spec"]["persistentVolumeClaimRetentionPolicy"] = {"whenScaled": "Delete"}
    with pytest.raises(RuntimeError, match="retention policy"):
        await _apply(pa)
    assert not cluster.patches


async def test_apply_refuses_when_an_autoscaler_appeared(cluster):
    pa = await _approved(cluster, "scale db to 3", _scale(3), _DB)
    cluster.objects["HorizontalPodAutoscaler"].append(fx.hpa("db-hpa", "db", kind="StatefulSet"))
    with pytest.raises(RuntimeError, match="an autoscaler now governs this StatefulSet"):
        await _apply(pa)


async def test_a_retried_apply_verifies_without_writing_again(cluster):
    pa = await _approved(cluster, "scale db to 3", _scale(3), _DB)
    cluster.auto_rollout = True
    await _apply(pa)
    cluster.patches.clear()
    result = await _apply(pa)
    assert result["applied"]["already_at_target"] and not cluster.patches and result["status"] == "succeeded"


def test_a_scale_down_is_not_done_while_the_removed_pod_terminates():
    view = {"kind": "StatefulSet", "generation": 5, "observed_generation": 5, "replicas": 2,
            "status": {"replicas": 3, "ready": 2, "current_revision": "a", "update_revision": "a"}}
    assert not rollout.replicas_reached(view, 2)
    view["status"]["replicas"] = 2
    assert rollout.replicas_reached(view, 2)


def test_a_scale_is_done_even_with_a_template_update_pending():
    view = {"kind": "StatefulSet", "generation": 5, "observed_generation": 5, "replicas": 3,
            "status": {"replicas": 3, "ready": 3, "current_revision": "a", "update_revision": "b"}}
    assert rollout.replicas_reached(view, 3) and not rollout.workload_rolled_out(view)


# ---------------------------------------------------------------- the Deployment scale stays as it was


async def test_a_deployment_is_still_scaled_through_its_scale_subresource(cluster):
    pa = await _approved(cluster, "scale checkout to 4", _scale(4), None)
    assert pa["deployment"] == "checkout" and pa["target"]["kind"] == "Deployment" and pa["risk_tier"] == "low"
    assert pa["snapshot"]["deployment_replicas"] == 3
    cluster.auto_rollout = True
    assert (await _apply(pa))["status"] == "succeeded"
    [write] = cluster.patches
    assert write["kind"] == "Scale" and write["body"] == {"spec": {"replicas": 4}}


async def test_a_deployment_proposal_from_before_statefulsets_still_applies(cluster):
    """Approved across the deploy: the older image's proposal had no workload_kind, workload or target."""
    pa = await _approved(cluster, "scale checkout to 4", _scale(4), None)
    for key in ("workload_kind", "workload", "target"):
        pa.pop(key)
    cluster.auto_rollout = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and result["target"] == {"kind": "Deployment", "namespace": "shop", "name": "checkout"}
    [write] = cluster.patches
    assert write["kind"] == "Scale" and write["name"] == "checkout"


def test_find_autoscaler_matches_the_target_kind():
    hpas = [{"name": "db-hpa", "target": {"kind": "StatefulSet", "name": "db"}, "min": 2, "max": 4}]
    assert governance.find_autoscaler("db", [], hpas) is None
    assert governance.find_autoscaler("db", [], hpas, "StatefulSet")["name"] == "db-hpa"
    sos = [{"kind": "ScaledObject", "name": "db-so", "target": {"kind": "StatefulSet", "name": "db"}}]
    assert governance.find_autoscaler("db", sos, [], "StatefulSet")["name"] == "db-so"
    assert governance.find_autoscaler("db", sos, []) is None
