"""`expand_pvc`: grow one StatefulSet claim-template PVC to a size the request names; everything else refused.

Uses the cluster of test_k8s_change_actions.py, plus StatefulSet `db` with claim template `data` (PVCs
data-db-0/1), a Helm-rendered PVC that shares the Deployment `checkout`'s name, and StorageClasses.
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
from test_k8s_capabilities import ScriptedLLM, _ctx  # noqa: E402
from test_k8s_change_actions import _apply, build_cluster  # noqa: E402

from k8s_agent.actions import expand_pvc  # noqa: E402
from k8s_agent.capabilities import find_workload, propose_change  # noqa: E402
from k8s_agent.tools import policy, rollout, write  # noqa: E402

_GROW = "expand pvc data-db-0 in namespace shop to 20Gi"


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.delenv("K8S_AGENT_PROTECTED_NAMESPACES", raising=False)
    monkeypatch.delenv("K8S_AGENT_PROTECTED_WORKLOADS", raising=False)
    fake = build_cluster(monkeypatch)
    fake.objects["StatefulSet"] = [fx.statefulset("db", labels=fx.HELM_LABELS, claim_templates={"data": "10Gi"})]
    fake.objects["PersistentVolumeClaim"] = [
        fx.pvc("data-db-0", labels={"app": "db"}),
        fx.pvc("data-db-1", labels={"app": "db"}),
        fx.pvc("checkout", size="5Gi", labels={"helm.toolkit.fluxcd.io/name": "shop", "app.kubernetes.io/managed-by": "Helm"},
               annotations={"meta.helm.sh/release-name": "shop"}),
        fx.pvc("scratch", size="5Gi"),
    ]
    fake.objects["StorageClass"] = [fx.storageclass("gp3"), fx.storageclass("fixed", expansion=False)]
    return fake


def _pvc_resolved(name, namespace="shop"):
    return {"find_workload": {"resolution": {"scope": "pvc", "kind": "PersistentVolumeClaim", "namespace": namespace, "name": name}}}


async def _propose(prompt, name="data-db-0", llm=None):
    return await propose_change.run(_ctx("propose_change", prompt, results=_pvc_resolved(name), llm=llm or ScriptedLLM()))


def _pvc(cluster, name):
    return next(o for o in cluster.objects["PersistentVolumeClaim"] if o["metadata"]["name"] == name)


async def _approved(cluster, prompt=_GROW, name="data-db-0"):
    result = await _propose(prompt, name)
    assert "pending_action" in result, result
    cluster.patches.clear()
    return fx.a2a_round_trip(result["pending_action"])


# ---------------------------------------------------------------- resolution


@pytest.mark.asyncio
async def test_a_disk_resize_naming_a_pvc_resolves_to_it_without_the_model(cluster):
    llm = ScriptedLLM()
    result = await find_workload.run(_ctx("find_workload", _GROW, llm=llm))
    assert result["resolution"] == {"scope": "pvc", "kind": "PersistentVolumeClaim", "namespace": "shop", "name": "data-db-0"}
    assert llm.prompts == []


@pytest.mark.asyncio
async def test_a_pvc_named_like_a_deployment_wins_only_for_a_disk_resize(cluster):
    llm = ScriptedLLM()
    disk = await find_workload.run(_ctx("find_workload", "grow the disk checkout to 10Gi", llm=llm))
    assert disk["resolution"]["kind"] == "PersistentVolumeClaim" and disk["resolution"]["name"] == "checkout"
    health = await find_workload.run(_ctx("find_workload", "why is checkout crashing? is its disk full?", llm=llm))
    assert health["resolution"] == {"scope": "deployment", "namespace": "shop", "name": "checkout"}
    assert llm.prompts == []


@pytest.mark.parametrize(
    "prompt",
    [
        "increase the memory limit of checkout to 2Gi, its volume is slow",
        "scale checkout to 4 replicas, the disk is fine",
        "raise the cpu of checkout to 2 and check its storage",
        "resize pod checkout-7d9f5c6b8-x2k4q in place: memory limit 2Gi, not the disk",
        "checkout reports ephemeral-storage pressure; increase its limit to 2Gi",
        "increase checkout to 3 replicas; node disk pressure on node-a",
    ],
)
@pytest.mark.asyncio
async def test_a_change_to_a_deployment_that_mentions_a_disk_stays_with_the_deployment(cluster, prompt):
    result = await find_workload.run(_ctx("find_workload", prompt, llm=ScriptedLLM()))
    assert (result["resolution"] or {}).get("kind", "Deployment") == "Deployment", result
    assert result["resolution"]["name"] == "checkout"


@pytest.mark.asyncio
async def test_a_pvc_named_without_a_disk_word_next_to_it_is_not_taken(cluster):
    result = await find_workload.run(_ctx("find_workload", "expand checkout to 10Gi; the old pvc was too small", llm=ScriptedLLM()))
    assert result["resolution"] == {"scope": "deployment", "namespace": "shop", "name": "checkout"}


@pytest.mark.asyncio
async def test_the_same_pvc_name_in_two_namespaces_gives_candidates(cluster):
    cluster.objects["PersistentVolumeClaim"].append(fx.pvc("data-db-0", namespace="other"))
    result = await find_workload.run(_ctx("find_workload", "expand pvc data-db-0 to 20Gi", llm=ScriptedLLM()))
    assert result["resolution"] is None and len(result["candidates"]) == 2
    named = await find_workload.run(_ctx("find_workload", "expand pvc data-db-0 in namespace other to 20Gi", llm=ScriptedLLM()))
    assert named["resolution"]["namespace"] == "other"


# ---------------------------------------------------------------- proposal


@pytest.mark.asyncio
async def test_growing_a_claim_template_pvc_is_proposed_by_code_alone(cluster):
    llm = ScriptedLLM()
    result = await _propose(_GROW, llm=llm)
    pa = result["pending_action"]
    assert llm.prompts == []
    assert pa["action"] == "expand_pvc" and pa["storage"] == "20Gi" and pa["pvc_uid"] == "uid-pvc-data-db-0"
    assert pa["target"] == {"kind": "PersistentVolumeClaim", "namespace": "shop", "name": "data-db-0"}
    assert pa["workload"] == "db" and pa["workload_kind"] == "StatefulSet"
    assert pa["before"] == {"storage": "10Gi"} and pa["after"] == {"storage": "20Gi"}
    assert pa["risk_tier"] == "high"
    assert any("cannot be made smaller" in r["message"] for r in pa["risks"])
    assert any("four times in 24 hours" in r["message"] for r in pa["risks"])
    assert any("claim template still asks for 10Gi" in n for n in pa["notes"])
    assert pa["persistence"]["needed"] is False
    assert cluster.patches == [
        {"kind": "PersistentVolumeClaim", "name": "data-db-0", "namespace": "shop",
         "body": {"metadata": {"uid": "uid-pvc-data-db-0"}, "spec": {"resources": {"requests": {"storage": "20Gi"}}}}, "dry_run": True,
         "content_type": cluster.patches[0]["content_type"]}
    ]
    assert _pvc(cluster, "data-db-0")["spec"]["resources"]["requests"]["storage"] == "10Gi"


@pytest.mark.parametrize(
    "prompt,reason",
    [
        ("shrink pvc data-db-0 in namespace shop to 5Gi", "only grows a disk"),
        ("expand pvc data-db-0 in namespace shop to 10Gi", "only grows a disk"),
        ("expand pvc data-db-0 in namespace shop to 61Gi", "at most 60Gi"),
        ("expand pvc data-db-0 in namespace shop to 15Gi or 20Gi", "several sizes"),
        ("expand pvc data-db-0 in namespace shop", "no size"),
        ("expand pvc data-db-0 in namespace shop to 30Gi by adding 10Gi", "both a size to reach"),
        ("expand pvc data-db-0 in namespace shop by 51Gi", "at most 60Gi"),
    ],
)
@pytest.mark.asyncio
async def test_sizes_that_are_not_a_clear_larger_size_are_refused(cluster, prompt, reason):
    result = await _propose(prompt)
    assert reason in result["refusal"]["reason"], result
    assert not cluster.patches


@pytest.mark.asyncio
async def test_the_ebs_note_also_covers_the_in_tree_provisioner(cluster):
    cluster.objects["StorageClass"] = [fx.storageclass("gp3", provisioner="kubernetes.io/aws-ebs")]
    pa = (await _propose(_GROW))["pending_action"]
    assert any("four times in 24 hours" in r["message"] for r in pa["risks"])
    cluster.objects["StorageClass"] = [fx.storageclass("gp3", provisioner="example.com/other")]
    other = (await _propose(_GROW))["pending_action"]
    assert not any("EBS" in r["message"] for r in other["risks"])


@pytest.mark.asyncio
async def test_the_cap_allows_fifty_gi_more_for_a_small_disk(cluster):
    result = await _propose("expand pvc data-db-0 in namespace shop to 60Gi")
    assert result["pending_action"]["storage"] == "60Gi"


@pytest.mark.parametrize(
    "prompt",
    ["expand pvc data-db-0 in namespace shop by 10Gi", "扩容 pvc data-db-0 增加 10Gi", "pvc data-db-0 的磁盘再加 10Gi", "grow pvc data-db-0 +10Gi"],
)
@pytest.mark.asyncio
async def test_an_amount_to_add_is_proposed_as_the_size_it_comes_to(cluster, prompt):
    pa = (await _propose(prompt))["pending_action"]
    assert pa["storage"] == "20Gi" and pa["after"] == {"storage": "20Gi"}
    assert pa["requested"]["computed"] == "10Gi + 10Gi = 20Gi"
    assert pa["action_name"].endswith("from 10Gi to 20Gi")
    assert cluster.patches[0]["body"] == {"metadata": {"uid": "uid-pvc-data-db-0"}, "spec": {"resources": {"requests": {"storage": "20Gi"}}}}


@pytest.mark.asyncio
async def test_an_added_amount_is_written_once_however_late_the_approval(cluster):
    pa = await _approved(cluster, "expand pvc data-db-0 in namespace shop by 10Gi")
    cluster.auto_expand = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and [p["body"] for p in cluster.patches] == [{"metadata": {"uid": "uid-pvc-data-db-0"}, "spec": {"resources": {"requests": {"storage": "20Gi"}}}}]
    again = await _apply(pa)
    assert again["applied"]["already_at_target"] is True and len(cluster.patches) == 1


@pytest.mark.asyncio
async def test_from_and_to_takes_the_target(cluster):
    result = await _propose("grow pvc data-db-0 from 10Gi to 20 GiB")
    assert result["pending_action"]["storage"] == "20Gi"


@pytest.mark.parametrize(
    "name,pvc,reason",
    [
        ("checkout", None, "Flux HelmRelease shop/shop"),
        ("kust", fx.pvc("kust", labels={"kustomize.toolkit.fluxcd.io/name": "apps"}), "Flux Kustomization apps"),
        ("argo", fx.pvc("argo", annotations={"argocd.argoproj.io/tracking-id": "x"}), "Argo CD"),
        ("data-db-1", fx.pvc("data-db-1", managers=["helm-controller"]), "field managers helm-controller"),
        ("scratch", None, "not created by a StatefulSet's volumeClaimTemplates"),
        ("data-db-1", fx.pvc("data-db-1", owners=[("Deployment", "x")]), "also owned by Deployment x"),
    ],
)
@pytest.mark.asyncio
async def test_a_pvc_something_re_applies_is_refused(cluster, name, pvc, reason):
    if pvc is not None:
        cluster.objects["PersistentVolumeClaim"] = [p for p in cluster.objects["PersistentVolumeClaim"] if p["metadata"]["name"] != name] + [pvc]
    result = await _propose(f"expand pvc {name} in namespace shop to 20Gi", name)
    assert reason in result["refusal"]["reason"], result
    assert not cluster.patches


@pytest.mark.asyncio
async def test_a_pvc_applied_with_kubectl_is_refused(cluster):
    _pvc(cluster, "data-db-0")["metadata"]["annotations"]["kubectl.kubernetes.io/last-applied-configuration"] = '{"kind":"PersistentVolumeClaim"}'
    result = await _propose(_GROW)
    assert "kubectl apply" in result["refusal"]["reason"] and not cluster.patches


@pytest.mark.asyncio
async def test_a_pvc_of_an_operator_owned_statefulset_is_refused(cluster):
    cluster.objects["StatefulSet"][0]["metadata"]["ownerReferences"] = [{"kind": "Prometheus", "name": "k8s"}]
    result = await _propose(_GROW)
    assert "which Prometheus k8s manages" in result["refusal"]["reason"] and not cluster.patches


@pytest.mark.asyncio
async def test_the_statefulsets_own_and_its_pods_ownership_is_allowed(cluster):
    _pvc(cluster, "data-db-0")["metadata"]["ownerReferences"] = [{"kind": "StatefulSet", "name": "db"}, {"kind": "Pod", "name": "db-0"}]
    assert "pending_action" in await _propose(_GROW)


@pytest.mark.asyncio
async def test_a_pvc_of_a_protected_statefulset_is_refused(cluster, monkeypatch):
    monkeypatch.setenv("K8S_AGENT_PROTECTED_WORKLOADS", "shop/db")
    result = await _propose(_GROW)
    assert result["refusal"]["protected"] is True and "StatefulSet db" in result["refusal"]["reason"]
    assert not cluster.patches


@pytest.mark.asyncio
async def test_a_pvc_in_a_protected_namespace_is_refused_before_reading_it(cluster):
    result = await propose_change.run(_ctx("propose_change", "expand pvc data-vault-0 to 2Gi", results=_pvc_resolved("data-vault-0", "vault"), llm=ScriptedLLM()))
    assert result["refusal"]["protected"] is True and "Namespace vault is protected" in result["refusal"]["reason"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"phase": "Pending"}, "not Bound"),
        ({"storage_class": "fixed"}, "does not allow volume expansion"),
        ({"storage_class": "gone"}, "does not exist"),
        ({"conditions": ["FileSystemResizePending"]}, "already under way"),
        ({"resize_status": "ControllerResizeInProgress"}, "already under way"),
        ({"requested": "15Gi"}, "already under way"),
    ],
)
@pytest.mark.asyncio
async def test_a_pvc_that_cannot_be_grown_now_is_refused(cluster, change, reason):
    cluster.objects["PersistentVolumeClaim"] = [p for p in cluster.objects["PersistentVolumeClaim"] if p["metadata"]["name"] != "data-db-0"] + [
        fx.pvc("data-db-0", **change)
    ]
    result = await _propose(_GROW)
    assert reason in result["refusal"]["reason"], result


@pytest.mark.asyncio
async def test_a_question_about_a_pvc_is_not_a_resize(cluster):
    result = await _propose("how big is pvc data-db-0?")
    assert result["refusal"]["asked_for"] == "expand_pvc" and not cluster.patches


@pytest.mark.asyncio
async def test_a_disk_request_about_a_statefulset_names_its_pvcs(cluster):
    results = {"find_workload": {"resolution": {"scope": "workload", "kind": "StatefulSet", "namespace": "shop", "name": "db"}}}
    result = await propose_change.run(_ctx("propose_change", "grow the disk of statefulset db to 20Gi", results=results, llm=ScriptedLLM()))
    reason = result["refusal"]["reason"]
    assert "read only" in reason and "data-db-0, data-db-1" in reason


@pytest.mark.asyncio
async def test_the_clusters_admission_policy_answer_is_a_refusal(cluster, monkeypatch):
    async def denied(kind, name, namespace, body, content_type, dry_run, subresource=None):
        raise fx.api_error(403, "persistentvolumeclaims \"data-db-0\" is forbidden: ValidatingAdmissionPolicy 'ari-k8s-protected' denied request", "Forbidden")

    monkeypatch.setattr(cluster, "patch", denied)
    result = await _propose(_GROW)
    assert result["refusal"]["protected"] is True


@pytest.mark.asyncio
async def test_a_missing_rbac_grant_is_a_refusal_naming_it(cluster, monkeypatch):
    async def forbidden(kind, name, namespace, body, content_type, dry_run, subresource=None):
        raise fx.api_error(403, "persistentvolumeclaims \"data-db-0\" is forbidden: User cannot patch resource", "Forbidden")

    monkeypatch.setattr(cluster, "patch", forbidden)
    result = await _propose(_GROW)
    assert 'patch on "persistentvolumeclaims"' in result["refusal"]["reason"]


# ---------------------------------------------------------------- approval


@pytest.mark.asyncio
async def test_an_approved_growth_is_applied_once_and_verified(cluster):
    pa = await _approved(cluster)
    cluster.auto_expand = True
    result = await _apply(pa)
    assert result["status"] == "succeeded" and result["verification"]["capacity"] == "20Gi"
    assert [p["dry_run"] for p in cluster.patches] == [False]
    assert result["target"] == {"kind": "PersistentVolumeClaim", "namespace": "shop", "name": "data-db-0"}
    assert json.dumps(result).count(fx.CANARY) == 0


@pytest.mark.asyncio
async def test_a_size_someone_else_already_asked_for_is_not_reported_as_applied(cluster):
    from common.capabilities_base import StepRefused

    pa = await _approved(cluster)
    _pvc(cluster, "data-db-0")["spec"]["resources"]["requests"]["storage"] = "20Gi"
    with pytest.raises(StepRefused, match="already holds what was approved, done by someone else"):
        await _apply(pa, attempt=1)
    assert not cluster.patches


@pytest.mark.asyncio
async def test_a_retried_approval_finds_the_size_asked_and_does_not_write_again(cluster):
    """The first attempt's own write (field manager ari-k8s-agent) landed before the worker was lost."""
    pa = await _approved(cluster)
    await _grow_as(cluster, "ari-k8s-agent")
    result = await _apply(pa, attempt=2)
    assert result["status"] == "succeeded" and result["applied"]["already_at_target"] is True
    assert result["applied"]["field_manager"] == "ari-k8s-agent"
    assert not cluster.patches


