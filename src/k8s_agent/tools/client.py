"""The one Kubernetes client this agent uses: the official `kubernetes`
Python client (kubernetes-client/python), with the pod's own
ServiceAccount.

Chosen for production stability: it is maintained under the Kubernetes
organisation and released in step with Kubernetes itself. It is
synchronous, so every call runs in a worker thread (`asyncio.to_thread`)
and never blocks the Temporal worker's event loop.

There is no kubeconfig fallback and no credential in any request. The
agent only ever runs inside the cluster it manages, with
`automountServiceAccountToken: true` on its worker; its authority is
exactly the RBAC bound to that ServiceAccount (see
docs/examples/k8s-rbac.yaml). The token is read by the client from the
projected volume and is never logged, returned or copied anywhere.

Everything else in this package talks to the cluster only through
`Kube` below, by kind name, and only ever sees plain camelCase dicts (the
API's own JSON shape) — which is also what the tests' fake implements.
"""

import asyncio
import json
import os
import threading

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from .redact import bounded, redact_text

FIELD_MANAGER = "ari-k8s-agent"
_TIMEOUT_SECONDS = float(os.getenv("K8S_API_TIMEOUT_SECONDS", "15"))
_PAGE_SIZE = 500

PATCH_MERGE = "application/merge-patch+json"
PATCH_STRATEGIC = "application/strategic-merge-patch+json"

# kind -> (group, version, plural) for objects read through CustomObjectsApi.
CUSTOM = {
    "ScaledObject": ("keda.sh", "v1alpha1", "scaledobjects"),
    "PodMetrics": ("metrics.k8s.io", "v1beta1", "pods"),
    "HelmRelease": ("helm.toolkit.fluxcd.io", "v2", "helmreleases"),
    "HelmReleaseV2beta2": ("helm.toolkit.fluxcd.io", "v2beta2", "helmreleases"),
    "Kustomization": ("kustomize.toolkit.fluxcd.io", "v1", "kustomizations"),
    "GitRepository": ("source.toolkit.fluxcd.io", "v1", "gitrepositories"),
    "OCIRepository": ("source.toolkit.fluxcd.io", "v1", "ocirepositories"),
    "OCIRepositoryV1beta2": ("source.toolkit.fluxcd.io", "v1beta2", "ocirepositories"),
    "Bucket": ("source.toolkit.fluxcd.io", "v1", "buckets"),
    "BucketV1beta2": ("source.toolkit.fluxcd.io", "v1beta2", "buckets"),
    "HelmRepository": ("source.toolkit.fluxcd.io", "v1", "helmrepositories"),
    "HelmRepositoryV1beta2": ("source.toolkit.fluxcd.io", "v1beta2", "helmrepositories"),
}
# Flux source kind -> kinds to try, newest API version first.
FLUX_SOURCES = {
    "GitRepository": ["GitRepository"],
    "OCIRepository": ["OCIRepository", "OCIRepositoryV1beta2"],
    "Bucket": ["Bucket", "BucketV1beta2"],
}
# Where a HelmRelease's chart can come from (`chart.spec.sourceRef` or
# `chartRef`). A `HelmChart` chartRef is reported by name only.
CHART_SOURCES = {**FLUX_SOURCES, "HelmRepository": ["HelmRepository", "HelmRepositoryV1beta2"]}
HELMRELEASE_KINDS = ["HelmRelease", "HelmReleaseV2beta2"]


