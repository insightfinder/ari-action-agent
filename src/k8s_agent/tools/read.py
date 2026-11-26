"""Read-only cluster access. Every function returns whitelisted views
(`views.py`), never raw objects, so nothing a capability passes on can
carry a field this agent did not choose to read.

Reads retry a couple of times on transient failures (5xx, 429, transport
errors); writes never do (see `write.py`).
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

from urllib3.exceptions import HTTPError as Urllib3Error

from . import capacity, catalog, governance, persistence, views
from .client import CHART_SOURCES, FLUX_SOURCES, HELMRELEASE_KINDS, api_status, get_client, safe_error
from .redact import bounded, redact_text

_READ_ATTEMPTS = int(os.getenv("K8S_READ_ATTEMPTS", "3"))
_LOG_TAIL_LINES = 40
_LOG_MAX_BYTES = 8000


async def _retry(fn):
    delay = 0.5
    for attempt in range(1, _READ_ATTEMPTS + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — classified below
            status = api_status(exc)
            transient = isinstance(exc, (Urllib3Error, ConnectionError, TimeoutError)) or status in (429, 500, 502, 503, 504)
            if not transient or attempt == _READ_ATTEMPTS:
                raise
            await asyncio.sleep(delay)
            delay *= 2


def _selector(labels: dict | str | None) -> str | None:
    if isinstance(labels, str):
        return labels or None
    return ",".join(f"{k}={v}" for k, v in labels.items()) if labels else None


async def _list_dicts(kind: str, *, namespace=None, labels=None, fields=None) -> list[dict]:
    field_selector = ",".join(f"{k}={v}" for k, v in fields.items()) if fields else None
    return await _retry(lambda: get_client().list(kind, namespace, _selector(labels), field_selector))


async def _get_dict(kind: str, name, namespace=None) -> dict:
    return await _retry(lambda: get_client().get(kind, name, namespace))


# ---------------------------------------------------------------- workloads


async def list_deployments(namespace: str | None = None) -> list[dict]:
    raws = await _list_dicts("Deployment", namespace=namespace)
    return [{"namespace": r["metadata"].get("namespace"), "name": r["metadata"].get("name")} for r in raws]


async def get_deployment(namespace: str, name: str) -> dict:
    try:
        return views.deployment_view(await _get_dict("Deployment", name, namespace))
    except Exception as exc:
        raise RuntimeError(f"could not read Deployment {namespace}/{name}: {safe_error(exc)}") from None


async def list_workloads(namespace: str | None = None) -> list[dict]:
    """`{kind, namespace, name}` of every Deployment, StatefulSet, DaemonSet and CronJob. Deployments must be
    readable; a list the ServiceAccount may not read (a chart older than 0.3.14) is left out."""
    out = [{"kind": "Deployment", **d} for d in await list_deployments(namespace)]
    for kind in ("StatefulSet", "DaemonSet", "CronJob"):
        try:
            raws = await _list_dicts(kind, namespace=namespace)
        except Exception as exc:  # noqa: BLE001
            if api_status(exc) in (403, 404):
                continue
            raise
        out.extend({"kind": kind, "namespace": r["metadata"].get("namespace"), "name": r["metadata"].get("name")} for r in raws)
    return out


async def get_workload(kind: str, namespace: str, name: str) -> dict:
    """One Deployment, StatefulSet, DaemonSet or CronJob as its view."""
    if kind not in governance.RESOLVABLE_KINDS:
        raise ValueError(f"not a workload kind: {kind!r}")
    if kind == "Deployment":
        return await get_deployment(namespace, name)
    try:
        return catalog.get(kind).view(await _get_dict(kind, name, namespace))
    except Exception as exc:
        raise RuntimeError(f"could not read {kind} {namespace}/{name}: {safe_error(exc)}") from None


async def list_deployment_views(namespace: str | None = None) -> list[dict]:
    return [views.deployment_view(r) for r in await _list_dicts("Deployment", namespace=namespace)]


async def list_pods(namespace: str | None = None, selector: dict | str | None = None, active_only: bool = False) -> list[dict]:
    raws = await _list_dicts("Pod", namespace=namespace, labels=selector or None)
    pods = [views.pod_view(r) for r in raws]
    if active_only:
        pods = [p for p in pods if p.get("phase") not in ("Succeeded", "Failed")]
    return pods


class ReadForbidden(RuntimeError):
    """The ServiceAccount may not read this kind (HTTP 403): reported as a gap, never as an empty result."""


class KindNotServed(RuntimeError):
    """The API server does not serve this kind (HTTP 404 on the list, e.g. KEDA or Flux not installed)."""


async def list_views(kind: str, namespace: str | None = None, label_selector: str | None = None) -> list[dict]:
    """Every object of a catalog kind, as its whitelisted view. A 403/404 becomes a typed error the caller
    reports as a gap, so a list the agent could not read is never shown as an empty one."""
    k = catalog.get(kind)
    try:
        raws = await _list_dicts(kind, namespace=namespace if k.namespaced else None, labels=label_selector)
    except Exception as exc:  # noqa: BLE001
        status = api_status(exc)
        if status == 403:
            raise ReadForbidden(f"not allowed to list {kind} ({safe_error(exc)})") from None
        if status == 404:
            raise KindNotServed(f"this cluster does not serve {kind}") from None
        raise RuntimeError(f"could not list {kind}: {safe_error(exc)}") from None
    return [k.view(r) for r in raws]


async def namespace_names() -> list[str] | None:
    """Every namespace's name, or None when the ServiceAccount may not list namespaces."""
    try:
        return sorted(v.get("name") for v in await list_views("Namespace"))
    except ReadForbidden:
        return None


