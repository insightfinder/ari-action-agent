"""Who controls a Deployment's replica count, and who owns its manifest.

Two separate questions with separate consequences:

- **Autoscaler** (KEDA ScaledObject or HPA) — decides whether a replica
  change can be made on the Deployment at all. A manual `replicas` change
  under an autoscaler is overwritten within seconds, so the only real
  lever is the autoscaler's own min/max, and only on explicit request.
- **Ownership** (Flux Kustomization / Helm / none) — never blocks a
  change, but decides what the user must be told about it lasting.
"""

import re
from datetime import datetime, timedelta, timezone

KEDA_GENERATED_HPA_PREFIX = "keda-hpa-"


def find_autoscaler(deployment_name: str, scaledobjects: list[dict], hpas: list[dict]) -> dict | None:
    """The autoscaler governing `deployment_name`, KEDA first.

    A KEDA-generated HPA (`keda-hpa-*`, owned by a ScaledObject) is never
    returned as the thing to change — KEDA would overwrite it — but if it is
    all that is visible, the Deployment is still reported as KEDA-managed.
    """
    for so in scaledobjects:
        target = so.get("target") or {}
        if target.get("name") == deployment_name and (target.get("kind") or "Deployment") == "Deployment":
            return so
    keda_hpa = None
    for hpa in hpas:
        target = hpa.get("target") or {}
        if target.get("name") != deployment_name or (target.get("kind") or "Deployment") != "Deployment":
            continue
        if hpa.get("owned_by_scaledobject") or (hpa.get("name") or "").startswith(KEDA_GENERATED_HPA_PREFIX):
            keda_hpa = hpa
            continue
        return hpa
    if keda_hpa:
        return {
            "kind": "ScaledObject",
            "name": None,
            "namespace": keda_hpa.get("namespace"),
            "target": keda_hpa.get("target"),
            "min": keda_hpa.get("min"),
            "max": keda_hpa.get("max"),
            "inferred_from_hpa": keda_hpa.get("name"),
        }
    return None


def autoscaler_fingerprint(autoscaler: dict | None) -> dict | None:
    if not autoscaler:
        return None
    return {
        "kind": autoscaler.get("kind"),
        "name": autoscaler.get("name"),
        "generation": autoscaler.get("generation"),
        "min": autoscaler.get("min"),
        "max": autoscaler.get("max"),
    }


def classify_ownership(labels: dict, annotations: dict | None = None) -> dict:
    labels = labels or {}
    annotations = annotations or {}
    helm_release = labels.get("helm.toolkit.fluxcd.io/name")
    if helm_release:
        return {
            "class": "helm",
            "flux_helmrelease": {
                "name": helm_release,
                "namespace": labels.get("helm.toolkit.fluxcd.io/namespace"),
            },
        }
    if labels.get("app.kubernetes.io/managed-by") == "Helm" or annotations.get("meta.helm.sh/release-name"):
        return {
            "class": "helm",
            "helm_release": {
                "name": annotations.get("meta.helm.sh/release-name") or labels.get("app.kubernetes.io/instance"),
                "namespace": annotations.get("meta.helm.sh/release-namespace"),
            },
        }
    kustomization = labels.get("kustomize.toolkit.fluxcd.io/name")
    if kustomization:
        return {
            "class": "kustomization",
            "flux_kustomization": {
                "name": kustomization,
                "namespace": labels.get("kustomize.toolkit.fluxcd.io/namespace"),
            },
        }
    return {"class": "unowned"}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def upgrade_activity(history: list[dict], now: datetime | None = None) -> dict:
    """How often a HelmRelease has been upgraded lately — the best available
    estimate of how soon a direct change to it will be overwritten."""
    now = now or datetime.now(timezone.utc)
    times = [t for t in (_parse_time(h.get("last_deployed")) for h in history or []) if t]
    recent = [t for t in times if now - t <= timedelta(hours=24)]
    return {
        "upgrades_last_24h": len(recent),
        "last_upgrade_at": max(times).isoformat() if times else None,
    }


def drift_notice(ownership: dict, chain: dict | None = None) -> str:
    """One plain sentence about how long a direct change will last."""
    cls = ownership.get("class")
    chain = chain or {}
    if cls == "kustomization":
        interval = (chain.get("kustomization") or {}).get("interval")
        when = f" (reconcile interval {interval})" if interval else ""
        return (
            "This object is applied directly by a Flux Kustomization: a direct change is reverted at the "
            f"next reconcile{when}."
        )
    if cls == "helm":
        activity = (chain.get("helmrelease") or {}).get("upgrade_activity") or {}
        detail = ""
        if activity.get("upgrades_last_24h") is not None and chain.get("helmrelease"):
            detail = (
                f" Its release was upgraded {activity['upgrades_last_24h']} time(s) in the last 24h"
                + (f", most recently at {activity['last_upgrade_at']}" if activity.get("last_upgrade_at") else "")
                + "."
            )
        return (
            "This object is rendered by a Helm release: a direct change lasts only until the next upgrade "
            "of that release (including one triggered by an image update), which silently restores the "
            f"chart's value.{detail}"
        )
    return "This object has no GitOps or Helm owner: the change persists, but nothing in version control records it."


