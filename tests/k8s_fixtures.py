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
            "uid": f"uid-deploy-{namespace}-{name}",
            "generation": generation,
            "labels": labels or {},
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": json.dumps({"env": CANARY}),
                "deployment.kubernetes.io/revision": "3",
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
    resize_policy=None,
    qos_class="Burstable",
):
    requests = requests if requests is not None else {"cpu": "500m", "memory": "1Gi"}
    container = {
        "name": "app",
        "env": [{"name": "API_KEY", "value": CANARY}],
        "resources": {"requests": requests, "limits": limits or {}},
    }
    if resize_policy:
        container["resizePolicy"] = [{"resourceName": k, "restartPolicy": v} for k, v in resize_policy.items()]
    statuses = containers or [{"name": "app", "ready": True, "restartCount": 0, "state": {"running": {"startedAt": iso(30)}}}]
    for st in statuses:
        if st.get("name") == "app" and "resources" not in st:
            st["resources"] = copy.deepcopy(container["resources"])
    return {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "namespace": namespace,
            "labels": {"app": app, "pod-template-hash": TEMPLATE_HASH},
            "ownerReferences": [{"kind": owner_kind, "name": f"{app}-{TEMPLATE_HASH}"}],
            "creationTimestamp": created or iso(30),
        },
        "spec": {"nodeName": node, "containers": [container]},
        "status": {
            "phase": phase,
            "qosClass": qos_class,
            "conditions": conditions or [],
            "containerStatuses": statuses,
        },
    }


