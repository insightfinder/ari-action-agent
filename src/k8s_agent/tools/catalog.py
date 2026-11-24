"""Every kind of object this agent can read or change, in one table.

The client (`client.py`) resolves a kind name here to its REST path; the reads (`read.py`) reduce what comes
back to the kind's whitelisted view (`views.py`); the table engine (`table.py`) turns a view into the flat
columns a query can select, filter and sort on. Adding a kind is one entry here, one view, and one RBAC rule
in docs/examples/k8s-rbac.yaml (and the Helm chart that mirrors it) — no capability changes.

Secrets are deliberately absent, and `refused_kind` names them: Kubernetes returns a Secret's data with every
get and list, so there is no way to read even their names without the values (II-25173 decision, kept).

Custom resources not listed here are not readable yet. `custom()` below builds a Kind for one found through
its CustomResourceDefinition, read with the generic, scalar-only `views.custom_view`; no capability uses it
so far (II-25740: a later step).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote

from . import views


# A Kubernetes object name or namespace (DNS-1123 subdomain), an API group, a version, a plural. Names reach
# `Kind.path` from the model's extraction and the planner's params, so a value like "../../secrets" or
# "x?dryRun=" must never become part of a request path.
_SEGMENT = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")


def segment(value: str, what: str = "name") -> str:
    text = str(value or "")
    if not _SEGMENT.fullmatch(text) or ".." in text:
        raise ValueError(f"invalid Kubernetes {what} {value!r}")
    return quote(text, safe="")


@dataclass(frozen=True)
class Kind:
    name: str
    group: str
    version: str
    plural: str
    namespaced: bool
    view: Callable[[dict], dict]
    # Flat columns for tables (query_resources); None for kinds that are read but not queried directly.
    row: Callable[[dict], dict] | None = None
    aliases: tuple[str, ...] = ()
    # Read by the code that needs them, never offered to the model as something to query.
    internal: bool = False
    # Subresources the client may address: name -> path suffix.
    subresources: dict = field(default_factory=dict)

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    def path(self, namespace: str | None = None, name: str | None = None, subresource: str | None = None) -> str:
        base = f"/apis/{segment(self.group, 'API group')}/{segment(self.version, 'API version')}" if self.group else f"/api/{segment(self.version, 'API version')}"
        if self.namespaced and namespace:
            base += f"/namespaces/{segment(namespace, 'namespace')}"
        base += f"/{segment(self.plural, 'resource')}"
        if name:
            base += f"/{segment(name)}"
        if subresource:
            if subresource not in self.subresources:
                raise ValueError(f"subresource {subresource!r} is not allowed for {self.name}")
            base += f"/{self.subresources[subresource]}"
        return base


# ---------------------------------------------------------------- rows


def age(created_at: str | None, now: datetime | None = None) -> str | None:
    """`kubectl`-style age: 45s, 12m, 5h, 3d, 2y."""
    if not created_at:
        return None
    try:
        then = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = int(((now or datetime.now(timezone.utc)) - then).total_seconds())
    for unit, size in (("y", 365 * 86400), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{max(seconds, 0)}s"


def _images(template: dict | None) -> str:
    return ", ".join(sorted({c.get("image") or "" for c in (template or {}).get("containers") or []}))


def _pod_row(v: dict) -> dict:
    containers = v.get("containers") or []
    ready = sum(1 for c in containers if c.get("ready"))
    waiting = next(
        ((c.get("state") or {}).get("reason") for c in containers if (c.get("state") or {}).get("state") == "waiting"),
        None,
    )
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "status": waiting or ("Terminating" if v.get("deleting") else v.get("reason") or v.get("phase")),
        "phase": v.get("phase"),
        "ready": f"{ready}/{len(v.get('images') or containers)}",
        "restarts": sum(c.get("restart_count") or 0 for c in containers),
        "node": v.get("node_name"),
        "owner_kind": v.get("owner_kind"),
        "owner": v.get("owner_name"),
        "images": ", ".join(sorted({i or "" for i in (v.get("images") or {}).values()})),
        "created_at": v.get("created_at"),
    }


def _deployment_row(v: dict) -> dict:
    s = v.get("status") or {}
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "replicas": v.get("replicas"),
        "ready": f"{s.get('ready', 0)}/{v.get('replicas')}",
        "available": s.get("available", 0),
        "updated": s.get("updated", 0),
        "strategy": (v.get("strategy") or {}).get("type"),
        "images": _images(v.get("template")),
        "managed_by": (v.get("labels") or {}).get("app.kubernetes.io/managed-by"),
        "created_at": v.get("created_at"),
    }


def _statefulset_row(v: dict) -> dict:
    s = v.get("status") or {}
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "replicas": v.get("replicas"),
        "ready": f"{s.get('ready', 0)}/{v.get('replicas')}",
        "updated": s.get("updated", 0),
        "current_revision": s.get("current_revision"),
        "update_revision": s.get("update_revision"),
        "service": v.get("service_name"),
        "images": _images(v.get("template")),
        "created_at": v.get("created_at"),
    }


def _daemonset_row(v: dict) -> dict:
    s = v.get("status") or {}
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "desired": s.get("desired"),
        "current": s.get("current"),
        "ready": s.get("ready"),
        "updated": s.get("updated"),
        "available": s.get("available"),
        "misscheduled": s.get("misscheduled"),
        "images": _images(v.get("template")),
        "created_at": v.get("created_at"),
    }


def _replicaset_row(v: dict) -> dict:
    s = v.get("status") or {}
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "owner": (v.get("owner") or {}).get("name"),
        "revision": v.get("revision"),
        "desired": v.get("replicas"),
        "ready": s.get("ready", 0),
        "images": _images(v.get("template")),
        "created_at": v.get("created_at"),
    }


def _job_row(v: dict) -> dict:
    s = v.get("status") or {}
    done = next((c.get("type") for c in v.get("conditions") or [] if c.get("status") == "True" and c.get("type") in ("Complete", "Failed")), None)
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "owner": (v.get("owner") or {}).get("name"),
        "status": done or ("Suspended" if v.get("suspend") else "Running" if s.get("active") else "Pending"),
        "completions": f"{s.get('succeeded', 0)}/{v.get('completions') or 1}",
        "active": s.get("active", 0),
        "failed": s.get("failed", 0),
        "start_time": s.get("start_time"),
        "completion_time": s.get("completion_time"),
        "created_at": v.get("created_at"),
    }


def _cronjob_row(v: dict) -> dict:
    s = v.get("status") or {}
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "schedule": v.get("schedule"),
        "time_zone": v.get("time_zone"),
        "suspend": v.get("suspend"),
        "active": s.get("active", 0),
        "last_schedule_time": s.get("last_schedule_time"),
        "last_successful_time": s.get("last_successful_time"),
        "images": _images(v.get("template")),
        "created_at": v.get("created_at"),
    }


def _service_row(v: dict) -> dict:
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "type": v.get("type"),
        "cluster_ip": v.get("cluster_ip"),
        "external": ", ".join(v.get("external") or []) or None,
        "ports": ", ".join(f"{p.get('port')}/{p.get('protocol')}" + (f":{p['node_port']}" if p.get("node_port") else "") for p in v.get("ports") or []),
        "selector": ",".join(f"{k}={val}" for k, val in (v.get("selector") or {}).items()) or None,
        "created_at": v.get("created_at"),
    }


def _ingress_row(v: dict) -> dict:
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "class": v.get("class"),
        "hosts": ", ".join(sorted({r.get("host") or "*" for r in v.get("rules") or []})),
        "backends": ", ".join(sorted({f"{r.get('service')}:{r.get('port')}" for r in v.get("rules") or []})),
        "tls": bool(v.get("tls_hosts")),
        "address": ", ".join(v.get("address") or []) or None,
        "created_at": v.get("created_at"),
    }


def _pvc_row(v: dict) -> dict:
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "status": v.get("phase"),
        "volume": v.get("volume"),
        "capacity": v.get("capacity") or v.get("requested"),
        "access_modes": ",".join(v.get("access_modes") or []),
        "storage_class": v.get("storage_class"),
        "created_at": v.get("created_at"),
    }


def _pv_row(v: dict) -> dict:
    return {
        "name": v.get("name"),
        "capacity": v.get("capacity"),
        "access_modes": ",".join(v.get("access_modes") or []),
        "reclaim_policy": v.get("reclaim_policy"),
        "status": v.get("phase"),
        "claim": v.get("claim"),
        "storage_class": v.get("storage_class"),
        "created_at": v.get("created_at"),
    }


def _node_row(v: dict) -> dict:
    ready = next((c.get("status") for c in v.get("conditions") or [] if c.get("type") == "Ready"), None)
    return {
        "name": v.get("name"),
        "status": ("Ready" if v.get("ready") else "NotReady") + (",SchedulingDisabled" if v.get("unschedulable") else ""),
        "ready": ready == "True" if ready is not None else v.get("ready"),
        "instance_type": v.get("instance_type"),
        "node_group": v.get("node_group"),
        "zone": v.get("zone"),
        "kubelet_version": v.get("kubelet_version"),
        "cpu_allocatable": (v.get("allocatable") or {}).get("cpu"),
        "memory_allocatable": (v.get("allocatable") or {}).get("memory"),
        "taints": ", ".join(f"{t.get('key')}={t.get('value') or ''}:{t.get('effect')}" for t in v.get("taints") or []) or None,
        "created_at": v.get("created_at"),
    }


def _event_row(v: dict) -> dict:
    inv = v.get("involved") or {}
    return {
        "namespace": inv.get("namespace"),
        "type": v.get("type"),
        "reason": v.get("reason"),
        "object": f"{inv.get('kind')}/{inv.get('name')}",
        "count": v.get("count"),
        "last_seen": v.get("last_seen"),
        "message": v.get("message"),
    }


def _hpa_row(v: dict) -> dict:
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "target": f"{(v.get('target') or {}).get('kind')}/{(v.get('target') or {}).get('name')}",
        "min": v.get("min"),
        "max": v.get("max"),
        "current_replicas": v.get("current_replicas"),
        "desired_replicas": v.get("desired_replicas"),
        "owned_by_scaledobject": v.get("owned_by_scaledobject"),
    }


def _scaledobject_row(v: dict) -> dict:
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "target": f"{(v.get('target') or {}).get('kind')}/{(v.get('target') or {}).get('name')}",
        "min": v.get("min"),
        "max": v.get("max"),
        "paused": (v.get("annotations") or {}).get("autoscaling.keda.sh/paused"),
    }


def _pdb_row(v: dict) -> dict:
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "selector": ",".join(f"{k}={val}" for k, val in (v.get("selector") or {}).items()),
        "min_available": v.get("min_available"),
        "max_unavailable": v.get("max_unavailable"),
        "disruptions_allowed": v.get("disruptions_allowed"),
    }


def _simple_row(*keys: str) -> Callable[[dict], dict]:
    def row(v: dict) -> dict:
        out = {k: v.get(k) for k in ("namespace", "name") if k in v}
        for k in keys:
            value = v.get(k)
            out[k] = ", ".join(map(str, value)) if isinstance(value, list) else value
        return out

    return row


def _quota_row(v: dict) -> dict:
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "hard": ", ".join(f"{k}={val}" for k, val in sorted((v.get("hard") or {}).items())),
        "used": ", ".join(f"{k}={val}" for k, val in sorted((v.get("used") or {}).items())),
    }


def _custom_row(v: dict) -> dict:
    ready = next((c for c in v.get("conditions") or [] if c.get("type") in ("Ready", "Available", "Healthy")), None)
    return {
        "namespace": v.get("namespace"),
        "name": v.get("name"),
        "kind": v.get("kind"),
        "ready": (ready or {}).get("status"),
        "reason": (ready or {}).get("reason"),
        "message": (ready or {}).get("message"),
        "created_at": v.get("created_at"),
    }


def _crd_row(v: dict) -> dict:
    return {
        "name": v.get("name"),
        "kind": v.get("resource_kind"),
        "group": v.get("group"),
        "version": v.get("version"),
        "scope": v.get("scope"),
        "short_names": ", ".join(v.get("short_names") or []),
    }


# ---------------------------------------------------------------- the table


def _k(name, group, version, plural, namespaced, view, row=None, aliases=(), internal=False, subresources=None):
    return Kind(name, group, version, plural, namespaced, view, row, tuple(aliases), internal, subresources or {})


_ALL = [
    _k("Pod", "", "v1", "pods", True, views.pod_view, _pod_row, ("po", "pods"), subresources={"eviction": "eviction", "log": "log", "resize": "resize"}),
    _k("Deployment", "apps", "v1", "deployments", True, views.deployment_view, _deployment_row, ("deploy", "deployments"), subresources={"scale": "scale"}),
    _k("StatefulSet", "apps", "v1", "statefulsets", True, views.statefulset_view, _statefulset_row, ("sts", "statefulsets"), subresources={"scale": "scale"}),
    _k("DaemonSet", "apps", "v1", "daemonsets", True, views.daemonset_view, _daemonset_row, ("ds", "daemonsets")),
    _k("ReplicaSet", "apps", "v1", "replicasets", True, views.replicaset_view, _replicaset_row, ("rs", "replicasets")),
    _k("ControllerRevision", "apps", "v1", "controllerrevisions", True, views.controllerrevision_view, _simple_row("revision", "created_at"), ("controllerrevisions",)),
    _k("Job", "batch", "v1", "jobs", True, views.job_view, _job_row, ("jobs",)),
    _k("CronJob", "batch", "v1", "cronjobs", True, views.cronjob_view, _cronjob_row, ("cj", "cronjobs")),
    _k("Service", "", "v1", "services", True, views.service_view, _service_row, ("svc", "services")),
    _k("EndpointSlice", "discovery.k8s.io", "v1", "endpointslices", True, views.endpointslice_view, _simple_row("service", "ready", "not_ready"), ("endpoints", "endpointslices")),
    _k("Ingress", "networking.k8s.io", "v1", "ingresses", True, views.ingress_view, _ingress_row, ("ing", "ingresses")),
    _k("NetworkPolicy", "networking.k8s.io", "v1", "networkpolicies", True, views.networkpolicy_view, _simple_row("pod_selector", "policy_types"), ("netpol", "networkpolicies")),
    _k("ConfigMap", "", "v1", "configmaps", True, views.configmap_view, _simple_row("keys", "immutable", "created_at"), ("cm", "configmaps")),
    _k("PersistentVolumeClaim", "", "v1", "persistentvolumeclaims", True, views.pvc_full_view, _pvc_row, ("pvc", "pvcs", "persistentvolumeclaims")),
    _k("PersistentVolume", "", "v1", "persistentvolumes", False, views.pv_view, _pv_row, ("pv", "pvs", "persistentvolumes")),
    _k("StorageClass", "storage.k8s.io", "v1", "storageclasses", False, views.storageclass_view, _simple_row("provisioner", "reclaim_policy", "volume_binding_mode", "allow_volume_expansion", "default"), ("sc", "storageclasses")),
    _k("Namespace", "", "v1", "namespaces", False, views.namespace_view, _simple_row("phase", "created_at"), ("ns", "namespaces")),
    _k("Node", "", "v1", "nodes", False, views.node_full_view, _node_row, ("no", "nodes")),
    _k("Event", "", "v1", "events", True, views.event_view, _event_row, ("ev", "events")),
    _k("ResourceQuota", "", "v1", "resourcequotas", True, views.resourcequota_view, _quota_row, ("quota", "resourcequotas")),
    _k("LimitRange", "", "v1", "limitranges", True, views.limitrange_view, _simple_row("limits"), ("limits", "limitranges")),
    _k("HorizontalPodAutoscaler", "autoscaling", "v2", "horizontalpodautoscalers", True, views.hpa_view, _hpa_row, ("hpa", "hpas", "horizontalpodautoscalers")),
    _k("PodDisruptionBudget", "policy", "v1", "poddisruptionbudgets", True, views.pdb_view, _pdb_row, ("pdb", "pdbs", "poddisruptionbudgets")),
    _k("ScaledObject", "keda.sh", "v1alpha1", "scaledobjects", True, views.scaledobject_view, _scaledobject_row, ("so", "scaledobjects", "keda")),
    _k("CustomResourceDefinition", "apiextensions.k8s.io", "v1", "customresourcedefinitions", False, views.crd_view, _crd_row, ("crd", "crds", "customresourcedefinitions")),
    _k("PodMetrics", "metrics.k8s.io", "v1beta1", "pods", True, views.pod_metrics_view, internal=True),
    _k("NodeMetrics", "metrics.k8s.io", "v1beta1", "nodes", False, views.node_metrics_view, internal=True),
    # Flux: read raw by persistence.py (which keeps only what it needs) through the ownership chain, and
    # queryable through the generic scalar-only view.
    _k("HelmRelease", "helm.toolkit.fluxcd.io", "v2", "helmreleases", True, views.custom_view, _custom_row, ("hr", "helmreleases")),
    _k("HelmReleaseV2beta2", "helm.toolkit.fluxcd.io", "v2beta2", "helmreleases", True, views.custom_view, internal=True),
    _k("Kustomization", "kustomize.toolkit.fluxcd.io", "v1", "kustomizations", True, views.custom_view, _custom_row, ("ks", "kustomizations")),
    _k("GitRepository", "source.toolkit.fluxcd.io", "v1", "gitrepositories", True, views.custom_view, _custom_row, ("gitrepo", "gitrepositories")),
    _k("OCIRepository", "source.toolkit.fluxcd.io", "v1", "ocirepositories", True, views.custom_view, _custom_row, ("ocirepositories",)),
    _k("OCIRepositoryV1beta2", "source.toolkit.fluxcd.io", "v1beta2", "ocirepositories", True, views.custom_view, internal=True),
    _k("Bucket", "source.toolkit.fluxcd.io", "v1", "buckets", True, views.custom_view, _custom_row, ("buckets",)),
    _k("BucketV1beta2", "source.toolkit.fluxcd.io", "v1beta2", "buckets", True, views.custom_view, internal=True),
    _k("HelmRepository", "source.toolkit.fluxcd.io", "v1", "helmrepositories", True, views.custom_view, _custom_row, ("helmrepo", "helmrepositories")),
    _k("HelmRepositoryV1beta2", "source.toolkit.fluxcd.io", "v1beta2", "helmrepositories", True, views.custom_view, internal=True),
]

KINDS: dict[str, Kind] = {k.name: k for k in _ALL}
# Container is not an API kind: one row per container of every matching Pod, joined with metrics.k8s.io
# usage (see table.container_rows). It is what "CPU and memory limits and usage of every pod" asks for.
CONTAINER = "Container"
_REFUSED = {"secret", "secrets"}


def _index() -> dict[str, str]:
    index = {}
    for kind in _ALL:
        if kind.internal:
            continue
        for alias in (kind.name, *kind.aliases):
            index[alias.lower()] = kind.name
    index["container"] = index["containers"] = CONTAINER
    return index


_ALIASES = _index()


def resolve(name: str | None) -> str | None:
    """A kind name or alias (case-insensitive) as its catalog name, or None."""
    if not name:
        return None
    return _ALIASES.get(str(name).strip().lower())


def refused_kind(name: str | None) -> bool:
    return bool(name) and str(name).strip().lower() in _REFUSED


def get(name: str) -> Kind:
    try:
        return KINDS[name]
    except KeyError:
        raise ValueError(f"unknown kind {name!r}") from None


def queryable() -> list[str]:
    """Kinds the model may name in a query, Container first."""
    return [CONTAINER] + [k.name for k in _ALL if not k.internal and k.row is not None]


def custom(group: str, version: str, plural: str, kind_name: str, namespaced: bool) -> Kind:
    """A Kind for a custom resource found through its CustomResourceDefinition."""
    for value, what in ((group, "API group"), (version, "API version"), (plural, "resource")):
        segment(value, what)
    return Kind(kind_name, group, version, plural, namespaced, views.custom_view, _custom_row)
