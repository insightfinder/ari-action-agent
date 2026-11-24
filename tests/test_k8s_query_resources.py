"""`query_resources`: tables over any readable kind, end to end against the fake API.

The cluster is test_k8s_capabilities.py's: namespace `shop` with checkout-1..3, worker-1 and two DaemonSet
pods (ds-a, ds-b), each with one container `app`.
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
from test_k8s_capabilities import ScriptedLLM, _ctx  # noqa: E402

from common.models.plan import Plan, Step  # noqa: E402
from k8s_agent import meta as k8s_meta  # noqa: E402
from k8s_agent.capabilities import query_resources  # noqa: E402
from k8s_agent.tools import catalog, table  # noqa: E402

pytestmark = pytest.mark.asyncio
cluster = base.cluster


def _q(**fields):
    return "QUERY_JSON: " + json.dumps(fields)


@pytest.fixture
def shop(cluster):
    cluster.objects["Namespace"] = [{"metadata": {"name": n}, "status": {"phase": "Active"}} for n in ("shop", "kube-system")]
    cluster.objects["PodMetrics"] = [
        {"metadata": {"name": "checkout-1", "namespace": "shop"}, "containers": [{"name": "app", "usage": {"cpu": "250m", "memory": "1843Mi"}}]},
        {"metadata": {"name": "checkout-2", "namespace": "shop"}, "containers": [{"name": "app", "usage": {"cpu": "100m", "memory": "512Mi"}}]},
    ]
    for name in ("checkout-1", "checkout-2"):
        pod = next(p for p in cluster.objects["Pod"] if p["metadata"]["name"] == name)
        pod["spec"]["containers"][0]["resources"]["limits"] = {"cpu": "1", "memory": "2Gi"}
    return cluster


async def _run(prompt, query, params=None):
    return await query_resources.run(_ctx("query_resources", prompt, llm=ScriptedLLM(_q(**query)), params=params))


def _rows(result):
    t = result["table"]
    return [dict(zip(t["columns"], r)) for r in t["rows"]]


# ---------------------------------------------------------------- the stg question


async def test_every_container_with_limits_and_usage_in_a_namespace(shop):
    prompt = "list all pods with cpu and memory limit, usage for shop"
    result = await _run(prompt, {"kind": "Container", "namespace": "shop"})
    t = result["table"]
    assert t["kind"] == "Container" and t["query"]["namespace"] == "shop" and t["query"]["all_namespaces"] is False
    rows = _rows(result)
    # Every container of every active pod, not only the unhealthy ones.
    assert sorted(r["pod"] for r in rows) == ["checkout-1", "checkout-2", "checkout-3", "ds-a", "ds-b", "worker-1"]
    assert t["matched"] == t["returned"] == 6 and t["truncated"] is False
    c1 = next(r for r in rows if r["pod"] == "checkout-1")
    assert c1["owner_kind"] == "Deployment" and c1["owner"] == "checkout"
    assert c1["memory_limit"] == "2Gi" and c1["memory_usage"] == "1843Mi" and c1["memory_usage_pct_of_limit"] == 90.0
    assert c1["cpu_usage"] == "250m" and c1["cpu_usage_pct_of_limit"] == 25.0 and c1["cpu_usage_pct_of_request"] == 50.0
    # No usage -> null, never 0 and never NaN.
    worker = next(r for r in rows if r["pod"] == "worker-1")
    assert worker["memory_usage"] is None and worker["memory_usage_pct_of_limit"] is None
    assert fx.CANARY not in json.dumps(result)
    # The artifact survives the A2A round trip unchanged in shape.
    assert fx.a2a_round_trip(t)["columns"] == t["columns"]


async def test_metrics_unavailable_is_a_gap(shop):
    shop.forbidden.add("PodMetrics")
    result = await _run("list all pods with cpu and memory limit, usage for shop", {"kind": "Container", "namespace": "shop"})
    assert any("usage columns are empty" in g for g in result["table"]["gaps"])
    assert result["table"]["returned"] == 6


# ---------------------------------------------------------------- what the model may not decide


async def test_a_namespace_the_request_does_not_name_is_not_applied(shop):
    result = await _run("list all pods with their memory limits", {"kind": "Container", "namespace": "shop"})
    t = result["table"]
    assert t["query"]["all_namespaces"] is True and any("namespace 'shop'" in i for i in t["ignored"])


async def test_the_callers_namespace_param_is_used_and_said(shop):
    result = await _run("list all pods with their memory limits", {"kind": "Container"}, params={"namespace": "shop"})
    assert result["table"]["query"]["namespace"] == "shop"


async def test_an_unknown_namespace_is_a_refusal_listing_namespaces(shop):
    result = await _run("list pods in shp", {"kind": "Pod", "namespace": "shp"})
    assert "does not exist" in result["refusal"]["reason"] and "shop" in result["refusal"]["reason"]


async def test_namespaces_not_listable_still_answers_with_a_note(shop):
    shop.forbidden.add("Namespace")
    result = await _run("list pods in shop", {"kind": "Pod", "namespace": "shop"})
    assert result["table"]["returned"] == 6 and any("not checked" in n for n in result["table"]["notes"])


async def test_a_label_selector_not_written_in_the_request_is_ignored(shop):
    result = await _run("list the checkout pods in shop", {"kind": "Pod", "namespace": "shop", "label_selector": "app=checkout"})
    t = result["table"]
    assert t["query"]["label_selector"] is None and t["returned"] == 6 and t["ignored"]
    verbatim = await _run("list pods with app=checkout in shop", {"kind": "Pod", "namespace": "shop", "label_selector": "app=checkout"})
    assert verbatim["table"]["returned"] == 3


async def test_filters_need_their_value_in_the_request(shop):
    prompt = "which containers in shop use more than 80% of their memory limit"
    applied = await _run(prompt, {"kind": "Container", "namespace": "shop", "filters": [{"column": "memory_usage_pct_of_limit", "op": ">", "value": 80}]})
    assert [r["pod"] for r in _rows(applied)] == ["checkout-1"]
    guessed = await _run("which containers in shop use a lot of memory", {"kind": "Container", "namespace": "shop", "filters": [{"column": "memory_usage_pct_of_limit", "op": ">", "value": 80}]})
    assert guessed["table"]["returned"] == 6 and any("not stated" in i for i in guessed["table"]["ignored"])


async def test_secrets_are_refused(shop):
    result = await _run("list the secrets in shop", {"kind": "Secret", "namespace": "shop"})
    assert "never read" in result["refusal"]["reason"]


async def test_an_unknown_kind_is_a_refusal_listing_kinds(shop):
    result = await _run("list the widgets", {"kind": "Widget"})
    assert "Container" in result["refusal"]["kinds"]


async def test_a_kind_not_granted_is_a_gap_not_an_empty_table(shop):
    shop.forbidden.add("ConfigMap")
    result = await _run("list configmaps in shop", {"kind": "ConfigMap", "namespace": "shop"})
    t = result["table"]
    assert t["returned"] == 0 and any("not allowed to list ConfigMap" in g for g in t["gaps"])
    assert not any("nothing of this kind matched" in n for n in t["notes"])


async def test_a_kind_the_cluster_does_not_serve_is_a_gap(shop):
    shop.not_served.add("ScaledObject")
    result = await _run("list scaledobjects in shop", {"kind": "ScaledObject", "namespace": "shop"})
    assert any("does not serve ScaledObject" in g for g in result["table"]["gaps"])


# ---------------------------------------------------------------- sort, cap, columns


async def test_sort_descending_puts_missing_values_last(shop):
    result = await _run("containers in shop by memory usage", {"kind": "Container", "namespace": "shop", "sort": [{"column": "memory_usage", "desc": True}]})
    pods = [r["pod"] for r in _rows(result)]
    assert pods[:2] == ["checkout-1", "checkout-2"] and set(pods[2:]) == {"checkout-3", "ds-a", "ds-b", "worker-1"}


async def test_rows_are_capped_and_the_count_says_so(shop):
    shop.objects["Pod"] = [fx.pod(f"bulk-{i}", app="bulk") for i in range(250)]
    result = await _run("list pods in shop", {"kind": "Pod", "namespace": "shop"})
    t = result["table"]
    assert t["matched"] == 250 and t["returned"] == table.MAX_ROWS and t["truncated"] is True


async def test_a_limit_the_request_states_is_applied(shop):
    result = await _run("top 2 containers in shop by cpu usage", {"kind": "Container", "namespace": "shop", "limit": 2, "sort": [{"column": "cpu_usage", "desc": True}]})
    assert result["table"]["returned"] == 2 and result["table"]["matched"] == 6


async def test_selected_columns_keep_the_identity_columns(shop):
    result = await _run("memory limits in shop", {"kind": "Container", "namespace": "shop", "columns": ["memory_limit"]})
    assert result["table"]["columns"] == ["namespace", "pod", "container", "memory_limit"]


@pytest.mark.parametrize("kind", catalog.queryable())
async def test_no_queryable_kind_leaks_the_canary(cluster, kind):
    result = await _run(f"list every {kind}", {"kind": kind})
    assert fx.CANARY not in json.dumps(result)


@pytest.mark.parametrize("kind", catalog.queryable())
async def test_every_value_has_its_declared_column_type(shop, kind):
    shop.objects["Job"] = [{"metadata": {"name": "j", "namespace": "shop", "creationTimestamp": fx.iso(5)}, "spec": {"completions": 1}, "status": {"active": 1, "startTime": fx.iso(5)}}]
    shop.objects["CronJob"] = [{"metadata": {"name": "c", "namespace": "shop"}, "spec": {"schedule": "*/5 * * * *", "suspend": False}, "status": {"active": [{"name": "c-1"}]}}]
    t = (await _run(f"list every {kind}", {"kind": kind}))["table"]
    assert t["schema"] == "k8s.query_result/v1" and len(t["column_types"]) == len(t["columns"])
    assert all(ty in table.TYPES for ty in t["column_types"])
    for row in t["rows"]:
        for column, ty, value in zip(t["columns"], t["column_types"], row):
            assert table.conforms(ty, value), (kind, column, ty, value)


# ---------------------------------------------------------------- planning


async def test_a_listing_plan_keeps_query_resources_alone():
    plan = Plan(steps=[Step(capability="query_resources", params={"namespace": "insightfinder"})])
    steps = k8s_meta._keep_plans_safe(plan, {})
    assert [s.capability for s in steps] == ["query_resources"]
