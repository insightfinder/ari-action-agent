"""`resize_pod`: CPU/memory of one running pod changed in place (pods/resize), end to end against the fake API.

Uses the same fake cluster as test_k8s_capabilities.py: `checkout` has three pods (checkout-1 on node-a,
checkout-2 and checkout-3 on node-b), node-a has 7910m CPU with 1910m requested by its other pods.
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

from k8s_agent import actions  # noqa: E402
from k8s_agent.actions.resize_pod import qos_class  # noqa: E402
from k8s_agent.capabilities import apply_approved_change, find_workload, propose_change  # noqa: E402

pytestmark = pytest.mark.asyncio
cluster = base.cluster  # the same fake cluster fixture

_CPU_LIMIT = _change(action="resize_pod", pod="checkout-1", container="app", resources={"limits": {"cpu": {"value": "2", "quote": "cpu limit 2"}}})
_MEM_LIMIT = _change(action="resize_pod", pod="checkout-1", container="app", resources={"limits": {"memory": {"value": "3Gi", "quote": "memory limit 3Gi"}}})


@pytest.fixture(autouse=True)
def _usage(cluster):
    """metrics-server samples for the running checkout pods (checkout-3 is crash-looping: none)."""
    cluster.objects["PodMetrics"] = [fx.pod_metrics("checkout-1"), fx.pod_metrics("checkout-2")]


def _set_limits(cluster, name="checkout-1", **limits):
    """Give the pod's container limits (spec and the kubelet's status alike)."""
    pod = _pod(cluster, name)
    pod["spec"]["containers"][0]["resources"]["limits"].update(limits)
    pod["status"]["containerStatuses"][0]["resources"]["limits"].update(limits)


def _pod(cluster, name="checkout-1"):
    return next(p for p in cluster.objects["Pod"] if p["metadata"]["name"] == name)


def _set_policy(cluster, memory="RestartContainer", name="checkout-1"):
    _pod(cluster, name)["spec"]["containers"][0]["resizePolicy"] = [
        {"resourceName": "cpu", "restartPolicy": "NotRequired"},
        {"resourceName": "memory", "restartPolicy": memory},
    ]


async def _propose(cluster, prompt, change):
    return await propose_change.run(_ctx("propose_change", prompt, results=_resolved("checkout"), llm=ScriptedLLM(change)))


async def _approved(cluster, prompt, change):
    result = await _propose(cluster, prompt, change)
    cluster.patches.clear()
    return fx.a2a_round_trip(result["pending_action"])


# ---------------------------------------------------------------- propose


async def test_cpu_resize_of_a_named_pod_is_proposed_in_place(cluster):
    _set_limits(cluster, cpu="1")
    result = await _propose(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    pa = result["pending_action"]
    assert pa["action"] == "resize_pod" and pa["pod"] == "checkout-1" and pa["deployment"] == "checkout"
    assert pa["target"] == {"kind": "Pod", "namespace": "shop", "name": "checkout-1"}
    assert pa["resources"] == {"limits": {"cpu": "2"}}
    assert pa["before"] == {"limits.cpu": "1"} and pa["after"] == {"limits.cpu": "2"}
    assert pa["risk_tier"] == "low"  # a raise of cpu under the default NotRequired: no restart
    assert "without a restart" in pa["notes"][0]
    assert pa["snapshot"]["pod_uid"] == "uid-checkout-1"
    # The dry run went to the pod's resize subresource, as a strategic merge by container name.
    [dry] = cluster.patches
    assert dry["kind"] == "Pod" and dry["name"] == "checkout-1" and dry["dry_run"] is True
    assert dry["body"] == {"spec": {"containers": [{"name": "app", "resources": {"limits": {"cpu": "2"}}}]}}
    # Lifetime and persistence speak about the Deployment template, not a Flux revert of the pod.
    assert "until this pod is replaced" in pa["drift_notice"]
    assert pa["persistence"]["changes"][0]["object"] == "Deployment shop/checkout container app"
    assert pa["persistence"]["changes"][0]["from"] == "1" and pa["persistence"]["changes"][0]["to"] == "2"
    assert fx.CANARY not in json.dumps(result)


async def test_a_resize_back_to_the_template_value_has_nothing_to_persist(cluster):
    # The Deployment's template already has cpu limit 1; the pod gets it back in place.
    change = _change(action="resize_pod", pod="checkout-1", container="app", resources={"limits": {"cpu": {"value": "1", "quote": "cpu limit 1"}}})
    pa = (await _propose(cluster, "resize pod checkout-1 in place: cpu limit 1", change))["pending_action"]
    assert pa["persistence"]["needed"] is False and pa["persistence"]["changes"] == []
    assert "already holds these values" in pa["persistence"]["reason"]


async def test_memory_is_refused_in_place_unless_the_container_restarts_for_it(cluster):
    result = await _propose(cluster, "resize pod checkout-1 in place: memory limit 3Gi", _MEM_LIMIT)
    assert "memory resizePolicy NotRequired" in result["refusal"]["reason"]
    assert "pending_action" not in result


async def test_memory_with_restart_container_is_proposed_as_medium(cluster):
    _set_policy(cluster)
    _set_limits(cluster, memory="2Gi")
    pa = (await _propose(cluster, "resize pod checkout-1 in place: memory limit 3Gi", _MEM_LIMIT))["pending_action"]
    assert pa["risk_tier"] == "medium"
    assert "restarts in place" in pa["notes"][0]


async def test_a_deployment_with_several_pods_needs_the_pod_named(cluster):
    change = _change(action="resize_pod", container="app", resources={"limits": {"cpu": {"value": "2", "quote": "cpu limit 2"}}})
    result = await _propose(cluster, "resize checkout in place: cpu limit 2", change)
    reason = result["refusal"]["reason"]
    assert "3 running pods" in reason and result["refusal"]["pods"] == ["checkout-1", "checkout-2", "checkout-3"]


async def test_the_only_pod_is_used_when_none_is_named(cluster):
    cluster.objects["Pod"] = [p for p in cluster.objects["Pod"] if p["metadata"]["name"] not in ("checkout-2", "checkout-3")]
    change = _change(action="resize_pod", container="app", resources={"limits": {"cpu": {"value": "2", "quote": "cpu limit 2"}}})
    pa = (await _propose(cluster, "resize checkout in place: cpu limit 2", change))["pending_action"]
    assert pa["pod"] == "checkout-1"


async def test_a_pod_name_not_in_the_request_is_refused(cluster):
    result = await _propose(cluster, "resize the first checkout pod in place: cpu limit 2", _CPU_LIMIT)
    assert "is not a running pod of shop/checkout named in the request" in result["refusal"]["reason"]


async def test_unsupported_cluster_is_refused_with_the_version(cluster):
    cluster.resize_supported = False
    cluster.server_git_version = "v1.32.9"
    result = await _propose(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    assert "does not serve pods/resize" in result["refusal"]["reason"] and "v1.32.9" in result["refusal"]["reason"]
    assert not cluster.patches


async def test_unreadable_discovery_counts_as_unsupported(cluster):
    cluster.discovery_error = fx.api_error(403, "forbidden")
    result = await _propose(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    assert "could not be read" in result["refusal"]["reason"]


async def test_an_old_kubelet_on_the_pods_node_is_refused(cluster):
    node_a = next(n for n in cluster.objects["Node"] if n["metadata"]["name"] == "node-a")
    node_a["status"]["nodeInfo"]["kubeletVersion"] = "v1.32.4-eks-1"
    result = await _propose(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    assert "runs kubelet v1.32.4-eks-1" in result["refusal"]["reason"]


async def test_a_request_increase_must_fit_the_pods_own_node(cluster):
    # node-a: 7910m allocatable, 910m (ds-a) + 500m (checkout-1) + 500m (worker-1) requested -> 6000m free.
    change = _change(action="resize_pod", pod="checkout-1", container="app", resources={"requests": {"cpu": {"value": "7", "quote": "cpu request 7"}}, "limits": {"cpu": {"value": "7", "quote": "cpu limit 7"}}})
    result = await _propose(cluster, "resize pod checkout-1 in place: cpu request 7, cpu limit 7", change)
    assert "node-a has 6 cpu left unrequested" in result["refusal"]["reason"]


async def test_a_qos_class_change_is_refused(cluster):
    _set_policy(cluster)
    change = _change(
        action="resize_pod",
        pod="checkout-1",
        container="app",
        resources={"limits": {"cpu": {"value": "500m", "quote": "cpu limit 500m"}, "memory": {"value": "1Gi", "quote": "memory limit 1Gi"}}},
    )
    result = await _propose(cluster, "resize pod checkout-1 in place: cpu limit 500m, memory limit 1Gi", change)
    assert "QoS class from Burstable to Guaranteed" in result["refusal"]["reason"]


async def test_a_403_on_the_dry_run_is_a_refusal_naming_the_rbac_rule(cluster, monkeypatch):
    real_patch = cluster.patch

    async def forbidden(kind, name, namespace, body, content_type, dry_run, subresource=None):
        if subresource == "resize":
            raise fx.api_error(403, 'pods "checkout-1" is forbidden: cannot patch resource "pods/resize"', "Forbidden")
        return await real_patch(kind, name, namespace, body, content_type, dry_run, subresource)

    monkeypatch.setattr(cluster, "patch", forbidden)
    result = await _propose(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    assert 'patch on "pods/resize"' in result["refusal"]["reason"] and "Nothing was proposed" in result["refusal"]["reason"]


# ---------------------------------------------------------------- apply


async def test_apply_resizes_the_same_pod_and_verifies_it(cluster):
    _set_policy(cluster)
    pa = await _approved(cluster, "resize pod checkout-1 in place: memory limit 3Gi", _MEM_LIMIT)
    cluster.auto_resize = True
    ar = (await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True)))["action_result"]
    assert [p["dry_run"] for p in cluster.patches] == [False]
    assert cluster.patches[0]["kind"] == "Pod" and cluster.patches[0]["name"] == "checkout-1"
    assert ar["status"] == "succeeded", ar
    assert ar["verification"]["restart_count"] == 1  # RestartContainer: one restart, same pod
    assert ar["target"] == {"kind": "Pod", "namespace": "shop", "name": "checkout-1"}
    assert ar["applied"]["resources"]["limits"]["memory"] == "3Gi"
    assert "until this pod is replaced" in ar["drift_notice"]
    # The Deployment template is untouched.
    dep = next(d for d in cluster.objects["Deployment"] if d["metadata"]["name"] == "checkout")
    assert dep["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "2Gi"
    assert fx.CANARY not in json.dumps(ar)


async def test_apply_refuses_when_the_pod_was_replaced(cluster):
    pa = await _approved(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    _pod(cluster)["metadata"]["uid"] = "uid-a-new-pod"
    with pytest.raises(RuntimeError, match="was replaced"):
        await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True))
    assert not cluster.patches


async def test_apply_refuses_when_the_pod_is_gone(cluster):
    pa = await _approved(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    cluster.objects["Pod"] = [p for p in cluster.objects["Pod"] if p["metadata"]["name"] != "checkout-1"]
    with pytest.raises(RuntimeError, match="no longer exists"):
        await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True))


async def test_a_resize_someone_else_already_made_is_not_reported_as_applied(cluster):
    from common.capabilities_base import StepRefused

    pa = await _approved(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    _set_limits(cluster, cpu="2")
    with pytest.raises(StepRefused, match="already holds what was approved, done by someone else"):
        await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True, attempt=1))
    assert not [p for p in cluster.patches if not p["dry_run"]]


async def test_a_retried_apply_after_the_patch_landed_verifies_instead_of_refusing(cluster):
    pa = await _approved(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    cluster.auto_resize = True
    await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True))
    again = (await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True)))["action_result"]
    assert again["applied"]["already_at_target"] is True and again["status"] == "succeeded"
    assert len([p for p in cluster.patches if not p["dry_run"]]) == 1


async def test_an_infeasible_resize_is_reported_at_once(cluster):
    pa = await _approved(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    _pod(cluster)["status"]["conditions"] = [
        {"type": "PodResizePending", "status": "True", "reason": "Infeasible", "message": "Node didn't have enough capacity"}
    ]
    ar = (await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True)))["action_result"]
    assert ar["status"] == "resize_infeasible" and "enough capacity" in ar["verification"]["detail"]


async def test_apply_needs_the_resize_still_supported(cluster):
    pa = await _approved(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    cluster.resize_supported = False
    with pytest.raises(RuntimeError, match="no longer available"):
        await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True))


# ---------------------------------------------------------------- resolution and rules


async def test_find_workload_resolves_a_named_pod_to_its_deployment(cluster):
    llm = ScriptedLLM()
    result = await find_workload.run(_ctx("find_workload", "resize pod checkout-7d9f5c6b8-x2k4q in place: cpu limit 2", llm=llm))
    assert result["resolution"] == {"scope": "deployment", "namespace": "shop", "name": "checkout"}
    assert llm.prompts == []


async def test_the_extraction_prompt_offers_every_registered_action_and_a_pod_field(cluster):
    llm = ScriptedLLM(_CPU_LIMIT)
    await propose_change.run(_ctx("propose_change", "resize pod checkout-1 in place: cpu limit 2", results=_resolved("checkout"), llm=llm))
    prompt = llm.prompts[0]
    # Every action a Deployment can take; the CronJob-only ones are offered only for a CronJob.
    offered = [name for name, spec in actions.REGISTRY.items() if "Deployment" in spec.workload_kinds]
    assert all(f'"{name}"' in prompt for name in offered) and '"pod":' in prompt
    assert not any(f'"{name}"' in prompt for name in actions.REGISTRY if name not in offered)


async def test_qos_class():
    assert qos_class({"a": {"requests": {}, "limits": {}}}) == "BestEffort"
    assert qos_class({"a": {"requests": {"cpu": "1"}, "limits": {}}}) == "Burstable"
    assert qos_class({"a": {"requests": {}, "limits": {"cpu": "1", "memory": "1Gi"}}}) == "Guaranteed"
    assert qos_class({"a": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "1Gi"}}}) == "Burstable"


async def test_naming_a_pod_without_asking_for_in_place_plans_the_deployment_change(cluster):
    """On-call prompts quote pod names from findings; the model may still pick resize_pod. Code decides."""
    prompt = "checkout-1 was OOMKilled; set the memory limit of container app in deployment checkout to 3Gi"
    change = _change(action="resize_pod", pod="checkout-1", container="app", resources={"limits": {"memory": {"value": "3Gi", "quote": "memory limit of container app in deployment checkout to 3Gi"}}})
    pa = (await _propose(cluster, prompt, change))["pending_action"]
    assert pa["action"] == "set_resources" and pa["target"]["kind"] == "Deployment"


async def test_a_cluster_below_the_version_floor_is_refused_even_when_it_serves_resize(cluster):
    cluster.server_git_version = "v1.34.12-eks-91f89a9"  # prod today: pods/resize served as beta
    result = await _propose(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    assert "only from 1.35" in result["refusal"]["reason"] and "v1.34.12" in result["refusal"]["reason"]


async def test_a_kubelet_below_the_floor_is_refused(cluster):
    node_a = next(n for n in cluster.objects["Node"] if n["metadata"]["name"] == "node-a")
    node_a["status"]["nodeInfo"]["kubeletVersion"] = "v1.34.2-eks-1"
    result = await _propose(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT)
    assert "runs kubelet v1.34.2-eks-1" in result["refusal"]["reason"]


async def test_a_cpu_resize_without_restart_has_no_startup_estimate(cluster):
    pa = (await _propose(cluster, "resize pod checkout-1 in place: cpu limit 2", _CPU_LIMIT))["pending_action"]
    assert pa["estimated_seconds_per_pod_to_ready"] is None


async def test_wait_resize_waits_for_the_expected_restart(cluster):
    """Under RestartContainer, values in status with the OLD container still ready are not the result."""
    from k8s_agent.tools import rollout

    pod = _pod(cluster)
    pod["spec"]["containers"][0]["resources"]["limits"] = {"memory": "3Gi"}
    pod["status"]["containerStatuses"][0]["resources"] = {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"memory": "3Gi"}}
    approved = {"limits": {"memory": "3Gi"}}
    waiting = await rollout.wait_resize("shop", "checkout-1", "app", approved, "uid-checkout-1", 0, restarts_before=0)
    assert waiting["status"] == "in_progress"
    pod["status"]["containerStatuses"][0]["restartCount"] = 1
    done = await rollout.wait_resize("shop", "checkout-1", "app", approved, "uid-checkout-1", 0, restarts_before=0)
    assert done["status"] == "succeeded"


@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("resize pod checkout-1 in place: cpu limit 2", True),
        ("raise the cpu limit of checkout-1 to 2 without a restart", True),
        ("change memory to 4Gi with no rollout", True),
        ("原地把 checkout-1 的 cpu limit 调到 2", True),
        ("resize the checkout-1 pod: cpu limit 2", True),
        ("checkout-1 was OOMKilled; set the memory limit of deployment checkout to 3Gi", False),
        ("set the cpu limit of checkout to 2", False),
    ],
)
async def test_asks_in_place(prompt, expected):
    from k8s_agent.tools import policy

    assert policy.asks_in_place(prompt) is expected
