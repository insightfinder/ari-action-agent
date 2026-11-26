"""Whitelisted views of raw Kubernetes objects.

This is the structural half of not leaking secrets (see `redact.py` for
the other half). Every object read from the cluster is reduced to the
fields listed here before anything else — a capability, the LLM, a step
result, an artifact — sees it. Container `env`/`envFrom`/`args`/
`command`/`volumeMounts`, pod `volumes`, and every annotation outside
`_KEPT_ANNOTATION_PREFIXES` are never copied, so they cannot be returned
however a later step uses the view.

Inputs are the camelCase dicts the API returns (the official client's `sanitize_for_serialization`),
which is also what the unit tests use as fixtures.
"""

from typing import Any

from .redact import bounded, redact_text

_OWNERSHIP_LABELS = (
    "app.kubernetes.io/managed-by",
    "app.kubernetes.io/instance",
    "app.kubernetes.io/name",
    "helm.sh/chart",
    "helm.toolkit.fluxcd.io/name",
    "helm.toolkit.fluxcd.io/namespace",
    "kustomize.toolkit.fluxcd.io/name",
    "kustomize.toolkit.fluxcd.io/namespace",
)
_KEPT_ANNOTATION_PREFIXES = (
    "meta.helm.sh/",
    "autoscaling.keda.sh/paused",
    "deployment.kubernetes.io/revision",
)
_MESSAGE_CHARS = 600


def _meta(raw: dict) -> dict:
    return raw.get("metadata") or {}


def ownership_labels(raw: dict) -> dict:
    labels = _meta(raw).get("labels") or {}
    return {k: v for k, v in labels.items() if k in _OWNERSHIP_LABELS}


def kept_annotations(raw: dict) -> dict:
    annotations = _meta(raw).get("annotations") or {}
    return {k: v for k, v in annotations.items() if k.startswith(_KEPT_ANNOTATION_PREFIXES)}


def _message(text: Any) -> str | None:
    if text is None:
        return None
    return bounded(redact_text(str(text)), _MESSAGE_CHARS)


def _resources(container: dict) -> dict:
    res = container.get("resources") or {}
    return {
        "requests": {k: str(v) for k, v in (res.get("requests") or {}).items() if k in ("cpu", "memory")},
        "limits": {k: str(v) for k, v in (res.get("limits") or {}).items() if k in ("cpu", "memory")},
    }


def _probe(probe: dict | None) -> dict | None:
    if not probe:
        return None
    return {
        "initial_delay_seconds": probe.get("initialDelaySeconds", 0),
        "period_seconds": probe.get("periodSeconds", 10),
        "failure_threshold": probe.get("failureThreshold", 3),
    }


def _container(container: dict) -> dict:
    return {
        "name": container.get("name"),
        "image": container.get("image"),
        "resources": _resources(container),
        "startup_probe": _probe(container.get("startupProbe")),
        "readiness_probe": _probe(container.get("readinessProbe")),
    }


def _conditions(status: dict) -> list[dict]:
    return [
        {
            "type": c.get("type"),
            "status": c.get("status"),
            "reason": c.get("reason"),
            "message": _message(c.get("message")),
            "last_update": c.get("lastUpdateTime") or c.get("lastTransitionTime"),
        }
        for c in status.get("conditions") or []
    ]


def _required_node_affinity(spec: dict) -> list[dict]:
    """`requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms`,
    as a list of terms (OR) of match expressions (AND)."""
    affinity = (spec.get("affinity") or {}).get("nodeAffinity") or {}
    required = affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    terms = []
    for term in required.get("nodeSelectorTerms") or []:
        terms.append(
            [
                {"key": e.get("key"), "operator": e.get("operator"), "values": list(e.get("values") or [])}
                for e in term.get("matchExpressions") or []
            ]
        )
    return terms


def _tolerations(spec: dict) -> list[dict]:
    return [
        {
            "key": t.get("key"),
            "operator": t.get("operator", "Equal"),
            "value": t.get("value"),
            "effect": t.get("effect"),
        }
        for t in spec.get("tolerations") or []
    ]


# Set by `kubectl rollout restart` (and this agent's restart_rollout) on the pod template: changing it rolls the pods.
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"