async def _grow_as(cluster, manager):
    await cluster.patch("PersistentVolumeClaim", "data-db-0", "shop", {"spec": {"resources": {"requests": {"storage": "20Gi"}}}},
                        "application/merge-patch+json", False, field_manager=manager)
    _pvc(cluster, "data-db-0")["status"]["capacity"]["storage"] = "20Gi"
    cluster.patches.clear()


@pytest.mark.asyncio
async def test_a_retried_approval_refuses_a_size_someone_else_set(cluster):
    """The first attempt failed before writing and someone grew the PVC by hand before the retry: the retry finds
    the size owned by kubectl, not this agent, and refuses instead of reporting it as applied."""
    pa = await _approved(cluster)
    await _grow_as(cluster, "kubectl-patch")
    with pytest.raises(RuntimeError, match="already holds what was approved, done by someone else"):
        await _apply(pa, attempt=2)
    assert not cluster.patches


@pytest.mark.asyncio
async def test_a_retried_approval_with_no_owner_on_record_is_refused_as_undecidable(cluster):
    pa = await _approved(cluster)
    _pvc(cluster, "data-db-0")["spec"]["resources"]["requests"]["storage"] = "20Gi"
    _pvc(cluster, "data-db-0")["status"]["capacity"]["storage"] = "20Gi"
    with pytest.raises(RuntimeError, match="records no field manager for it") as refused:
        await _apply(pa, attempt=2)
    assert "Nothing was changed" not in str(refused.value)
    assert not cluster.patches


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda p: p["spec"]["resources"]["requests"].update(storage="12Gi"), "requested size changed"),
        (lambda p: p["metadata"]["annotations"].update({"meta.helm.sh/release-name": "db"}), "Helm release db"),
        (lambda p: p["metadata"].update(uid="uid-new"), "replaced"),
    ],
)
@pytest.mark.asyncio
async def test_an_approval_after_the_pvc_changed_is_not_applied(cluster, mutate, message):
    pa = await _approved(cluster)
    mutate(_pvc(cluster, "data-db-0"))
    with pytest.raises(RuntimeError, match=message):
        await _apply(pa)
    assert not cluster.patches


