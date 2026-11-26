"""scripts/k8s_agent_live_test.py against what it depends on, without a cluster: the RBAC it expects the
worker to have (and not have) must be exactly what docs/examples/k8s-rbac.yaml grants, and the fixtures its
scenarios name must exist in deploy/k8s-agent-e2e/fixtures.yaml. Both drift silently otherwise: the rbac
scenario listed `delete pods` as denied after write batch 1 granted it.
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

pytest.importorskip("httpx")

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("k8s_agent_live_test", ROOT / "scripts" / "k8s_agent_live_test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live = _script()


def _rbac_rules() -> list[tuple[str | None, dict]]:
    """(namespace or None for cluster-wide, rule) for every rule bound to the agent's ServiceAccount."""
    out = []
    for doc in yaml.safe_load_all((ROOT / "docs" / "examples" / "k8s-rbac.yaml").read_text()):
        if doc and doc.get("kind") in ("ClusterRole", "Role"):
            scope = None if doc["kind"] == "ClusterRole" else doc["metadata"]["namespace"]
            out.extend((scope, rule) for rule in doc.get("rules") or [])
    return out


def _granted(entry: tuple) -> bool:
    verb, resource, scope, *sub = entry
    resource_name = None
    if "/" in resource:
        resource, resource_name = resource.split("/", 1)
    plural, _, group = resource.partition(".")
    if sub:
        plural = f"{plural}/{sub[0]}"
    namespace = scope[len("-n="):] if scope.startswith("-n=") else None
    for rule_ns, rule in _rbac_rules():
        if rule_ns is not None and rule_ns != namespace:
            continue
        if group not in rule.get("apiGroups", []) or plural not in rule.get("resources", []) or verb not in rule.get("verbs", []):
            continue
        names = rule.get("resourceNames")
        if names and resource_name not in names:
            continue
        return True  # a rule without resourceNames covers every name
    return False


@pytest.mark.parametrize("entry", live.RBAC_ALLOWED, ids=lambda e: " ".join(filter(None, map(str, e))))
def test_what_the_rbac_scenario_expects_allowed_is_granted(entry):
    assert _granted(entry)


@pytest.mark.parametrize("entry", live.RBAC_DENIED, ids=lambda e: " ".join(filter(None, map(str, e))))
def test_what_the_rbac_scenario_expects_denied_is_not_granted(entry):
    assert not _granted(entry)


def _fixtures() -> dict:
    docs = yaml.safe_load_all((ROOT / "deploy" / "k8s-agent-e2e" / "fixtures.yaml").read_text())
    return {(d["kind"], d["metadata"]["name"]): d for d in docs if d}


@pytest.mark.parametrize(
    "kind,name",
    [
        ("Deployment", "e2e-plain"),
        ("PodDisruptionBudget", "e2e-plain-pdb"),
        ("StatefulSet", "e2e-sts"),
        ("DaemonSet", "e2e-ds"),
    ],
)
def test_the_write_scenarios_have_their_fixtures(kind, name):
    obj = _fixtures().get((kind, name))
    assert obj is not None and obj["metadata"]["namespace"] == live.NAMESPACE


def test_the_refusal_scenarios_have_their_fixtures():
    assert _fixtures()[("StatefulSet", "e2e-sts")]["metadata"]["namespace"] == live.NAMESPACE


def test_the_disk_fixture_makes_the_pvc_the_scenarios_name():
    sts = _fixtures()[("StatefulSet", live.DISK_STS)]
    [template] = sts["spec"]["volumeClaimTemplates"]
    assert sts["metadata"]["namespace"] == live.NAMESPACE and sts["spec"]["replicas"] == 1
    assert f"{template['metadata']['name']}-{live.DISK_STS}-0" == live.DISK_PVC
    assert template["spec"]["resources"]["requests"]["storage"] == "1Gi"


def test_the_late_approval_volume_uses_the_fixtures_own_storageclass():
    fixtures = _fixtures()
    sc = fixtures[("StorageClass", live.VOL_CLASS)]
    assert sc["allowVolumeExpansion"] is True and "namespace" not in sc["metadata"]
    sts = fixtures[("StatefulSet", live.VOL_STS)]
    [template] = sts["spec"]["volumeClaimTemplates"]
    assert sts["metadata"]["namespace"] == live.NAMESPACE
    assert f"{template['metadata']['name']}-{live.VOL_STS}-0" == live.VOL_PVC
    assert template["spec"]["storageClassName"] == live.VOL_CLASS


def test_the_ambiguous_statefulset_would_name_the_disk_pvc():
    """late_pvc_ambiguous creates StatefulSet AMBIGUOUS_STS with claim template `data-e2e`: its PVC 0 must be
    named exactly like the disk fixture's, and no fixture may already be called that."""
    assert f"data-e2e-{live.AMBIGUOUS_STS}-0" == live.DISK_PVC
    assert ("StatefulSet", live.AMBIGUOUS_STS) not in _fixtures()


def test_the_memory_fixture_restarts_on_a_memory_resize():
    """late_resize_pod_usage resizes e2e-memuse's memory in place; resize_pod refuses that under NotRequired."""
    [container] = _fixtures()[("Deployment", "e2e-memuse")]["spec"]["template"]["spec"]["containers"]
    assert {"resourceName": "memory", "restartPolicy": "RestartContainer"} in container["resizePolicy"]


def test_the_cronjob_fixture_never_fires_on_its_own_and_its_job_ends():
    cron = _fixtures()[("CronJob", "e2e-cron")]
    assert cron["metadata"]["namespace"] == live.NAMESPACE and cron["spec"]["schedule"] == "0 0 1 1 *"
    job = cron["spec"]["jobTemplate"]["spec"]
    assert job["template"]["spec"]["restartPolicy"] == "Never" and job["template"]["spec"]["containers"][0]["name"] == "run"


def test_every_scenario_says_what_it_checks():
    assert all((fn.__doc__ or "").strip() for fn in live.SCENARIOS.values())


_FAILED = "Completing activity as failed ({'activity_id': '3', 'attempt': 1, 'workflow_id': '%s', 'workflow_type': 'AriAgentWorkflow'})"


def test_a_refusal_is_read_from_the_worker_log_in_either_shape():
    old = "\n".join([_FAILED % "t-1", "Traceback (most recent call last):", '  File "x.py", line 1, in run',
                     "RuntimeError: apply_approved_change: the cluster changed (a). Nothing was changed."])
    new = "\n".join([_FAILED % "t-2", "Traceback (most recent call last):",
                     "common.capabilities_base.StepRefused: apply_approved_change: the cluster changed (b). Nothing was changed.",
                     "", "The above exception was the direct cause of the following exception:", "",
                     "temporalio.exceptions.ApplicationError: StepRefused: apply_approved_change: the cluster changed (b). Nothing was changed."])
    other = "\n".join([_FAILED % "t-3", "RuntimeError: apply_approved_change: someone else's task."])
    logs = "\n".join([old, new, other])
    assert live._refusals_in(logs, "t-1") == ["the cluster changed (a). Nothing was changed."]
    assert live._refusals_in(logs, "t-2") == ["the cluster changed (b). Nothing was changed."]
    assert live._refusals_in(logs, "t-9") == []
