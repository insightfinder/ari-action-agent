"""Lowering CPU/memory (`set_resources`, `resize_pod`): a memory request or limit goes down only to at least
1.2 x the container's current usage, read from metrics.k8s.io at proposal and again at approval; any decrease is
a `high` risk; `set_resources` under Recreate is `high` (user decision 2026-11-26).

Uses the fake cluster of test_k8s_capabilities.py: `checkout` runs container `app` with requests 500m/1Gi and
limits 1/2Gi in its template; checkout-1 and checkout-2 run, checkout-3 is crash-looping (no metrics sample).
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_fixtures as fx  # noqa: E402
import test_k8s_capabilities as base  # noqa: E402
from test_k8s_capabilities import ScriptedLLM, _change, _ctx, _proposal, _resolved  # noqa: E402

from k8s_agent.actions.set_resources import decreases, peak_usage  # noqa: E402
from k8s_agent.capabilities import apply_approved_change, propose_change  # noqa: E402
from k8s_agent.tools import quantities  # noqa: E402

cluster = base.cluster  # the same fake cluster fixture


def _usage(cluster, **memory_by_pod):
    """metrics-server samples: `checkout_1="600Mi"` gives checkout-1 that memory (cpu 300m unless `cpu_<pod>`)."""
    cpu = {k[4:]: v for k, v in memory_by_pod.items() if k.startswith("cpu_")}
    cluster.objects["PodMetrics"] = [
        fx.pod_metrics(pod.replace("_", "-"), memory=memory, cpu=cpu.get(pod, "300m"))
        for pod, memory in memory_by_pod.items()
        if not pod.startswith("cpu_")
    ]


def _resources_change(side, resource, value, quote):
    return _change(action="set_resources", container="app", resources={side: {resource: {"value": value, "quote": quote}}})


async def _propose(cluster, prompt, change):
    return await propose_change.run(_ctx("propose_change", prompt, results=_resolved("checkout"), llm=ScriptedLLM(change)))


def _checkout(cluster):
    return next(d for d in cluster.objects["Deployment"] if d["metadata"]["name"] == "checkout")


# ---------------------------------------------------------------- the rule itself


def test_a_limit_where_there_was_none_and_a_request_below_its_defaulted_limit_are_decreases():
    mi = quantities.MI
    assert decreases({"requests": {"memory": "1Gi"}, "limits": {}}, {("limits", "memory"): 4096 * mi}) == [
        ("limits", "memory", None, 4096 * mi)
    ]
    # A request left out defaults to the limit: setting one lower takes memory away.
    assert decreases({"limits": {"memory": "2Gi"}}, {("requests", "memory"): 1024 * mi}) == [
        ("requests", "memory", 2048 * mi, 1024 * mi)
    ]
    # No request and no limit: nothing was reserved, so a new request takes nothing away.
    assert decreases({}, {("requests", "memory"): 1024 * mi}) == []
    assert decreases({"limits": {"memory": "2Gi"}}, {("limits", "memory"): 3072 * mi}) == []


def test_peak_usage_is_the_highest_sample_of_the_pods_and_says_when_there_is_none():
    usage = {"pods": ["shop/a", "shop/b", "shop/c"], "usage": {"shop/a": {"app": {"cpu": "200m", "memory": "600Mi"}}, "shop/b": {"app": {"cpu": "900m", "memory": "500Mi"}}}}
    peak, gap = peak_usage(usage, "app")
    assert gap is None and peak == {"cpu": 900, "memory": 600 * quantities.MI, "pods": 2}
    _peak, gap = peak_usage({"pods": ["shop/a"], "usage": {}}, "app")
    assert "no usage for container app in 1 pod(s)" in gap
    _peak, gap = peak_usage({"pods": ["shop/a"], "gap": "metrics.k8s.io unavailable: 404"}, "app")
    assert gap == "metrics.k8s.io unavailable: 404"


# ---------------------------------------------------------------- set_resources: propose


@pytest.mark.asyncio
async def test_a_memory_decrease_is_refused_when_usage_cannot_be_read(cluster):
    result = await _propose(cluster, "set the memory limit of checkout to 1Gi", _resources_change("limits", "memory", "1Gi", "memory limit of checkout to 1Gi"))
    reason = result["refusal"]["reason"]
    assert "lowers the memory limit of container app" in reason and "could not" in reason
    assert not cluster.patches


@pytest.mark.asyncio
async def test_a_memory_limit_below_the_margin_over_the_busiest_pod_is_refused(cluster):
    # checkout-2 uses 1500Mi: 1.2 x 1500Mi = 1800Mi is the lowest limit proposed.
    _usage(cluster, checkout_1="600Mi", checkout_2="1500Mi")
    result = await _propose(cluster, "set the memory limit of checkout to 1700Mi", _resources_change("limits", "memory", "1700Mi", "memory limit of checkout to 1700Mi"))
    reason = result["refusal"]["reason"]
    assert "below 1.2 x the current memory usage of container app (1500Mi, the highest of 2 pod(s))" in reason
    assert "lowest value proposed is 1800Mi" in reason
    assert not cluster.patches


@pytest.mark.asyncio
async def test_a_memory_decrease_above_the_margin_is_proposed_as_high_with_the_usage(cluster):
    _usage(cluster, checkout_1="600Mi", checkout_2="700Mi")
    result = await _propose(cluster, "set the memory limit of checkout to 1Gi", _resources_change("limits", "memory", "1Gi", "memory limit of checkout to 1Gi"))
    pa = result["pending_action"]
    assert pa["resources"] == {"limits": {"memory": "1Gi"}}
    assert pa["risk_tier"] == "high"
    [lowered] = [r for r in pa["risks"] if r["message"].startswith("This lowers")]
    assert lowered["level"] == "high"
    assert "limits.memory 2Gi -> 1Gi" in lowered["message"] and "memory 700Mi (highest of 2 pod(s)" in lowered["message"]
    assert "does not undo it" in lowered["message"]


@pytest.mark.asyncio
async def test_a_memory_request_decrease_is_held_to_the_same_margin(cluster):
    _usage(cluster, checkout_1="900Mi", checkout_2="800Mi")
    result = await _propose(cluster, "set the memory request of checkout to 1000Mi", _resources_change("requests", "memory", "1000Mi", "memory request of checkout to 1000Mi"))
    assert "A memory request of 1000Mi would be below 1.2 x" in result["refusal"]["reason"]


@pytest.mark.asyncio
async def test_a_cpu_decrease_needs_no_usage_but_is_high(cluster):
    result = await _propose(cluster, "set the cpu limit of checkout to 500m", _resources_change("limits", "cpu", "500m", "cpu limit of checkout to 500m"))
    pa = result["pending_action"]
    assert pa["risk_tier"] == "high"
    [lowered] = [r for r in pa["risks"] if r["message"].startswith("This lowers")]
    assert "limits.cpu 1 -> 500m" in lowered["message"] and "Current usage: unknown" in lowered["message"]


@pytest.mark.asyncio
async def test_a_cpu_limit_below_the_current_cpu_usage_says_so(cluster):
    _usage(cluster, checkout_1="600Mi", cpu_checkout_1="800m")
    result = await _propose(cluster, "set the cpu limit of checkout to 500m", _resources_change("limits", "cpu", "500m", "cpu limit of checkout to 500m"))
    [lowered] = [r for r in result["pending_action"]["risks"] if r["message"].startswith("This lowers")]
    assert "below the current CPU usage (800m)" in lowered["message"]


@pytest.mark.asyncio
async def test_a_memory_limit_added_where_there_was_none_is_a_decrease(cluster):
    del _checkout(cluster)["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]
    _usage(cluster, checkout_1="600Mi", checkout_2="700Mi")
    result = await _propose(cluster, "set the memory limit of checkout to 4Gi", _resources_change("limits", "memory", "4Gi", "memory limit of checkout to 4Gi"))
    pa = result["pending_action"]
    assert pa["risk_tier"] == "high"
    assert any("limits.memory unset (no limit) -> 4Gi" in r["message"] for r in pa["risks"])


@pytest.mark.asyncio
async def test_a_raise_is_unaffected_and_needs_no_metrics(cluster):
    result = await _propose(cluster, "set the memory limit of checkout to 3Gi", _resources_change("limits", "memory", "3Gi", "memory limit of checkout to 3Gi"))
    pa = result["pending_action"]
    assert pa["risk_tier"] == "medium"
    assert not any(r["message"].startswith("This lowers") for r in pa["risks"])


@pytest.mark.asyncio
async def test_set_resources_under_recreate_is_high(cluster):
    _checkout(cluster)["spec"]["strategy"] = {"type": "Recreate"}
    result = await _propose(cluster, "set the memory limit of checkout to 3Gi", _resources_change("limits", "memory", "3Gi", "memory limit of checkout to 3Gi"))
    pa = result["pending_action"]
    assert pa["risk_tier"] == "high"
    assert any(r["level"] == "high" and r["message"].startswith("Strategy Recreate") for r in pa["risks"])


# ---------------------------------------------------------------- set_resources: approval


@pytest.mark.asyncio
async def test_an_approval_after_usage_grew_past_the_margin_is_refused(cluster):
    _usage(cluster, checkout_1="600Mi", checkout_2="700Mi")
    pa = await _proposal(cluster, "set the memory limit of checkout to 1Gi", _resources_change("limits", "memory", "1Gi", "memory limit of checkout to 1Gi"), "checkout")
    _usage(cluster, checkout_1="600Mi", checkout_2="900Mi")  # 1.2 x 900Mi = 1080Mi > 1Gi
    with pytest.raises(RuntimeError, match="usage changed: A memory limit of 1Gi would be below 1.2 x"):
        await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True))
    assert not cluster.patches


@pytest.mark.asyncio
async def test_an_approval_when_metrics_are_gone_is_refused(cluster):
    _usage(cluster, checkout_1="600Mi", checkout_2="700Mi")
    pa = await _proposal(cluster, "set the memory limit of checkout to 1Gi", _resources_change("limits", "memory", "1Gi", "memory limit of checkout to 1Gi"), "checkout")
    cluster.not_served.add("PodMetrics")
    with pytest.raises(RuntimeError, match="usage changed: .*could not"):
        await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True))
    assert not cluster.patches


@pytest.mark.asyncio
async def test_an_approved_decrease_with_usage_still_low_is_applied(cluster):
    _usage(cluster, checkout_1="600Mi", checkout_2="700Mi")
    pa = await _proposal(cluster, "set the memory limit of checkout to 1Gi", _resources_change("limits", "memory", "1Gi", "memory limit of checkout to 1Gi"), "checkout")
    cluster.auto_rollout = True
    result = await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True))
    assert result["action_result"]["status"] == "succeeded"
    assert [p["dry_run"] for p in cluster.patches] == [False]


# ---------------------------------------------------------------- resize_pod


def _restart_on_memory(cluster, name="checkout-1"):
    pod = next(p for p in cluster.objects["Pod"] if p["metadata"]["name"] == name)
    pod["spec"]["containers"][0]["resizePolicy"] = [
        {"resourceName": "cpu", "restartPolicy": "NotRequired"},
        {"resourceName": "memory", "restartPolicy": "RestartContainer"},
    ]
    for holder in (pod["spec"]["containers"][0], pod["status"]["containerStatuses"][0]):
        holder["resources"]["limits"] = {"memory": "2Gi"}


@pytest.mark.asyncio
async def test_an_in_place_memory_decrease_is_held_to_the_pods_own_usage(cluster):
    _restart_on_memory(cluster)
    _usage(cluster, checkout_1="1500Mi", checkout_2="100Mi")  # only checkout-1's own sample counts
    change = _change(action="resize_pod", pod="checkout-1", container="app", resources={"limits": {"memory": {"value": "1Gi", "quote": "memory limit 1Gi"}}})
    result = await _propose(cluster, "resize pod checkout-1 in place: memory limit 1Gi", change)
    assert "(1500Mi, the highest of 1 pod(s))" in result["refusal"]["reason"]


@pytest.mark.asyncio
async def test_an_in_place_memory_decrease_above_the_margin_is_high(cluster):
    _restart_on_memory(cluster)
    _usage(cluster, checkout_1="600Mi")
    change = _change(action="resize_pod", pod="checkout-1", container="app", resources={"limits": {"memory": {"value": "1Gi", "quote": "memory limit 1Gi"}}})
    pa = (await _propose(cluster, "resize pod checkout-1 in place: memory limit 1Gi", change))["pending_action"]
    assert pa["risk_tier"] == "high"
    assert any(r["message"].startswith("This lowers limits.memory 2Gi -> 1Gi") for r in pa["risks"])


@pytest.mark.asyncio
async def test_an_in_place_approval_after_the_pods_usage_grew_is_refused(cluster):
    _restart_on_memory(cluster)
    _usage(cluster, checkout_1="600Mi")
    change = _change(action="resize_pod", pod="checkout-1", container="app", resources={"limits": {"memory": {"value": "1Gi", "quote": "memory limit 1Gi"}}})
    pa = await _proposal(cluster, "resize pod checkout-1 in place: memory limit 1Gi", change, "checkout")
    _usage(cluster, checkout_1="1000Mi")
    with pytest.raises(RuntimeError, match="usage changed"):
        await apply_approved_change.run(_ctx("apply_approved_change", "", params=pa, confirmed=True))
    assert not cluster.patches