def pod_template_view(template: dict) -> dict:
    spec = template.get("spec") or {}
    meta = template.get("metadata") or {}
    return {
        "labels": dict(meta.get("labels") or {}),
        "restarted_at": (meta.get("annotations") or {}).get(RESTARTED_AT),
        "node_selector": dict(spec.get("nodeSelector") or {}),
        "required_node_affinity": _required_node_affinity(spec),
        "tolerations": _tolerations(spec),
        "containers": [_container(c) for c in spec.get("containers") or []],
        "pvc_claims": [
            (v.get("persistentVolumeClaim") or {}).get("claimName")
            for v in spec.get("volumes") or []
            if v.get("persistentVolumeClaim")
        ],
    }


def deployment_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    strategy = spec.get("strategy") or {}
    rolling = strategy.get("rollingUpdate") or {}
    return {
        "kind": "Deployment",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "uid": meta.get("uid"),
        "generation": meta.get("generation"),
        "observed_generation": status.get("observedGeneration"),
        "created_at": meta.get("creationTimestamp"),
        "replicas": spec.get("replicas", 1),
        "paused": bool(spec.get("paused")),
        "selector": dict((spec.get("selector") or {}).get("matchLabels") or {}),
        "label_selector": label_selector(spec.get("selector") or {}),
        "progress_deadline_seconds": spec.get("progressDeadlineSeconds", 600),
        "strategy": {
            "type": strategy.get("type", "RollingUpdate"),
            "max_surge": rolling.get("maxSurge", "25%"),
            "max_unavailable": rolling.get("maxUnavailable", "25%"),
        },
        "status": {
            "replicas": status.get("replicas", 0),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", 0),
            "updated": status.get("updatedReplicas", 0),
            "unavailable": status.get("unavailableReplicas", 0),
        },
        "conditions": _conditions(status),
        "labels": ownership_labels(raw),
        "annotations": kept_annotations(raw),
        "template": pod_template_view(spec.get("template") or {}),
    }


def _container_state(state: dict | None) -> dict | None:
    if not state:
        return None
    if "waiting" in state and state["waiting"] is not None:
        w = state["waiting"]
        return {"state": "waiting", "reason": w.get("reason"), "message": _message(w.get("message"))}
    if "terminated" in state and state["terminated"] is not None:
        t = state["terminated"]
        return {
            "state": "terminated",
            "reason": t.get("reason"),
            "exit_code": t.get("exitCode"),
            "finished_at": t.get("finishedAt"),
            "message": _message(t.get("message")),
        }
    if "running" in state and state["running"] is not None:
        return {"state": "running", "started_at": state["running"].get("startedAt")}
    return None


def pod_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    owners = meta.get("ownerReferences") or []
    owner = owners[0] if owners else {}
    statuses = status.get("containerStatuses") or []
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        # Identity across an in-place resize: the same pod keeps its uid; a replacement has a new one.
        "uid": meta.get("uid"),
        "qos_class": status.get("qosClass"),
        "labels": dict(meta.get("labels") or {}),
        "owner_kind": owner.get("kind"),
        "owner_name": owner.get("name"),
        "created_at": meta.get("creationTimestamp"),
        "deleting": meta.get("deletionTimestamp") is not None,
        "node_name": spec.get("nodeName"),
        "phase": status.get("phase"),
        "reason": status.get("reason"),
        "conditions": _conditions(status),
        "containers": [
            {
                "name": cs.get("name"),
                "ready": cs.get("ready", False),
                "restart_count": cs.get("restartCount", 0),
                "state": _container_state(cs.get("state")),
                "last_state": _container_state(cs.get("lastState")),
            }
            for cs in status.get("containerStatuses") or []
        ],
        "images": {c.get("name"): c.get("image") for c in spec.get("containers") or []},
        "requests": [_resources(c)["requests"] for c in spec.get("containers") or []],
        "limits": {c.get("name"): _resources(c)["limits"] for c in spec.get("containers") or []},
        "resources": {c.get("name"): _resources(c) for c in spec.get("containers") or []},
        # In-place resize (pods/resize): per container, what a cpu/memory resize does to it ("NotRequired" is
        # Kubernetes' default when the field is unset), and the resources the kubelet actually applied.
        "resize_policy": {c.get("name"): _resize_policy(c) for c in spec.get("containers") or []},
        "status_resources": {cs.get("name"): _resources(cs) for cs in statuses if cs.get("resources") is not None},
    }