@pytest.mark.asyncio
async def test_an_approval_after_the_statefulset_became_protected_is_not_applied(cluster, monkeypatch):
    pa = await _approved(cluster)
    monkeypatch.setenv("K8S_AGENT_PROTECTED_WORKLOADS", "shop/db")
    with pytest.raises(RuntimeError, match="protected"):
        await _apply(pa)
    assert not cluster.patches


@pytest.mark.asyncio
async def test_a_volume_still_growing_is_pending_not_failed(cluster):
    result = await rollout.wait_pvc_expanded("shop", "data-db-0", "uid-pvc-data-db-0", "20Gi", 0, poll_seconds=0)
    assert result["status"] == "resize_pending" and "grows on its own" in result["detail"]
    _pvc(cluster, "data-db-0")["status"]["allocatedResourceStatuses"] = {"storage": "NodeResizeInfeasible"}
    failed = await rollout.wait_pvc_expanded("shop", "data-db-0", "uid-pvc-data-db-0", "20Gi", 0, poll_seconds=0)
    assert failed["status"] == "resize_failed"
    gone = await rollout.wait_pvc_expanded("shop", "data-db-0", "uid-other", "20Gi", 0, poll_seconds=0)
    assert gone["status"] == "pvc_replaced"


# ---------------------------------------------------------------- parsing and rules


