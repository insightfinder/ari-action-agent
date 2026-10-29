"""Raw Kubernetes objects (camelCase, as the API returns them) for the k8s
kind's tests, plus a fake lightkube client that serves them."""

import copy
import json
from datetime import datetime, timedelta, timezone

from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

CANARY = "CANARY-7f3a9c1e5b2d"


def a2a_round_trip(data: dict) -> dict:
    """What an artifact becomes on the real A2A path: the executor publishes
    it with `json_format.ParseDict` into a protobuf `Value` and resumes a
    confirmation from `MessageToDict` (ints come back as floats; non-string
    keys or non-finite floats would be rejected outright)."""
    value = Value()
    json_format.ParseDict(data, value)
    return json_format.MessageToDict(value)


def iso(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def deployment(
    name="checkout",
    namespace="shop",
    replicas=3,
    generation=7,
    containers=None,
    labels=None,
    annotations=None,
    status=None,
    node_selector=None,
    volumes=None,
):
    containers = containers or [
        {
            "name": "app",
            "image": "registry.example/checkout:1.2",
            "env": [{"name": "DB_PASSWORD", "value": CANARY}],
            "args": ["--token", CANARY],
            "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}},
            "startupProbe": {"initialDelaySeconds": 60, "periodSeconds": 10, "failureThreshold": 10},
            "readinessProbe": {"initialDelaySeconds": 60, "periodSeconds": 10},
        }
    ]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "generation": generation,
            "labels": labels or {},
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": json.dumps({"env": CANARY}),
                **(annotations or {}),
            },
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "progressDeadlineSeconds": 600,
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": containers, "nodeSelector": node_selector or {}, "volumes": volumes or []},
            },
        },
        "status": status
        or {
            "observedGeneration": generation,
            "replicas": replicas,
            "readyReplicas": replicas,
            "availableReplicas": replicas,
            "updatedReplicas": replicas,
            "conditions": [{"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"}],
        },
    }


# A pod-template-hash as Kubernetes generates them (safe-encoding alphabet).
TEMPLATE_HASH = "7d9f5c6b8"


def pod(
    name,
    namespace="shop",
    app="checkout",
    node="node-a",
    phase="Running",
    containers=None,
    owner_kind="ReplicaSet",
    requests=None,
    limits=None,
    conditions=None,
    created=None,
):
    requests = requests if requests is not None else {"cpu": "500m", "memory": "1Gi"}
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": app, "pod-template-hash": TEMPLATE_HASH},
            "ownerReferences": [{"kind": owner_kind, "name": f"{app}-{TEMPLATE_HASH}"}],
            "creationTimestamp": created or iso(30),
        },
        "spec": {
            "nodeName": node,
            "containers": [
                {
                    "name": "app",
                    "env": [{"name": "API_KEY", "value": CANARY}],
                    "resources": {"requests": requests, "limits": limits or {}},
                }
            ],
        },
        "status": {
            "phase": phase,
            "conditions": conditions or [],
            "containerStatuses": containers
            or [{"name": "app", "ready": True, "restartCount": 0, "state": {"running": {"startedAt": iso(30)}}}],
        },
    }