def deployment_of(pod: dict) -> str | None:
    """The Deployment that owns `pod` (a pod view), read from its ReplicaSet owner and pod-template-hash label."""
    owner = pod.get("owner_name")
    template_hash = (pod.get("labels") or {}).get("pod-template-hash")
    if pod.get("owner_kind") == "ReplicaSet" and owner and template_hash and owner.endswith(f"-{template_hash}"):
        return owner[: -len(template_hash) - 1]
    return None


# The workload kinds this agent resolves a request to (find_workload) and changes as a whole.
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


def workload_of(pod: dict) -> tuple[str, str] | None:
    """`(kind, name)` of the workload that owns `pod` (a pod view): a Deployment through its ReplicaSet, or a
    StatefulSet or DaemonSet directly. None for a bare pod, a Job's pod or a static (mirror) pod."""
    deployment = deployment_of(pod)
    if deployment:
        return "Deployment", deployment
    if pod.get("owner_kind") in ("StatefulSet", "DaemonSet") and pod.get("owner_name"):
        return pod["owner_kind"], pod["owner_name"]
    return None


# What find_workload resolves a request to: the workloads above, plus a CronJob (suspend, resume).
RESOLVABLE_KINDS = (*WORKLOAD_KINDS, "CronJob")


def is_cronjob_job(job: str | None, cronjob: str) -> bool:
    """Whether a Job name is one `cronjob` makes on its schedule: `<cronjob>-<minutes since epoch>`. Read from
    the name: a pod view carries its Job, not the Job's own owner. A Job someone made by hand
    (`kubectl create job --from=cronjob/...`) has a name of their choosing and is not counted."""
    return bool(job) and re.fullmatch(rf"{re.escape(cronjob)}-\d{{8,}}", job) is not None


def owned_by(pod: dict, kind: str, name: str) -> bool:
    if kind == "CronJob":
        return pod.get("owner_kind") == "Job" and is_cronjob_job(pod.get("owner_name"), name)
    return workload_of(pod) == (kind, name)


# ---------------------------------------------------------------- draining a node


def workload_key(pod: dict) -> str | None:
    """`Kind/namespace/name` of what owns `pod` and replaces it once it is evicted: its Deployment,
    StatefulSet or DaemonSet, else its direct owner (a Job, a bare ReplicaSet). None for a bare pod."""
    owner = workload_of(pod)
    if owner:
        return f"{owner[0]}/{pod.get('namespace')}/{owner[1]}"
    if pod.get("owner_kind") and pod.get("owner_name"):
        return f"{pod['owner_kind']}/{pod.get('namespace')}/{pod['owner_name']}"
    return None


def drain_plan(pods: list[dict]) -> dict:
    """What a drain does with each pod on the node, by kubectl drain's default rules: a DaemonSet's pod and a
    static pod's mirror stay (their controller would put them straight back; an eviction cannot move a mirror),
    a finished or already deleting pod is left alone, a pod no controller owns blocks the drain (nothing would
    recreate it), and every other pod is evicted. `empty_dir` lists the evicted pods whose emptyDir data is
    lost."""
    plan: dict = {"evict": [], "bare": [], "empty_dir": [], "skipped": {"daemonset": 0, "mirror": 0, "finished": 0, "deleting": 0}}
    for pod in pods:
        if pod.get("phase") in ("Succeeded", "Failed"):
            plan["skipped"]["finished"] += 1
        elif pod.get("deleting"):
            plan["skipped"]["deleting"] += 1
        elif pod.get("mirror"):
            plan["skipped"]["mirror"] += 1
        elif pod.get("owner_kind") == "DaemonSet":
            plan["skipped"]["daemonset"] += 1
        elif workload_key(pod) is None:
            plan["bare"].append(pod)
        else:
            plan["evict"].append(pod)
            if pod.get("empty_dir"):
                plan["empty_dir"].append(pod)
    return plan


# Taints that say something else cordoned a node to remove or replace it.
_GOING_AWAY_TAINTS = (
    "ToBeDeletedByClusterAutoscaler",
    "DeletionCandidateOfClusterAutoscaler",
    "aws-node-termination-handler",
    "karpenter.sh/disrupt",
    "node.kubernetes.io/out-of-service",
)


def going_away_taints(node: dict) -> list[str]:
    return [t["key"] for t in node.get("taints") or [] if t.get("key") and any(m in t["key"] for m in _GOING_AWAY_TAINTS)]