def _resize_policy(container: dict) -> dict:
    policy = {"cpu": "NotRequired", "memory": "NotRequired"}
    for entry in container.get("resizePolicy") or []:
        if entry.get("resourceName") in policy and entry.get("restartPolicy"):
            policy[entry["resourceName"]] = entry["restartPolicy"]
    return policy


def node_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    labels = dict(meta.get("labels") or {})
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
    return {
        "name": meta.get("name"),
        "labels": labels,
        "instance_type": labels.get("node.kubernetes.io/instance-type") or labels.get("beta.kubernetes.io/instance-type"),
        "node_group": labels.get("eks.amazonaws.com/nodegroup"),
        "ready": ready,
        "unschedulable": bool(spec.get("unschedulable")),
        "taints": [
            {"key": t.get("key"), "value": t.get("value"), "effect": t.get("effect")} for t in spec.get("taints") or []
        ],
        "allocatable": {k: str(v) for k, v in (status.get("allocatable") or {}).items() if k in ("cpu", "memory")},
        "capacity": {k: str(v) for k, v in (status.get("capacity") or {}).items() if k in ("cpu", "memory")},
        # The kubelet performs an in-place resize, so its version (not the control plane's) decides support.
        "kubelet_version": (status.get("nodeInfo") or {}).get("kubeletVersion"),
    }


def event_view(raw: dict) -> dict:
    involved = raw.get("involvedObject") or raw.get("regarding") or {}
    return {
        "type": raw.get("type"),
        "reason": raw.get("reason"),
        "message": _message(raw.get("message") or raw.get("note")),
        "count": raw.get("count") or ((raw.get("series") or {}).get("count")) or 1,
        "last_seen": raw.get("lastTimestamp") or raw.get("eventTime") or _meta(raw).get("creationTimestamp"),
        "involved": {
            "kind": involved.get("kind"),
            "name": involved.get("name"),
            "namespace": involved.get("namespace"),
        },
    }


def scaledobject_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}
    target = spec.get("scaleTargetRef") or {}
    return {
        "kind": "ScaledObject",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "generation": meta.get("generation"),
        # KEDA defaults a missing kind to Deployment (apps/v1) — seen on stg.
        "target": {"kind": target.get("kind") or "Deployment", "name": target.get("name")},
        "min": spec.get("minReplicaCount"),
        "max": spec.get("maxReplicaCount"),
        "labels": ownership_labels(raw),
        "annotations": kept_annotations(raw),
    }


def hpa_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    target = spec.get("scaleTargetRef") or {}
    owners = meta.get("ownerReferences") or []
    return {
        "kind": "HorizontalPodAutoscaler",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "generation": meta.get("generation"),
        "target": {"kind": target.get("kind"), "name": target.get("name")},
        "min": spec.get("minReplicas", 1),
        "max": spec.get("maxReplicas"),
        "current_replicas": status.get("currentReplicas"),
        "desired_replicas": status.get("desiredReplicas"),
        "owned_by_scaledobject": any(o.get("kind") == "ScaledObject" for o in owners),
        "labels": ownership_labels(raw),
        "annotations": kept_annotations(raw),
    }


def label_selector(selector: dict) -> str | None:
    """A LabelSelector as the API's string form, matchExpressions included: a Deployment selected only
    by expressions must not end up listing every pod in its namespace."""
    terms = [f"{k}={v}" for k, v in (selector.get("matchLabels") or {}).items()]
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op == "In":
            terms.append(f"{key} in ({','.join(values)})")
        elif op == "NotIn":
            terms.append(f"{key} notin ({','.join(values)})")
        elif op == "Exists":
            terms.append(key)
        elif op == "DoesNotExist":
            terms.append(f"!{key}")
    return ",".join(terms) or None


def pdb_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "selector": dict((spec.get("selector") or {}).get("matchLabels") or {}),
        "min_available": spec.get("minAvailable"),
        "max_unavailable": spec.get("maxUnavailable"),
        "disruptions_allowed": status.get("disruptionsAllowed"),
    }


def pvc_view(raw: dict) -> dict:
    meta = _meta(raw)
    return {"name": meta.get("name"), "access_modes": list((raw.get("spec") or {}).get("accessModes") or [])}