def node(name, cpu="8", memory="32Gi", labels=None, taints=None, ready=True, instance_type="m5.2xlarge"):
    return {
        "metadata": {
            "name": name,
            "labels": {"node.kubernetes.io/instance-type": instance_type, **(labels or {})},
        },
        "spec": {"taints": taints or []},
        "status": {
            "allocatable": {"cpu": cpu, "memory": memory},
            "capacity": {"cpu": cpu, "memory": memory},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def event(reason, name, message, namespace="shop", kind="Pod", type_="Warning", minutes_ago=1, count=1):
    return {
        "type": type_,
        "reason": reason,
        "message": message,
        "count": count,
        "lastTimestamp": iso(minutes_ago),
        "involvedObject": {"kind": kind, "name": name, "namespace": namespace},
        "metadata": {"name": f"{name}.evt", "namespace": namespace},
    }


def scaledobject(name, target, namespace="shop", min_=1, max_=5, generation=2, labels=None, kind_in_ref=False):
    ref = {"name": target}
    if kind_in_ref:
        ref["kind"] = "Deployment"
    return {
        "metadata": {"name": name, "namespace": namespace, "generation": generation, "labels": labels or {}},
        "spec": {"scaleTargetRef": ref, "minReplicaCount": min_, "maxReplicaCount": max_},
    }


def hpa(name, target, namespace="shop", min_=2, max_=6, owned_by_so=False, generation=1):
    meta = {"name": name, "namespace": namespace, "generation": generation}
    if owned_by_so:
        meta["ownerReferences"] = [{"kind": "ScaledObject", "name": target}]
    return {
        "metadata": meta,
        "spec": {"scaleTargetRef": {"kind": "Deployment", "name": target}, "minReplicas": min_, "maxReplicas": max_},
        "status": {"currentReplicas": min_, "desiredReplicas": min_},
    }


CA_STATUS_YAML = """
autoscalerStatus: Running
clusterWide:
  scaleUp:
    status: NoActivity
nodeGroups:
- name: ng-general
  health:
    cloudProviderTarget: 6
    maxSize: 50
    minSize: 0
    status: Healthy
    nodeCounts:
      registered:
        total: 6
  scaleUp:
    status: NoActivity
    backoffInfo: {}
"""

CA_STATUS_BACKOFF_YAML = """
nodeGroups:
- name: ng-general
  health:
    cloudProviderTarget: 6
    maxSize: 50
    minSize: 0
    status: Healthy
  scaleUp:
    status: Backoff
    backoffInfo:
      errorInfo:
        errorCode: VcpuLimitExceeded
        errorMessage: You have requested more vCPU capacity than your current vCPU limit allows
"""


def helmrelease(name="shop-release", namespace="flux-apps", inline_values=True):
    spec = {
        "chart": {"spec": {"chart": "shop-chart", "version": "1.4.2", "sourceRef": {"kind": "HelmRepository", "name": "charts"}}},
        "valuesFrom": [{"kind": "ConfigMap", "name": "shop-values", "valuesKey": "values.yaml"}],
    }
    if inline_values:
        spec["values"] = {"db": {"password": CANARY}}
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"kustomize.toolkit.fluxcd.io/name": "apps", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"},
        },
        "spec": spec,
        "status": {"history": [{"lastDeployed": iso(10)}, {"lastDeployed": iso(70)}, {"lastDeployed": iso(60 * 30)}]},
    }


def kustomization(name="apps", namespace="flux-system", source_kind="GitRepository"):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"path": "./clusters/example/apps", "interval": "10m", "sourceRef": {"kind": source_kind, "name": "fleet"}},
        "status": {"lastAppliedRevision": "main@sha1:0123456789abcdef"},
    }


def gitrepository(name="fleet", namespace="flux-system"):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"url": f"https://deploy-bot:{CANARY}@git.example.invalid/acme/fleet.git?token={CANARY}", "ref": {"branch": "main"}},
        "status": {"artifact": {"revision": "main@sha1:0123456789abcdef"}},
    }


def helmrepository(name="charts", namespace="flux-apps"):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"url": f"https://reader:{CANARY}@charts.example.invalid/stable"},
    }


HELM_LABELS = {
    "app.kubernetes.io/managed-by": "Helm",
    "helm.toolkit.fluxcd.io/name": "shop-release",
    "helm.toolkit.fluxcd.io/namespace": "flux-apps",
}


# ---------------------------------------------------------------- fake lightkube client


def _deep_merge(base, patch, strategic=False):
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if strategic and key == "containers" and isinstance(value, list):
            containers = out.get("containers") or []
            for change in value:
                existing = next((c for c in containers if c.get("name") == change.get("name")), None)
                if existing is None:
                    containers.append(copy.deepcopy(change))
                else:
                    merged = _deep_merge(existing, change, strategic)
                    containers[containers.index(existing)] = merged
            out["containers"] = containers
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value, strategic)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _matches_selector(labels: dict, selector: str | None) -> bool:
    if not selector:
        return True
    for term in selector.split(","):
        key, _, value = term.partition("=")
        if labels.get(key) != value:
            return False
    return True