@pytest.mark.parametrize(
    "prompt,value",
    [
        ("expand pvc x to 20Gi", "20Gi"),
        ("expand pvc x to 20 GiB", "20Gi"),
        ("grow the disk to 500G", "500G"),
        ("grow the disk to 1.5TB", "1.5T"),
        ("把 pvc data-loki-0 扩容到 100Gi", "100Gi"),
        ("grow pvc data-loki-0 from 50Gi to 100Gi", "100Gi"),
    ],
)
def test_the_size_is_read_from_the_requests_words(prompt, value):
    asked, problem = policy.requested_storage(prompt)
    assert problem is None and asked["value"] == value and asked["relative"] is False


@pytest.mark.parametrize(
    "prompt,value",
    [
        ("expand pvc x by 10Gi", "10Gi"),
        ("add 5 GiB to pvc x", "5Gi"),
        ("把 pvc x 增加 10Gi", "10Gi"),
        ("pvc x 再加 20G", "20G"),
        ("grow pvc x from 50Gi, add 10Gi", "10Gi"),
    ],
)
def test_an_amount_to_add_is_read_as_relative(prompt, value):
    asked, problem = policy.requested_storage(prompt)
    assert problem is None and asked["value"] == value and asked["relative"] is True


def test_increase_to_is_a_size_to_reach():
    asked, _ = policy.requested_storage("把 pvc x 增加到 100Gi")
    assert asked["value"] == "100Gi" and asked["relative"] is False