def node(name, cpu="8", memory="32Gi", labels=None, taints=None, ready=True, instance_type="m5.2xlarge", kubelet="v1.36.3-eks-cb19647"):
    return {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "labels": {"node.kubernetes.io/instance-type": instance_type, **(labels or {})},
        },
        "spec": {"taints": taints or []},
        "status": {
            "allocatable": {"cpu": cpu, "memory": memory},
            "capacity": {"cpu": cpu, "memory": memory},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "nodeInfo": {"kubeletVersion": kubelet},
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


# The free-text status older Cluster Autoscaler versions write (shape copied from stg/prod/eu, CA 1.25-1.27).
CA_STATUS_TEXT = """Cluster-autoscaler status at 2026-11-26 07:18:21.287255465 +0000 UTC:
Cluster-wide:
  Health:      Healthy (ready=9 unready=0 (resourceUnready=0) notStarted=0 longNotStarted=0 registered=9 longUnregistered=0)
               LastProbeTime:      2026-11-26 07:18:20.971900674 +0000 UTC m=+442905.996350407
  ScaleUp:     InProgress (ready=9 registered=9)
               LastProbeTime:      2026-11-26 07:18:20.971900674 +0000 UTC m=+442905.996350407
  ScaleDown:   NoCandidates (candidates=0)

NodeGroups:
  Name:        eks-ng-general-4ecbd857
  Health:      Healthy (ready=6 unready=0 (resourceUnready=0) notStarted=0 longNotStarted=0 registered=6 longUnregistered=0 cloudProviderTarget=6 (minSize=0, maxSize=50))
               LastProbeTime:      2026-11-26 07:18:20.971900674 +0000 UTC m=+442905.996350407
  ScaleUp:     NoActivity (ready=6 cloudProviderTarget=6)
  ScaleDown:   NoCandidates (candidates=0)

  Name:        eks-ng-spot-00cbe66c
  Health:      Healthy (ready=3 unready=0 (resourceUnready=0) notStarted=0 longNotStarted=0 registered=3 longUnregistered=0 cloudProviderTarget=4 (minSize=1, maxSize=4))
  ScaleUp:     Backoff (ready=3 cloudProviderTarget=4)
  ScaleDown:   NoCandidates (candidates=0)
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


def _leaf_paths(body, path=()):
    """`fieldsV1` paths of the values a patch body sets (list items keyed by name, as containers are)."""
    if isinstance(body, dict):
        for key, value in body.items():
            yield from _leaf_paths(value, (*path, f"f:{key}"))
    elif isinstance(body, list) and body and all(isinstance(item, dict) and "name" in item for item in body):
        for item in body:
            key = "k:" + json.dumps({"name": item["name"]}, separators=(",", ":"))
            yield from _leaf_paths({k: v for k, v in item.items() if k != "name"}, (*path, key))
    else:
        yield path


def _value_at(obj, path):
    for key in path:
        if key.startswith("k:"):
            name = json.loads(key[2:])["name"]
            obj = next((item for item in obj or [] if isinstance(item, dict) and item.get("name") == name), None)
        else:
            obj = (obj or {}).get(key[2:]) if isinstance(obj, dict) else None
        if obj is None:
            return None
    return obj


def _take_ownership(before, after, body, manager, subresource):
    entries = (after.setdefault("metadata", {})).setdefault("managedFields", [])
    for path in _leaf_paths({k: v for k, v in (body or {}).items() if k != "metadata"}):
        if _value_at(before, path) == _value_at(after, path):
            continue
        for entry in entries:
            _drop(entry.get("fieldsV1") or {}, path)
        entry = next((e for e in entries if e.get("manager") == manager and e.get("subresource") == subresource), None)
        if entry is None:
            entry = {"manager": manager, "operation": "Update", "fieldsV1": {}}
            if subresource:
                entry["subresource"] = subresource
            entries.append(entry)
        node = entry["fieldsV1"]
        for key in path:
            node = node.setdefault(key, {})


def _drop(fields, path):
    if not path or path[0] not in fields:
        return
    if len(path) == 1:
        del fields[path[0]]
        return
    _drop(fields[path[0]], path[1:])
    if not fields[path[0]]:
        del fields[path[0]]


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
        if key == "spec.nodeName" and (obj.get("spec") or {}).get("nodeName") != value:
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
        # When set, a real (non-dry-run) pod resize is applied at once by the "kubelet": the container status
        # takes on the new resources, and the container restarts once under RestartContainer.
        self.auto_resize = False
        self.deletes: list[dict] = []
        # When set, a real pod delete is followed at once by the controller's ready replacement.
        self.auto_replace = False
        # When set, a real PVC size patch is followed at once by the volume reporting the new capacity.
        self.auto_expand = False
        # What discovery and /version answer.
        self.resize_supported = True
        self.server_git_version = "v1.36.5-eks-91f89a9"
        self.discovery_error: Exception | None = None
        # Kinds whose list answers 403 (not granted) or 404 (not served by this cluster).
        self.forbidden: set[str] = set()
        self.not_served: set[str] = set()

    def _roll_out(self, dep: dict) -> None:
        spec = dep["spec"]
        replicas = spec.get("replicas", 1)
        if spec.get("paused"):
            # A paused Deployment rolls nothing; the controller only reports that it is paused.
            dep["status"]["observedGeneration"] = dep["metadata"]["generation"]
            dep["status"]["conditions"] = [{"type": "Progressing", "status": "Unknown", "reason": "DeploymentPaused"}]
            return
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
        if kind in self.forbidden:
            raise api_error(403, f"{kind} is forbidden", "Forbidden")
        if kind in self.not_served:
            raise _not_found(kind, "")
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

    async def patch(self, kind, name, namespace, body, content_type, dry_run, subresource=None, field_manager="ari-k8s-agent"):
        """As the API server: `field_manager` (the agent's own unless a test writes as someone else) takes over
        each field this patch changes in `metadata.managedFields`; a field it leaves as it was keeps its owner."""
        target_kind = kind
        # Recorded as before the generic client: a scale-subresource patch is kind "Scale".
        kind = "Scale" if subresource == "scale" else kind
        obj = self._find(target_kind, name, namespace)
        if obj is None:
            raise _not_found(kind, name)
        self.patches.append({"kind": kind, "name": name, "namespace": namespace, "body": body, "dry_run": dry_run, "content_type": content_type})
        uid = ((body or {}).get("metadata") or {}).get("uid")
        if uid and uid != (obj.get("metadata") or {}).get("uid"):
            # As the API server answers a patch whose metadata.uid is not the object's (seen on test).
            raise api_error(422, f'{kind} "{name}" is invalid: metadata.uid: Invalid value: "{uid}": field is immutable', "Invalid")
        merged = _deep_merge(obj, body, strategic="strategic" in content_type)
        if not dry_run:
            merged["metadata"]["generation"] = (obj["metadata"].get("generation") or 0) + 1
            _take_ownership(obj, merged, body, field_manager, subresource)
            obj.clear()
            obj.update(merged)
            if self.auto_rollout and target_kind == "Deployment":
                self._roll_out(obj)
            if self.auto_rollout and target_kind in ("StatefulSet", "DaemonSet"):
                self._roll_out_workload(obj)
            if self.auto_resize and target_kind == "Pod" and subresource == "resize":
                self._resize(obj, body)
            if self.auto_expand and target_kind == "PersistentVolumeClaim":
                obj.setdefault("status", {})["capacity"] = {"storage": obj["spec"]["resources"]["requests"]["storage"]}
        if kind == "Scale":
            return {"metadata": {"name": name}, "spec": {"replicas": merged["spec"]["replicas"]}}
        return copy.deepcopy(merged)

    def _roll_out_workload(self, obj: dict) -> None:
        spec, kind = obj["spec"], obj["kind"]
        generation = obj["metadata"]["generation"]
        if kind == "StatefulSet":
            replicas = spec.get("replicas", 1)
            obj["status"] = {
                "observedGeneration": generation, "replicas": replicas, "readyReplicas": replicas, "availableReplicas": replicas,
                "updatedReplicas": replicas, "currentRevision": f"rev-{generation}", "updateRevision": f"rev-{generation}",
            }
        else:
            desired = (obj.get("status") or {}).get("desiredNumberScheduled", 0)
            obj["status"] = {
                "observedGeneration": generation, "desiredNumberScheduled": desired, "currentNumberScheduled": desired,
                "numberReady": desired, "updatedNumberScheduled": desired, "numberAvailable": desired,
            }

    async def delete(self, kind, name, namespace, dry_run, grace_period_seconds=None, preconditions=None):
        obj = self._find(kind, name, namespace)
        if obj is None:
            raise _not_found(kind, name)
        if preconditions and preconditions.get("uid") != obj["metadata"].get("uid"):
            raise api_error(409, f"Precondition failed: UID in precondition: {preconditions.get('uid')}, UID in object meta: {obj['metadata'].get('uid')}", "Conflict")
        self.deletes.append({"kind": kind, "name": name, "namespace": namespace, "dry_run": dry_run, "preconditions": preconditions})
        result = copy.deepcopy(obj)
        result["metadata"]["deletionTimestamp"] = iso(0)
        if not dry_run:
            self.objects[kind].remove(obj)
            if self.auto_replace and kind == "Pod":
                self._replace_pod(obj)
        return result

    def _replace_pod(self, old: dict) -> None:
        """The controller creates a ready replacement at once: same name for a StatefulSet's pod, a new name
        otherwise; always a new uid."""
        new = copy.deepcopy(old)
        meta = new["metadata"]
        owner_kind = (meta.get("ownerReferences") or [{}])[0].get("kind")
        if owner_kind != "StatefulSet":
            meta["name"] = f"{meta['name']}-r"
        meta["uid"] = f"uid-{meta['name']}-new"
        # One second later than "now" so it counts as created after the delete (second precision).
        meta["creationTimestamp"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for st in new["status"].get("containerStatuses") or []:
            st.update({"ready": True, "restartCount": 0, "state": {"running": {}}})
            st.pop("lastState", None)
        self.objects["Pod"].append(new)

    def _resize(self, pod: dict, body: dict) -> None:
        for change in (body.get("spec") or {}).get("containers") or []:
            spec_c = next(c for c in pod["spec"]["containers"] if c["name"] == change["name"])
            policy = {p["resourceName"]: p["restartPolicy"] for p in spec_c.get("resizePolicy") or []}
            changed = {r for side in (change.get("resources") or {}).values() for r in side}
            for st in pod["status"].get("containerStatuses") or []:
                if st["name"] != change["name"]:
                    continue
                st["resources"] = copy.deepcopy(spec_c["resources"])
                if any(policy.get(r) == "RestartContainer" for r in changed):
                    st["restartCount"] = (st.get("restartCount") or 0) + 1
                st["ready"] = True
                st["state"] = {"running": {}}

    async def server_version(self):
        return {"major": "1", "minor": "36", "gitVersion": self.server_git_version}

    async def core_resources(self):
        if self.discovery_error:
            raise self.discovery_error
        return ["pods", "pods/log", "pods/eviction", "nodes"] + (["pods/resize"] if self.resize_supported else [])

    async def pod_log(self, namespace, pod, container=None, previous=True, tail_lines=40, limit_bytes=8000, since_seconds=None):
        self.log_calls.append({"pod": pod, "container": container, "previous": previous})
        entry = self.logs.get(pod, "")
        if isinstance(entry, dict):  # {"previous": ..., "current": ...}
            return entry.get("previous" if previous else "current", "")
        return entry


def replicaset(deployment="checkout", revision=1, image="registry.example/checkout:1.1", template_hash="5c8d7f6b4", namespace="shop", replicas=0):
    name = f"{deployment}-{template_hash}"
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-{name}",
            "labels": {"app": deployment, "pod-template-hash": template_hash},
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
            "ownerReferences": [{"kind": "Deployment", "name": deployment}],
        },
        "spec": {
            "replicas": replicas,
            "template": {
                "metadata": {"labels": {"app": deployment, "pod-template-hash": template_hash}},
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": image,
                            "env": [{"name": "DB_PASSWORD", "value": CANARY}],
                            "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}},
                        }
                    ]
                },
            },
        },
        "status": {"replicas": replicas, "readyReplicas": replicas},
    }


def statefulset(name="db", namespace="shop", replicas=2, strategy="RollingUpdate", generation=3, labels=None, claim_templates=None):
    sts = _statefulset(name, namespace, replicas, strategy, generation, labels)
    if claim_templates:
        sts["spec"]["volumeClaimTemplates"] = [
            {"metadata": {"name": t}, "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}, "storageClassName": "gp3"}}
            for t, size in claim_templates.items()
        ]
    return sts


def _statefulset(name, namespace, replicas, strategy, generation, labels):
    return {
        "kind": "StatefulSet",
        "metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}", "generation": generation, "labels": labels or {}},
        "spec": {
            "replicas": replicas,
            "serviceName": name,
            "selector": {"matchLabels": {"app": name}},
            "updateStrategy": {"type": strategy},
            "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [{"name": "db", "image": "registry.example/db:16", "env": [{"name": "PW", "value": CANARY}]}]}},
        },
        "status": {
            "observedGeneration": generation, "replicas": replicas, "readyReplicas": replicas, "availableReplicas": replicas,
            "updatedReplicas": replicas, "currentRevision": "rev-1", "updateRevision": "rev-1",
        },
    }


def daemonset(name="agent", namespace="shop", desired=2, strategy="RollingUpdate", generation=4):
    return {
        "kind": "DaemonSet",
        "metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}", "generation": generation, "labels": {}},
        "spec": {
            "selector": {"matchLabels": {"app": name}},
            "updateStrategy": {"type": strategy, "rollingUpdate": {"maxUnavailable": 1}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [{"name": "agent", "image": "registry.example/agent:3"}]}},
        },
        "status": {
            "observedGeneration": generation, "desiredNumberScheduled": desired, "currentNumberScheduled": desired,
            "numberReady": desired, "updatedNumberScheduled": desired, "numberAvailable": desired,
        },
    }


def owned_pod(name, owner_kind, owner, node="node-a", namespace="shop"):
    """A pod a StatefulSet or DaemonSet owns directly."""
    p = pod(name, namespace=namespace, app=owner, node=node, owner_kind=owner_kind)
    p["metadata"]["ownerReferences"] = [{"kind": owner_kind, "name": owner}]
    p["metadata"]["labels"] = {"app": owner}
    return p


def pod_metrics(name, namespace="shop", cpu="300m", memory="600Mi", container="app"):
    """A metrics.k8s.io PodMetrics object: one container's current usage."""
    return {
        "metadata": {"name": name, "namespace": namespace},
        "containers": [{"name": container, "usage": {"cpu": cpu, "memory": memory}}],
    }


def api_error(status: int, message: str, reason: str = "Error"):
    from kubernetes.client.exceptions import ApiException

    exc = ApiException(status=status, reason=reason)
    exc.body = json.dumps({"kind": "Status", "message": message, "reason": reason, "code": status})
    return exc


def _not_found(kind, name):
    return api_error(404, f"{kind} {name} not found", "NotFound")


def cronjob(name="report", namespace="shop", suspend=False, active=None, concurrency="Allow", starting_deadline=None, labels=None):
    spec = {
        "schedule": "0 2 * * *",
        "suspend": suspend,
        "concurrencyPolicy": concurrency,
        "jobTemplate": {
            "metadata": {"labels": {"team": "data"}},
            "spec": {
                "backoffLimit": 1,
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [{"name": "run", "image": "registry.example/report:5", "env": [{"name": "TOKEN", "value": CANARY}]}],
                    },
                },
            },
        },
    }
    if starting_deadline is not None:
        spec["startingDeadlineSeconds"] = starting_deadline
    return {
        "kind": "CronJob",
        "metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}", "generation": 2, "labels": labels or {}},
        "spec": spec,
        "status": {"active": [{"kind": "Job", "name": j} for j in active or []], "lastScheduleTime": iso(600)},
    }