def _matches_fields(obj: dict, selector: str | None) -> bool:
    if not selector:
        return True
    for term in selector.split(","):
        key, _, value = term.partition("=")
        if key == "type" and obj.get("type") != value:
            return False
        if key == "involvedObject.name" and (obj.get("involvedObject") or {}).get("name") != value:
            return False
    return True


class FakeClient:
    """Implements `k8s_agent.tools.client.Kube` over raw objects keyed by kind;
    records patches."""

    def __init__(self, objects: dict, logs: dict | None = None):
        self.objects = {k: [copy.deepcopy(o) for o in v] for k, v in objects.items()}
        self.logs = logs or {}
        self.log_calls: list[dict] = []
        self.patches: list[dict] = []
        # When set, a real (non-dry-run) Deployment patch "rolls out" at once:
        # status catches up with spec and the Deployment's pods take on the
        # new container resources.
        self.auto_rollout = False

    def _roll_out(self, dep: dict) -> None:
        spec = dep["spec"]
        replicas = spec.get("replicas", 1)
        dep["status"] = {
            "observedGeneration": dep["metadata"]["generation"],
            "replicas": replicas,
            "readyReplicas": replicas,
            "availableReplicas": replicas,
            "updatedReplicas": replicas,
            "conditions": [{"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"}],
        }
        template = {c["name"]: c for c in spec["template"]["spec"]["containers"]}
        app = spec["selector"]["matchLabels"].get("app")
        for pod in self.objects.get("Pod", []):
            if (pod["metadata"].get("labels") or {}).get("app") != app:
                continue
            for container in pod["spec"]["containers"]:
                if container["name"] in template:
                    container["resources"] = copy.deepcopy(template[container["name"]].get("resources") or {})
            for status in pod["status"].get("containerStatuses") or []:
                status["ready"] = True
                status["state"] = {"running": {}}

    def _find(self, kind, name, namespace):
        for obj in self.objects.get(kind, []):
            meta = obj.get("metadata") or {}
            if meta.get("name") == name and (namespace is None or meta.get("namespace") in (namespace, None)):
                return obj
        return None

    async def get(self, kind, name, namespace=None):
        obj = self._find(kind, name, namespace)
        if obj is None:
            raise _not_found(kind, name)
        return copy.deepcopy(obj)

    async def list(self, kind, namespace=None, label_selector=None, field_selector=None, max_items=5000):
        out = []
        for obj in self.objects.get(kind, []):
            meta = obj.get("metadata") or {}
            if namespace and meta.get("namespace") != namespace:
                continue
            if not _matches_selector(meta.get("labels") or {}, label_selector):
                continue
            if not _matches_fields(obj, field_selector):
                continue
            out.append(copy.deepcopy(obj))
        return out[:max_items]

    async def patch(self, kind, name, namespace, body, content_type, dry_run):
        target_kind = "Deployment" if kind == "Scale" else kind
        obj = self._find(target_kind, name, namespace)
        if obj is None:
            raise _not_found(kind, name)
        self.patches.append({"kind": kind, "name": name, "namespace": namespace, "body": body, "dry_run": dry_run, "content_type": content_type})
        merged = _deep_merge(obj, body, strategic="strategic" in content_type)
        if not dry_run:
            merged["metadata"]["generation"] = (obj["metadata"].get("generation") or 0) + 1
            obj.clear()
            obj.update(merged)
            if self.auto_rollout and target_kind == "Deployment":
                self._roll_out(obj)
        if kind == "Scale":
            return {"metadata": {"name": name}, "spec": {"replicas": merged["spec"]["replicas"]}}
        return copy.deepcopy(merged)

    async def pod_log(self, namespace, pod, container=None, previous=True, tail_lines=40, limit_bytes=8000):
        self.log_calls.append({"pod": pod, "container": container, "previous": previous})
        entry = self.logs.get(pod, "")
        if isinstance(entry, dict):  # {"previous": ..., "current": ...}
            return entry.get("previous" if previous else "current", "")
        return entry


def api_error(status: int, message: str, reason: str = "Error"):
    from kubernetes.client.exceptions import ApiException

    exc = ApiException(status=status, reason=reason)
    exc.body = json.dumps({"kind": "Status", "message": message, "reason": reason, "code": status})
    return exc


def _not_found(kind, name):
    return api_error(404, f"{kind} {name} not found", "NotFound")