@pytest.mark.parametrize(
    "prompt,asks",
    [
        ("expand pvc data-db-0 to 20Gi", True),
        ("把 data-loki-0 的磁盘扩容到 100Gi", True),
        ("how big is the disk of loki", False),
        ("scale checkout to 4", False),
        ("don't resize the volume", False),
    ],
)
def test_a_disk_resize_is_asked_for_in_words(prompt, asks):
    assert policy.asks_disk_size_change(prompt) is asks


@pytest.mark.asyncio
async def test_a_pvc_name_two_statefulsets_could_have_made_is_refused(cluster, monkeypatch):
    # data-a-b-0: template "data" of StatefulSet "a-b", or template "data-a" of StatefulSet "b".
    cluster.objects["StatefulSet"] += [fx.statefulset("a-b", claim_templates={"data": "10Gi"}), fx.statefulset("b", claim_templates={"data-a": "10Gi"})]
    cluster.objects["PersistentVolumeClaim"].append(fx.pvc("data-a-b-0"))
    monkeypatch.setenv("K8S_AGENT_PROTECTED_WORKLOADS", "")
    result = await _propose("expand pvc data-a-b-0 in namespace shop to 20Gi", "data-a-b-0")
    assert "several StatefulSets" in result["refusal"]["reason"] and not cluster.patches