def job_pod(name, job, namespace="shop"):
    """A pod of a Job (a CronJob's run)."""
    p = pod(name, namespace=namespace, app="report", owner_kind="Job")
    p["metadata"]["ownerReferences"] = [{"kind": "Job", "name": job}]
    p["metadata"]["labels"] = {"job-name": job}
    return p


def pvc(name, namespace="shop", size="10Gi", requested=None, storage_class="gp3", phase="Bound", labels=None, annotations=None,
        owners=None, managers=None, conditions=None, resize_status=None, uid=None):
    meta = {"name": name, "namespace": namespace, "uid": uid or f"uid-pvc-{name}", "labels": labels or {}, "annotations": annotations or {}}
    if owners:
        meta["ownerReferences"] = [{"kind": k, "name": n} for k, n in owners]
    if managers:
        meta["managedFields"] = [{"manager": m, "operation": "Apply"} for m in managers]
    status = {"phase": phase, "capacity": {"storage": size}}
    if conditions:
        status["conditions"] = [{"type": c, "status": "True"} for c in conditions]
    if resize_status:
        status["allocatedResourceStatuses"] = {"storage": resize_status}
    return {
        "kind": "PersistentVolumeClaim",
        "metadata": meta,
        "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": storage_class, "resources": {"requests": {"storage": requested or size}}},
        "status": status,
    }


def storageclass(name="gp3", expansion=True, provisioner="ebs.csi.aws.com"):
    return {"kind": "StorageClass", "metadata": {"name": name, "annotations": {}}, "provisioner": provisioner, "allowVolumeExpansion": expansion}
