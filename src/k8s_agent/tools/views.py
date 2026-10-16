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


def pod_template_view(template: dict) -> dict:
    spec = template.get("spec") or {}
    return {
        "labels": dict((template.get("metadata") or {}).get("labels") or {}),
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
        "generation": meta.get("generation"),
        "observed_generation": status.get("observedGeneration"),
        "replicas": spec.get("replicas", 1),
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
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
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
        "requests": [_resources(c)["requests"] for c in spec.get("containers") or []],
        "limits": {c.get("name"): _resources(c)["limits"] for c in spec.get("containers") or []},
        "resources": {c.get("name"): _resources(c) for c in spec.get("containers") or []},
    }


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
        "selector": dict((spec.get("selector") or {}).get("matchLabels") or {}),
        "min_available": spec.get("minAvailable"),
        "max_unavailable": spec.get("maxUnavailable"),
        "disruptions_allowed": status.get("disruptionsAllowed"),
    }


def pvc_view(raw: dict) -> dict:
    meta = _meta(raw)
    return {"name": meta.get("name"), "access_modes": list((raw.get("spec") or {}).get("accessModes") or [])}