async def get_pod(namespace: str, name: str) -> dict | None:
    """The pod's view, or None when it no longer exists (replaced, evicted, scaled away)."""
    try:
        return views.pod_view(await _get_dict("Pod", name, namespace))
    except Exception as exc:  # noqa: BLE001
        if api_status(exc) == 404:
            return None
        raise RuntimeError(f"could not read Pod {namespace}/{name}: {safe_error(exc)}") from None


async def get_resize_support() -> dict:
    """Whether this API server serves in-place pod resize (`pods/resize`), read live from discovery, plus its
    version for the record. Discovery, not the version number, decides: 1.33/1.34 serve it as beta (on by
    default), and an alpha cluster with the feature gate off does not serve it at all. Anything unreadable
    counts as unsupported."""
    out: dict = {"supported": False, "server_version": None}
    try:
        out["server_version"] = (await _retry(lambda: get_client().server_version())).get("gitVersion")
    except Exception as exc:  # noqa: BLE001 — the version is informational
        out["version_error"] = safe_error(exc)
    try:
        resources = await _retry(lambda: get_client().core_resources())
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"API discovery (/api/v1) could not be read: {safe_error(exc)}"
        return out
    out["supported"] = "pods/resize" in resources
    if not out["supported"]:
        out["reason"] = (
            f"the API server ({out['server_version'] or 'unknown version'}) does not serve pods/resize: in-place "
            "pod resize needs Kubernetes 1.33 or later (beta in 1.33-1.34, GA from 1.35)"
        )
    return out