class Kube:
    """Async, dict-in/dict-out access to exactly the operations this agent uses."""

    def __init__(self, api_client: k8s.ApiClient):
        self._api = api_client
        self._core = k8s.CoreV1Api(api_client)
        self._apps = k8s.AppsV1Api(api_client)
        self._autoscaling = k8s.AutoscalingV2Api(api_client)
        self._policy = k8s.PolicyV1Api(api_client)
        self._custom = k8s.CustomObjectsApi(api_client)

    def _dict(self, obj) -> dict:
        return obj if isinstance(obj, dict) else self._api.sanitize_for_serialization(obj)

    # ------------------------------------------------------------ dispatch

    def _typed(self, kind: str):
        """(namespaced get, namespaced list, all-namespaces list) for built-in kinds."""
        c, a = self._core, self._apps
        return {
            "Deployment": (a.read_namespaced_deployment, a.list_namespaced_deployment, a.list_deployment_for_all_namespaces),
            "Pod": (c.read_namespaced_pod, c.list_namespaced_pod, c.list_pod_for_all_namespaces),
            "Event": (c.read_namespaced_event, c.list_namespaced_event, c.list_event_for_all_namespaces),
            "ConfigMap": (c.read_namespaced_config_map, c.list_namespaced_config_map, None),
            "PersistentVolumeClaim": (c.read_namespaced_persistent_volume_claim, c.list_namespaced_persistent_volume_claim, None),
            "Node": (c.read_node, None, c.list_node),
            "HorizontalPodAutoscaler": (
                self._autoscaling.read_namespaced_horizontal_pod_autoscaler,
                self._autoscaling.list_namespaced_horizontal_pod_autoscaler,
                None,
            ),
            "PodDisruptionBudget": (
                self._policy.read_namespaced_pod_disruption_budget,
                self._policy.list_namespaced_pod_disruption_budget,
                None,
            ),
        }[kind]

    def _get_sync(self, kind: str, name: str, namespace: str | None) -> dict:
        if kind in CUSTOM:
            group, version, plural = CUSTOM[kind]
            return self._custom.get_namespaced_custom_object(
                group, version, namespace, plural, name, _request_timeout=_TIMEOUT_SECONDS
            )
        get, _, _ = self._typed(kind)
        if kind == "Node":
            return self._dict(get(name, _request_timeout=_TIMEOUT_SECONDS))
        return self._dict(get(name, namespace, _request_timeout=_TIMEOUT_SECONDS))

    def _list_sync(self, kind, namespace, label_selector, field_selector, max_items) -> list[dict]:
        items: list[dict] = []
        token = None
        while True:
            kwargs = {"limit": _PAGE_SIZE, "_request_timeout": _TIMEOUT_SECONDS}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if field_selector:
                kwargs["field_selector"] = field_selector
            if token:
                kwargs["_continue"] = token
            if kind in CUSTOM:
                group, version, plural = CUSTOM[kind]
                if namespace:
                    page = self._custom.list_namespaced_custom_object(group, version, namespace, plural, **kwargs)
                else:
                    page = self._custom.list_cluster_custom_object(group, version, plural, **kwargs)
                page_items = page.get("items") or []
                token = (page.get("metadata") or {}).get("continue")
            else:
                _, list_ns, list_all = self._typed(kind)
                page = list_ns(namespace, **kwargs) if namespace else list_all(**kwargs)
                page_items = [self._dict(i) for i in page.items or []]
                meta = page.metadata
                token = (getattr(meta, "_continue", None) or getattr(meta, "var_continue", None)) if meta else None
            items.extend(page_items)
            if not token or len(items) >= max_items:
                return items[:max_items]

    def _patch_sync(self, kind, name, namespace, body, content_type, dry_run) -> dict:
        kwargs = {
            "field_manager": FIELD_MANAGER,
            "_content_type": content_type,
            "_request_timeout": _TIMEOUT_SECONDS,
        }
        if dry_run:
            kwargs["dry_run"] = "All"
        if kind == "Scale":
            return self._dict(self._apps.patch_namespaced_deployment_scale(name, namespace, body, **kwargs))
        if kind == "Deployment":
            return self._dict(self._apps.patch_namespaced_deployment(name, namespace, body, **kwargs))
        if kind == "HorizontalPodAutoscaler":
            return self._dict(
                self._autoscaling.patch_namespaced_horizontal_pod_autoscaler(name, namespace, body, **kwargs)
            )
        if kind == "ScaledObject":
            group, version, plural = CUSTOM[kind]
            return self._custom.patch_namespaced_custom_object(group, version, namespace, plural, name, body, **kwargs)
        raise ValueError(f"patching {kind} is not supported")

    def _log_sync(self, namespace, pod, container, previous, tail_lines, limit_bytes) -> str:
        kwargs = {"tail_lines": tail_lines, "limit_bytes": limit_bytes, "_request_timeout": _TIMEOUT_SECONDS}
        if container:
            kwargs["container"] = container
        if previous:
            kwargs["previous"] = True
        return self._core.read_namespaced_pod_log(pod, namespace, **kwargs)

    # ------------------------------------------------------------ async API

    async def get(self, kind: str, name: str, namespace: str | None = None) -> dict:
        return await asyncio.to_thread(self._get_sync, kind, name, namespace)

    async def list(self, kind: str, namespace=None, label_selector=None, field_selector=None, max_items=5000) -> list[dict]:
        return await asyncio.to_thread(self._list_sync, kind, namespace, label_selector, field_selector, max_items)

    async def patch(self, kind: str, name: str, namespace: str, body: dict, content_type: str, dry_run: bool) -> dict:
        return await asyncio.to_thread(self._patch_sync, kind, name, namespace, body, content_type, dry_run)

    async def pod_log(self, namespace, pod, container=None, previous=True, tail_lines=40, limit_bytes=8000) -> str:
        return await asyncio.to_thread(self._log_sync, namespace, pod, container, previous, tail_lines, limit_bytes)


_lock = threading.Lock()
_kube: Kube | None = None


def get_client() -> Kube:
    global _kube
    with _lock:
        if _kube is None:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                raise RuntimeError(
                    "No in-cluster ServiceAccount credentials found. This agent only runs inside the Kubernetes "
                    "cluster it manages, with automountServiceAccountToken enabled on its worker; there is no "
                    "kubeconfig fallback."
                ) from None
            _kube = Kube(k8s.ApiClient())
        return _kube


def api_status(exc: Exception) -> int | None:
    return exc.status if isinstance(exc, ApiException) else None


def safe_error(exc: Exception) -> str:
    """An API/transport error as text that is safe to return: the server's
    message can echo object content, so it is scrubbed and bounded."""
    if isinstance(exc, ApiException):
        message = exc.reason or ""
        try:
            message = json.loads(exc.body or "{}").get("message") or message
        except (TypeError, ValueError):
            pass
        text = f"HTTP {exc.status} {exc.reason or ''}: {message}".strip()
    else:
        text = f"{type(exc).__name__}: {exc}"
    return bounded(redact_text(text), 800)
