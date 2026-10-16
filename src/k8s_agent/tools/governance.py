"""Who controls a Deployment's replica count, and who owns its manifest.

Two separate questions with separate consequences:

- **Autoscaler** (KEDA ScaledObject or HPA) — decides whether a replica
  change can be made on the Deployment at all. A manual `replicas` change
  under an autoscaler is overwritten within seconds, so the only real
  lever is the autoscaler's own min/max, and only on explicit request.
- **Ownership** (Flux Kustomization / Helm / none) — never blocks a
  change, but decides what the user must be told about it lasting.
"""

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