# ---------------------------------------------------------------- more kinds (II-25740)
#
# Same rule as above: only the fields listed are copied. Nothing here copies env, args, command,
# volume definitions, ConfigMap values or Secret anything.


def _created(raw: dict) -> str | None:
    return _meta(raw).get("creationTimestamp")


def _owner(raw: dict) -> dict:
    owners = _meta(raw).get("ownerReferences") or []
    owner = owners[0] if owners else {}
    return {"kind": owner.get("kind"), "name": owner.get("name")}


def _controller(raw: dict) -> dict | None:
    owner = next((o for o in _meta(raw).get("ownerReferences") or [] if o.get("controller")), None)
    return {"kind": owner.get("kind"), "name": owner.get("name")} if owner else None


def _workload_common(raw: dict, kind: str) -> dict:
    meta = _meta(raw)
    return {
        "kind": kind,
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "uid": meta.get("uid"),
        "generation": meta.get("generation"),
        "created_at": _created(raw),
        "labels": ownership_labels(raw),
        "annotations": kept_annotations(raw),
    }


def statefulset_view(raw: dict) -> dict:
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    strategy = spec.get("updateStrategy") or {}
    return {
        **_workload_common(raw, "StatefulSet"),
        "observed_generation": status.get("observedGeneration"),
        "replicas": spec.get("replicas", 1),
        "selector": dict((spec.get("selector") or {}).get("matchLabels") or {}),
        "label_selector": label_selector(spec.get("selector") or {}),
        "service_name": spec.get("serviceName"),
        # The object that manages this StatefulSet, when one does (an operator's custom resource sets
        # spec.replicas itself).
        "controller": _controller(raw),
        "pod_management_policy": spec.get("podManagementPolicy", "OrderedReady"),
        "ordinals_start": (spec.get("ordinals") or {}).get("start", 0),
        "pvc_retention": {
            "when_deleted": (spec.get("persistentVolumeClaimRetentionPolicy") or {}).get("whenDeleted", "Retain"),
            "when_scaled": (spec.get("persistentVolumeClaimRetentionPolicy") or {}).get("whenScaled", "Retain"),
        },
        "strategy": {"type": strategy.get("type", "RollingUpdate"), "partition": (strategy.get("rollingUpdate") or {}).get("partition", 0)},
        "status": {
            "replicas": status.get("replicas", 0),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", 0),
            "updated": status.get("updatedReplicas", 0),
            "current_revision": status.get("currentRevision"),
            "update_revision": status.get("updateRevision"),
        },
        "volume_claim_templates": [
            {
                "name": (t.get("metadata") or {}).get("name"),
                "storage": str((((t.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage") or ""),
                "storage_class": (t.get("spec") or {}).get("storageClassName"),
            }
            for t in spec.get("volumeClaimTemplates") or []
        ],
        "conditions": _conditions(status),
        "template": pod_template_view(spec.get("template") or {}),
    }


def daemonset_view(raw: dict) -> dict:
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    strategy = spec.get("updateStrategy") or {}
    return {
        **_workload_common(raw, "DaemonSet"),
        "observed_generation": status.get("observedGeneration"),
        "selector": dict((spec.get("selector") or {}).get("matchLabels") or {}),
        "label_selector": label_selector(spec.get("selector") or {}),
        "strategy": {"type": strategy.get("type", "RollingUpdate"), "max_unavailable": (strategy.get("rollingUpdate") or {}).get("maxUnavailable", 1)},
        "status": {
            "desired": status.get("desiredNumberScheduled", 0),
            "current": status.get("currentNumberScheduled", 0),
            "ready": status.get("numberReady", 0),
            "updated": status.get("updatedNumberScheduled", 0),
            "available": status.get("numberAvailable", 0),
            "unavailable": status.get("numberUnavailable", 0),
            "misscheduled": status.get("numberMisscheduled", 0),
        },
        "conditions": _conditions(status),
        "template": pod_template_view(spec.get("template") or {}),
    }


def replicaset_view(raw: dict) -> dict:
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    annotations = _meta(raw).get("annotations") or {}
    return {
        **_workload_common(raw, "ReplicaSet"),
        "owner": _owner(raw),
        "revision": annotations.get("deployment.kubernetes.io/revision"),
        "replicas": spec.get("replicas", 0),
        "status": {"replicas": status.get("replicas", 0), "ready": status.get("readyReplicas", 0), "available": status.get("availableReplicas", 0)},
        "pod_template_hash": ((spec.get("template") or {}).get("metadata") or {}).get("labels", {}).get("pod-template-hash"),
        "template": pod_template_view(spec.get("template") or {}),
    }


def controllerrevision_view(raw: dict) -> dict:
    data = raw.get("data") or {}
    template = ((data.get("spec") or {}).get("template")) or {}
    return {
        **_workload_common(raw, "ControllerRevision"),
        "owner": _owner(raw),
        "revision": raw.get("revision"),
        "template": pod_template_view(template) if template else None,
    }


def job_view(raw: dict) -> dict:
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    return {
        **_workload_common(raw, "Job"),
        "owner": _owner(raw),
        "completions": spec.get("completions"),
        "parallelism": spec.get("parallelism"),
        "backoff_limit": spec.get("backoffLimit", 6),
        "suspend": bool(spec.get("suspend")),
        "status": {
            "active": status.get("active", 0),
            "succeeded": status.get("succeeded", 0),
            "failed": status.get("failed", 0),
            "start_time": status.get("startTime"),
            "completion_time": status.get("completionTime"),
        },
        "conditions": _conditions(status),
        "template": pod_template_view(spec.get("template") or {}),
    }


def cronjob_view(raw: dict) -> dict:
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    job_spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return {
        **_workload_common(raw, "CronJob"),
        "schedule": spec.get("schedule"),
        "time_zone": spec.get("timeZone"),
        "suspend": bool(spec.get("suspend")),
        "concurrency_policy": spec.get("concurrencyPolicy", "Allow"),
        "starting_deadline_seconds": spec.get("startingDeadlineSeconds"),
        "successful_jobs_history_limit": spec.get("successfulJobsHistoryLimit", 3),
        "failed_jobs_history_limit": spec.get("failedJobsHistoryLimit", 1),
        "status": {
            "active": len(status.get("active") or []),
            "active_jobs": [a.get("name") for a in status.get("active") or []],
            "last_schedule_time": status.get("lastScheduleTime"),
            "last_successful_time": status.get("lastSuccessfulTime"),
        },
        "template": pod_template_view(job_spec.get("template") or {}),
    }


def service_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}
    lb = ((raw.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    return {
        "kind": "Service",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "created_at": _created(raw),
        "labels": ownership_labels(raw),
        "type": spec.get("type", "ClusterIP"),
        "cluster_ip": spec.get("clusterIP"),
        "external": [i.get("hostname") or i.get("ip") for i in lb if i.get("hostname") or i.get("ip")],
        "external_name": spec.get("externalName"),
        "selector": dict(spec.get("selector") or {}),
        "ports": [
            {"name": p.get("name"), "port": p.get("port"), "target_port": p.get("targetPort"), "protocol": p.get("protocol", "TCP"), "node_port": p.get("nodePort")}
            for p in spec.get("ports") or []
        ],
    }


def endpointslice_view(raw: dict) -> dict:
    meta = _meta(raw)
    endpoints = raw.get("endpoints") or []
    ready = [e for e in endpoints if (e.get("conditions") or {}).get("ready") is not False]
    return {
        "kind": "EndpointSlice",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "service": (meta.get("labels") or {}).get("kubernetes.io/service-name"),
        "ready": len(ready),
        "not_ready": len(endpoints) - len(ready),
        "targets": [
            {
                "kind": (e.get("targetRef") or {}).get("kind"),
                "name": (e.get("targetRef") or {}).get("name"),
                "ready": (e.get("conditions") or {}).get("ready") is not False,
                "node": e.get("nodeName"),
            }
            for e in endpoints
        ],
        "ports": [{"name": p.get("name"), "port": p.get("port"), "protocol": p.get("protocol", "TCP")} for p in raw.get("ports") or []],
    }


def ingress_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}

    def backend(b: dict) -> dict:
        svc = (b or {}).get("service") or {}
        port = svc.get("port") or {}
        return {"service": svc.get("name"), "port": port.get("number") or port.get("name")}

    rules = []
    for rule in spec.get("rules") or []:
        for path in ((rule.get("http") or {}).get("paths")) or []:
            rules.append({"host": rule.get("host"), "path": path.get("path", "/"), "path_type": path.get("pathType"), **backend(path.get("backend"))})
    lb = ((raw.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    return {
        "kind": "Ingress",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "created_at": _created(raw),
        "labels": ownership_labels(raw),
        "class": spec.get("ingressClassName") or (meta.get("annotations") or {}).get("kubernetes.io/ingress.class"),
        "rules": rules,
        "default_backend": backend(spec.get("defaultBackend")) if spec.get("defaultBackend") else None,
        "tls_hosts": sorted({h for t in spec.get("tls") or [] for h in t.get("hosts") or []}),
        "address": [i.get("hostname") or i.get("ip") for i in lb if i.get("hostname") or i.get("ip")],
    }


def _peer(p: dict) -> dict:
    out = {}
    if p.get("podSelector") is not None:
        out["pods"] = label_selector(p["podSelector"]) or "all"
    if p.get("namespaceSelector") is not None:
        out["namespaces"] = label_selector(p["namespaceSelector"]) or "all"
    if p.get("ipBlock"):
        out["cidr"] = p["ipBlock"].get("cidr")
        out["except"] = list(p["ipBlock"].get("except") or [])
    return out


def networkpolicy_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}

    def rules(items, side):
        return [
            {"peers": [_peer(p) for p in r.get(side) or []] or ["any"], "ports": [{"port": p.get("port"), "protocol": p.get("protocol", "TCP")} for p in r.get("ports") or []]}
            for r in items or []
        ]

    return {
        "kind": "NetworkPolicy",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "pod_selector": label_selector(spec.get("podSelector") or {}) or "all pods",
        "policy_types": list(spec.get("policyTypes") or []),
        "ingress": rules(spec.get("ingress"), "from"),
        "egress": rules(spec.get("egress"), "to"),
    }


def configmap_view(raw: dict) -> dict:
    """Keys only, never values: a ConfigMap is where people put credentials they should have put in a Secret
    (test's Helm values ConfigMap holds plaintext passwords, docs/MEMORY.md 2026-10-13)."""
    meta = _meta(raw)
    return {
        "kind": "ConfigMap",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "created_at": _created(raw),
        "labels": ownership_labels(raw),
        "keys": sorted([*(raw.get("data") or {}).keys(), *(raw.get("binaryData") or {}).keys()]),
        "immutable": bool(raw.get("immutable")),
    }


def pvc_full_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    return {
        "kind": "PersistentVolumeClaim",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "created_at": _created(raw),
        "labels": ownership_labels(raw),
        "phase": status.get("phase"),
        "storage_class": spec.get("storageClassName"),
        "requested": str(((spec.get("resources") or {}).get("requests") or {}).get("storage") or ""),
        "capacity": str((status.get("capacity") or {}).get("storage") or ""),
        "access_modes": list(spec.get("accessModes") or []),
        "volume": spec.get("volumeName"),
        "conditions": _conditions(status),
    }


def pv_view(raw: dict) -> dict:
    meta = _meta(raw)
    spec = raw.get("spec") or {}
    claim = spec.get("claimRef") or {}
    return {
        "kind": "PersistentVolume",
        "name": meta.get("name"),
        "created_at": _created(raw),
        "capacity": str((spec.get("capacity") or {}).get("storage") or ""),
        "access_modes": list(spec.get("accessModes") or []),
        "reclaim_policy": spec.get("persistentVolumeReclaimPolicy"),
        "storage_class": spec.get("storageClassName"),
        "phase": (raw.get("status") or {}).get("phase"),
        "claim": f"{claim.get('namespace')}/{claim.get('name')}" if claim.get("name") else None,
    }


def storageclass_view(raw: dict) -> dict:
    meta = _meta(raw)
    annotations = meta.get("annotations") or {}
    return {
        "kind": "StorageClass",
        "name": meta.get("name"),
        "provisioner": raw.get("provisioner"),
        "reclaim_policy": raw.get("reclaimPolicy", "Delete"),
        "volume_binding_mode": raw.get("volumeBindingMode", "Immediate"),
        "allow_volume_expansion": bool(raw.get("allowVolumeExpansion")),
        "default": annotations.get("storageclass.kubernetes.io/is-default-class") == "true",
    }


def namespace_view(raw: dict) -> dict:
    meta = _meta(raw)
    return {
        "kind": "Namespace",
        "name": meta.get("name"),
        "created_at": _created(raw),
        "phase": (raw.get("status") or {}).get("phase"),
        "labels": dict(meta.get("labels") or {}),
    }


def resourcequota_view(raw: dict) -> dict:
    meta = _meta(raw)
    status = raw.get("status") or {}
    return {
        "kind": "ResourceQuota",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "hard": {k: str(v) for k, v in (status.get("hard") or (raw.get("spec") or {}).get("hard") or {}).items()},
        "used": {k: str(v) for k, v in (status.get("used") or {}).items()},
    }


def limitrange_view(raw: dict) -> dict:
    meta = _meta(raw)
    return {
        "kind": "LimitRange",
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "limits": [
            {
                "type": item.get("type"),
                **{
                    field: {k: str(v) for k, v in (item.get(key) or {}).items()}
                    for field, key in (("default_limit", "default"), ("default_request", "defaultRequest"), ("max", "max"), ("min", "min"))
                    if item.get(key)
                },
            }
            for item in (raw.get("spec") or {}).get("limits") or []
        ],
    }


def node_full_view(raw: dict) -> dict:
    """`node_view` plus the conditions and versions a "describe node" needs. No addresses."""
    status = raw.get("status") or {}
    info = status.get("nodeInfo") or {}
    return {
        **node_view(raw),
        "kind": "Node",
        "created_at": _created(raw),
        "conditions": _conditions(status),
        "kubelet_version": info.get("kubeletVersion"),
        "os_image": info.get("osImage"),
        "container_runtime": info.get("containerRuntimeVersion"),
        "architecture": info.get("architecture"),
        "zone": (_meta(raw).get("labels") or {}).get("topology.kubernetes.io/zone"),
        "capacity_pods": str((status.get("capacity") or {}).get("pods") or ""),
        "allocatable_pods": str((status.get("allocatable") or {}).get("pods") or ""),
    }


def crd_view(raw: dict) -> dict:
    spec = raw.get("spec") or {}
    names = spec.get("names") or {}
    versions = spec.get("versions") or []
    served = [v.get("name") for v in versions if v.get("served")]
    storage = next((v.get("name") for v in versions if v.get("storage")), served[0] if served else None)
    return {
        "kind": "CustomResourceDefinition",
        "name": _meta(raw).get("name"),
        "group": spec.get("group"),
        "resource_kind": names.get("kind"),
        "plural": names.get("plural"),
        "short_names": list(names.get("shortNames") or []),
        "scope": spec.get("scope"),
        "version": storage,
        "served_versions": served,
    }


def _scalars(obj: dict, limit: int = 20) -> dict:
    out = {}
    for key, value in (obj or {}).items():
        if isinstance(value, (str, int, float, bool)) and len(out) < limit:
            out[key] = _message(value) if isinstance(value, str) else value
    return out


def custom_view(raw: dict) -> dict:
    """Any other custom resource, read-only: identity, ownership, conditions and the top-level scalar fields
    of spec and status (redacted, bounded). Nested spec content is never copied: a custom resource can embed
    anything, including credentials (ExternalSecret, database operators)."""
    meta = _meta(raw)
    status = raw.get("status") or {}
    return {
        "kind": raw.get("kind"),
        "api_version": raw.get("apiVersion"),
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "created_at": _created(raw),
        "labels": ownership_labels(raw),
        "spec": _scalars(raw.get("spec") or {}),
        "status": _scalars(status),
        "conditions": _conditions(status) if isinstance(status, dict) else [],
    }


def pod_metrics_view(raw: dict) -> dict:
    meta = _meta(raw)
    return {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "containers": {
            c.get("name"): {k: str(v) for k, v in (c.get("usage") or {}).items() if k in ("cpu", "memory")}
            for c in raw.get("containers") or []
        },
    }


def node_metrics_view(raw: dict) -> dict:
    return {
        "name": _meta(raw).get("name"),
        "usage": {k: str(v) for k, v in (raw.get("usage") or {}).items() if k in ("cpu", "memory")},
    }