def test_the_claim_template_rule():
    sts = [{"name": "redis-cluster", "volume_claim_templates": [{"name": "redis-data"}]}]
    assert expand_pvc.claim_template_owner({"name": "redis-data-redis-cluster-3"}, sts)[1:] == ("redis-data", 3)
    assert expand_pvc.claim_template_owner({"name": "redis-data-redis-cluster-x"}, sts) is None
    assert expand_pvc.claim_template_owner({"name": "redis-data-redis-3"}, sts) is None


# ---------------------------------------------------------------- names, late approvals, older charts


def test_the_pvc_names_are_left_out_of_the_disk_check():
    prompt = "expand pvc redis-data-redis-replicas-0 to 20Gi"
    assert not policy.asks_disk_size_change(prompt)
    assert policy.asks_disk_size_change(prompt, ["redis-data-redis-replicas-0"])
    assert not policy.asks_disk_size_change(prompt + " and set replicas to 3", ["redis-data-redis-replicas-0"])
    in_namespace = "expand pvc data-db-0 in namespace memory-db to 20Gi"
    assert policy.asks_disk_size_change(in_namespace, ["data-db-0", "memory-db"])


@pytest.mark.asyncio
async def test_a_pvc_whose_name_says_replicas_can_be_grown(cluster):
    # Bitnami Redis names its replicas' PVCs redis-data-redis-replicas-<n>.
    cluster.objects["StatefulSet"].append(fx.statefulset("redis-replicas", claim_templates={"redis-data": "8Gi"}))
    cluster.objects["PersistentVolumeClaim"].append(fx.pvc("redis-data-redis-replicas-0", size="8Gi"))
    prompt = "expand pvc redis-data-redis-replicas-0 in namespace shop to 16Gi"
    llm = ScriptedLLM()
    resolved = await find_workload.run(_ctx("find_workload", prompt, llm=llm))
    assert resolved["resolution"]["name"] == "redis-data-redis-replicas-0" and llm.prompts == []
    result = await _propose(prompt, "redis-data-redis-replicas-0")
    assert result["pending_action"]["storage"] == "16Gi"