async def list_events(namespace: str | None = None, involved_name: str | None = None, since_minutes: int = 60) -> list[dict]:
    """Warning events, newest first. Events expire (an hour by default), so
    an empty list means "nothing recent was recorded", not "healthy"."""
    fields = {"type": "Warning"}
    if involved_name:
        fields["involvedObject.name"] = involved_name
    raws = await _list_dicts("Event", namespace=namespace, fields=fields)
    events = [views.event_view(r) for r in raws]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)

    def _when(e):
        try:
            return datetime.fromisoformat(str(e.get("last_seen")).replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    recent = [e for e in events if _when(e) >= cutoff]
    return sorted(recent, key=_when, reverse=True)


async def list_scheduling_events(namespace: str | None = None, since_minutes: int = 30) -> list[dict]:
    """Normal *and* Warning events relevant to scheduling (TriggeredScaleUp is
    a Normal event), plus Cluster Autoscaler's events in kube-system."""
    wanted = {"TriggeredScaleUp", "NotTriggerScaleUp", "FailedScheduling", "FailedToScaleUpGroup", "FailedScaleUp", "ScaleUpFailed"}
    collected = []
    for ns in {namespace, "kube-system"}:
        if ns is None:
            continue
        try:
            raws = await _list_dicts("Event", namespace=ns)
        except Exception:  # noqa: BLE001 — scheduling context is best-effort
            continue
        collected.extend(views.event_view(r) for r in raws if r.get("reason") in wanted)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    out = []
    for e in collected:
        try:
            when = datetime.fromisoformat(str(e.get("last_seen")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when >= cutoff:
            out.append(e)
    return out


_LOG_UNAVAILABLE = "unable to retrieve container logs"


async def get_pod_log(namespace: str, pod: str, container: str | None = None, previous: bool = True) -> str | None:
    """The redacted, bounded tail of a container's log — by default the
    *previous* (crashed) container's, which is where a crash loop's cause is.

    Seen on the test cluster (2026-10-13): when the previous container's log
    file is already gone, the kubelet answers HTTP 200 with the text "unable
    to retrieve container logs for containerd://..." instead of an error,
    while the current container — terminated between restarts — still holds
    the crash output. That text is never returned as a log; the current
    container's log is tried instead.
    """

    async def fetch(prev: bool) -> str | None:
        try:
            text = await _retry(
                lambda: get_client().pod_log(namespace, pod, container, prev, _LOG_TAIL_LINES, _LOG_MAX_BYTES)
            )
        except Exception:  # noqa: BLE001 — a log tail is optional evidence
            return None
        if not text or text.strip().startswith(_LOG_UNAVAILABLE):
            return None
        return text

    text = await fetch(previous)
    if text is None and previous:
        text = await fetch(False)
    return bounded(redact_text(text), _LOG_MAX_BYTES) if text else None


async def get_pod_metrics(namespace: str | None = None) -> dict:
    """`{"usage": {"namespace/pod": {container: {cpu, memory}}}}` or a gap marker."""
    try:
        raws = await _list_dicts("PodMetrics", namespace=namespace)
    except Exception as exc:  # noqa: BLE001
        return {"gap": f"metrics.k8s.io unavailable: {safe_error(exc)}"}
    usage: dict = {}
    for raw in raws:
        meta = raw.get("metadata") or {}
        key = f"{meta.get('namespace')}/{meta.get('name')}"
        usage[key] = {
            c.get("name"): {k: str(v) for k, v in (c.get("usage") or {}).items() if k in ("cpu", "memory")}
            for c in raw.get("containers") or []
        }
    return {"usage": usage}


# ---------------------------------------------------------------- autoscalers


async def list_scaledobjects(namespace: str) -> list[dict]:
    try:
        return [views.scaledobject_view(r) for r in await _list_dicts("ScaledObject", namespace=namespace)]
    except Exception as exc:  # noqa: BLE001
        if api_status(exc) == 404:  # KEDA not installed
            return []
        raise RuntimeError(f"could not list ScaledObjects in {namespace}: {safe_error(exc)}") from None


async def list_hpas(namespace: str) -> list[dict]:
    try:
        return [views.hpa_view(r) for r in await _list_dicts("HorizontalPodAutoscaler", namespace=namespace)]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not list HPAs in {namespace}: {safe_error(exc)}") from None


async def find_autoscaler(namespace: str, deployment: str) -> dict | None:
    scaledobjects, hpas = await asyncio.gather(list_scaledobjects(namespace), list_hpas(namespace))
    return governance.find_autoscaler(deployment, scaledobjects, hpas)


# ---------------------------------------------------------------- capacity context


async def list_nodes() -> list[dict]:
    return [views.node_view(r) for r in await _list_dicts("Node")]


async def get_node(name: str) -> dict | None:
    """One node as its view, or None when there is no such node (Cluster Autoscaler removes nodes)."""
    try:
        return views.node_view(await _get_dict("Node", name))
    except Exception as exc:  # noqa: BLE001
        if api_status(exc) == 404:
            return None
        raise RuntimeError(f"could not read node {name}: {safe_error(exc)}") from None


async def list_pods_on_node(node: str) -> list[dict]:
    """Every pod bound to `node` (field selector `spec.nodeName`), as views."""
    return [views.pod_view(r) for r in await _list_dicts("Pod", fields={"spec.nodeName": node})]


async def get_ca_status() -> dict:
    try:
        raw = await _get_dict("ConfigMap", "cluster-autoscaler-status", "kube-system")
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"could not read cluster-autoscaler-status: {safe_error(exc)}"}
    return capacity.parse_ca_status((raw.get("data") or {}).get("status"))


async def list_pdbs(namespace: str) -> list[dict]:
    try:
        return [views.pdb_view(r) for r in await _list_dicts("PodDisruptionBudget", namespace=namespace)]
    except Exception:  # noqa: BLE001 — pre-flight context only
        return []


async def get_pvcs(namespace: str, names: list[str]) -> list[dict]:
    out = []
    for name in names:
        try:
            out.append(views.pvc_view(await _get_dict("PersistentVolumeClaim", name, namespace)))
        except Exception:  # noqa: BLE001
            out.append({"name": name, "access_modes": [], "unreadable": True})
    return out


# ---------------------------------------------------------------- ownership chain


async def _get_first(classes, name, namespace) -> dict | None:
    last_exc = None
    for kind in classes:
        try:
            return await _get_dict(kind, name, namespace)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if api_status(exc) == 404:
                continue
            raise
    if last_exc is not None and api_status(last_exc) == 404:
        return None
    return None


async def _add_chart_source(chart_source: dict) -> None:
    """Fill in the chart source's URL. Optional: a failure here is noted on
    the chart source and never breaks the chain."""
    classes = CHART_SOURCES.get(chart_source.get("kind"))
    if not classes or not chart_source.get("name"):
        return
    try:
        raw = await _get_first(classes, chart_source["name"], chart_source.get("namespace"))
    except Exception as exc:  # noqa: BLE001
        chart_source["read_error"] = safe_error(exc)
        return
    if raw is None:
        chart_source["read_error"] = "not found"
        return
    chart_source.update(persistence.chart_source_details(raw))


async def get_flux_chain(labels: dict, annotations: dict | None = None) -> dict:
    """Follow the changed object's ownership labels to the Flux source it is
    reconciled from. Every link is read from the cluster; on any failure the
    chain stops and says where (`broken_at`)."""
    ownership = governance.classify_ownership(labels, annotations)
    chain: dict = {"ownership": ownership, "helmrelease": None, "kustomization": None, "source": None, "broken_at": None}
    if ownership["class"] == "unowned":
        return chain

    kustomization_ref = None
    hr_ref = ownership.get("flux_helmrelease")
    if ownership["class"] == "helm" and not hr_ref:
        chain["broken_at"] = "Helm release is not managed by a Flux HelmRelease (no helm.toolkit.fluxcd.io labels)"
        return chain
    if hr_ref:
        try:
            raw = await _get_first(HELMRELEASE_KINDS, hr_ref["name"], hr_ref.get("namespace"))
        except Exception as exc:  # noqa: BLE001
            chain["broken_at"] = f"HelmRelease {hr_ref.get('namespace')}/{hr_ref['name']}: {safe_error(exc)}"
            return chain
        if raw is None:
            chain["broken_at"] = f"HelmRelease {hr_ref.get('namespace')}/{hr_ref['name']} not found"
            return chain
        history = [{"last_deployed": h.get("lastDeployed")} for h in (raw.get("status") or {}).get("history") or []]
        chain["helmrelease"] = persistence.helmrelease_summary(raw, governance.upgrade_activity(history))
        await _add_chart_source(chain["helmrelease"]["chart_source"])
        kustomization_ref = chain["helmrelease"]["kustomization_ref"]
        if not kustomization_ref.get("name"):
            chain["broken_at"] = "HelmRelease has no kustomize.toolkit.fluxcd.io labels (not applied by a Kustomization)"
            return chain
    else:
        kustomization_ref = ownership.get("flux_kustomization")

    try:
        raw = await _get_first(["Kustomization"], kustomization_ref["name"], kustomization_ref.get("namespace"))
    except Exception as exc:  # noqa: BLE001
        chain["broken_at"] = f"Kustomization {kustomization_ref.get('namespace')}/{kustomization_ref['name']}: {safe_error(exc)}"
        return chain
    if raw is None:
        chain["broken_at"] = f"Kustomization {kustomization_ref.get('namespace')}/{kustomization_ref['name']} not found"
        return chain
    chain["kustomization"] = persistence.kustomization_summary(raw)

    source_ref = chain["kustomization"]["source_ref"]
    classes = FLUX_SOURCES.get(source_ref.get("kind"))
    if not classes:
        chain["broken_at"] = f"unsupported Flux source kind {source_ref.get('kind')!r}"
        return chain
    try:
        raw = await _get_first(classes, source_ref["name"], source_ref.get("namespace"))
    except Exception as exc:  # noqa: BLE001
        chain["broken_at"] = f"{source_ref.get('kind')} {source_ref.get('namespace')}/{source_ref.get('name')}: {safe_error(exc)}"
        return chain
    if raw is None:
        chain["broken_at"] = f"{source_ref.get('kind')} {source_ref.get('namespace')}/{source_ref.get('name')} not found"
        return chain
    chain["source"] = persistence.source_summary(raw, source_ref.get("kind"))
    return chain