@pytest.mark.asyncio
async def test_the_write_is_tied_to_the_approved_pvc(cluster):
    # A PVC deleted and made again under the same name between the approval-time re-read and the write.
    action = {"action": "expand_pvc", "namespace": "shop", "pvc": "data-db-0", "pvc_uid": "uid-of-the-old-pvc", "storage": "20Gi"}
    with pytest.raises(RuntimeError, match="field is immutable"):
        await write.patch_workload(action)
    assert _pvc(cluster, "data-db-0")["spec"]["resources"]["requests"]["storage"] == "10Gi"


@pytest.mark.asyncio
async def test_an_approval_after_the_storageclass_stopped_allowing_expansion_is_refused(cluster):
    pa = await _approved(cluster)
    next(s for s in cluster.objects["StorageClass"] if s["metadata"]["name"] == "gp3")["allowVolumeExpansion"] = False
    with pytest.raises(RuntimeError, match="no longer allows volume expansion"):
        await _apply(pa)
    assert not cluster.patches


@pytest.mark.asyncio
async def test_an_approval_after_the_pvc_lost_its_volume_is_refused(cluster):
    pa = await _approved(cluster)
    _pvc(cluster, "data-db-0")["status"]["phase"] = "Lost"
    with pytest.raises(RuntimeError, match="is Lost now, not Bound"):
        await _apply(pa)
    assert not cluster.patches


@pytest.mark.asyncio
async def test_an_older_chart_without_the_reads_is_refused_with_the_rule(cluster):
    cluster.forbidden.add("StatefulSet")
    result = await _propose(_GROW)
    assert 'lacks patch on "persistentvolumeclaims"' in result["refusal"]["reason"] and not cluster.patches


@pytest.mark.asyncio
async def test_a_resize_error_is_named_when_the_wait_ends(cluster):
    pvc = _pvc(cluster, "data-db-0")
    pvc["spec"]["resources"]["requests"]["storage"] = "20Gi"
    pvc["status"]["conditions"] = [{"type": "ControllerResizeError", "status": "True"}]
    out = await rollout.wait_pvc_expanded("shop", "data-db-0", "uid-pvc-data-db-0", "20Gi", 0)
    assert out["status"] == "resize_pending" and "reported ControllerResizeError" in out["detail"]
    assert "grows on its own" not in out["detail"]
