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
A kind name resolves to its REST path through `catalog.py`, so there is no
per-kind dispatch table here: one generic request path serves every kind,
subresource (`scale`, `eviction`, `resize`) and verb.
"""

import asyncio
import json
import os
import threading

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from . import catalog
from .catalog import Kind
from .redact import bounded, redact_text

FIELD_MANAGER = "ari-k8s-agent"
_TIMEOUT_SECONDS = float(os.getenv("K8S_API_TIMEOUT_SECONDS", "15"))
_PAGE_SIZE = 500

PATCH_MERGE = "application/merge-patch+json"
PATCH_STRATEGIC = "application/strategic-merge-patch+json"
# RFC 6902: replaces a whole subtree (a rollback's pod template) and can `test` a value first.
PATCH_JSON = "application/json-patch+json"

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


def _kind(kind: str | Kind) -> Kind:
    return kind if isinstance(kind, Kind) else catalog.get(kind)


class Kube:
    """Async, dict-in/dict-out access to the Kubernetes API by kind name (or a `catalog.Kind` for a custom
    resource found at run time). Verbs: get, list, patch, delete, create; plus pod_log."""

    def __init__(self, api_client: k8s.ApiClient):
        self._api = api_client
        self._core = k8s.CoreV1Api(api_client)

    # ------------------------------------------------------------ one request path

    def _request(self, method: str, path: str, query: list | None = None, body=None, content_type: str | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type or "application/json"
        method, url, headers, body, _ = self._api.param_serialize(
            method=method,
            resource_path=path,
            query_params=query or [],
            header_params=headers,
            body=body,
            auth_settings=["BearerToken"],
        )
        response = self._api.call_api(method, url, header_params=headers, body=body, _request_timeout=_TIMEOUT_SECONDS)
        response.read()
        text = response.data.decode("utf-8") if response.data else ""
        if not 200 <= response.status <= 299:
            raise ApiException.from_response(http_resp=response, body=text, data=None)
        return json.loads(text) if text else {}

    @staticmethod
    def _write_query(dry_run: bool, field_manager: bool = True) -> list:
        query = [("fieldManager", FIELD_MANAGER)] if field_manager else []
        if dry_run:
            query.append(("dryRun", "All"))
        return query

    # ------------------------------------------------------------ sync verbs

    def _get_sync(self, kind, name, namespace) -> dict:
        k = _kind(kind)
        return self._request("GET", k.path(namespace, name))

    def _list_sync(self, kind, namespace, label_selector, field_selector, max_items) -> list[dict]:
        k = _kind(kind)
        items: list[dict] = []
        token = None
        while True:
            query = [("limit", _PAGE_SIZE)]
            if label_selector:
                query.append(("labelSelector", label_selector))
            if field_selector:
                query.append(("fieldSelector", field_selector))
            if token:
                query.append(("continue", token))
            page = self._request("GET", k.path(namespace if k.namespaced else None), query)
            items.extend(page.get("items") or [])
            token = (page.get("metadata") or {}).get("continue")
            if not token or len(items) >= max_items:
                return items[:max_items]

    def _patch_sync(self, kind, name, namespace, body, content_type, dry_run, subresource) -> dict:
        k = _kind(kind)
        return self._request("PATCH", k.path(namespace, name, subresource), self._write_query(dry_run), body, content_type)

    def _delete_sync(self, kind, name, namespace, dry_run, grace_period_seconds, preconditions) -> dict:
        k = _kind(kind)
        # Every option goes in the DeleteOptions body, dryRun included. With a body present the API server
        # ignores the query string, so `?dryRun=All` beside a body deletes for real (found live on test,
        # 2026-11-24); kubectl's --dry-run=server sends it in the body too.
        body: dict = {"kind": "DeleteOptions", "apiVersion": "v1", "propagationPolicy": "Background"}
        if dry_run:
            body["dryRun"] = ["All"]
        if grace_period_seconds is not None:
            body["gracePeriodSeconds"] = int(grace_period_seconds)
        if preconditions:
            # The API server deletes only the object with this uid: a pod name a StatefulSet has already
            # reused for a new pod answers 409 instead of deleting the new one.
            body["preconditions"] = dict(preconditions)
        return self._request("DELETE", k.path(namespace, name), [], body)

    def _create_sync(self, kind, namespace, body, dry_run, name, subresource) -> dict:
        k = _kind(kind)
        # An eviction is a create on the pod's subresource; fieldManager is not a parameter there. A plain
        # create POSTs to the collection (the name is in the body): `.../jobs/<name>` would answer 405.
        query = self._write_query(dry_run, field_manager=subresource is None)
        return self._request("POST", k.path(namespace, name if subresource else None, subresource), query, body)

    # Fixed paths only: neither takes a value that could become part of a request path.
    def _server_version_sync(self) -> dict:
        return self._request("GET", "/version")

    def _core_resources_sync(self) -> list[str]:
        """Resource names the API server serves under /api/v1, subresources included ("pods/resize")."""
        return [r.get("name") for r in self._request("GET", "/api/v1").get("resources") or [] if r.get("name")]

    def _log_sync(self, namespace, pod, container, previous, tail_lines, limit_bytes, since_seconds) -> str:
        kwargs = {"tail_lines": tail_lines, "limit_bytes": limit_bytes, "_request_timeout": _TIMEOUT_SECONDS}
        if container:
            kwargs["container"] = container
        if previous:
            kwargs["previous"] = True
        if since_seconds:
            kwargs["since_seconds"] = int(since_seconds)
        return self._core.read_namespaced_pod_log(pod, namespace, **kwargs)

    # ------------------------------------------------------------ async API

    async def get(self, kind, name: str, namespace: str | None = None) -> dict:
        return await asyncio.to_thread(self._get_sync, kind, name, namespace)

    async def list(self, kind, namespace=None, label_selector=None, field_selector=None, max_items=5000) -> list[dict]:
        return await asyncio.to_thread(self._list_sync, kind, namespace, label_selector, field_selector, max_items)

    async def patch(self, kind, name: str, namespace: str | None, body: dict, content_type: str, dry_run: bool, subresource: str | None = None) -> dict:
        return await asyncio.to_thread(self._patch_sync, kind, name, namespace, body, content_type, dry_run, subresource)

    async def delete(
        self, kind, name: str, namespace: str | None, dry_run: bool, grace_period_seconds: int | None = None, preconditions: dict | None = None
    ) -> dict:
        return await asyncio.to_thread(self._delete_sync, kind, name, namespace, dry_run, grace_period_seconds, preconditions)

    async def create(self, kind, namespace: str | None, body: dict, dry_run: bool, name: str | None = None, subresource: str | None = None) -> dict:
        return await asyncio.to_thread(self._create_sync, kind, namespace, body, dry_run, name, subresource)

    async def pod_log(self, namespace, pod, container=None, previous=True, tail_lines=40, limit_bytes=8000, since_seconds=None) -> str:
        return await asyncio.to_thread(self._log_sync, namespace, pod, container, previous, tail_lines, limit_bytes, since_seconds)

    async def server_version(self) -> dict:
        return await asyncio.to_thread(self._server_version_sync)

    # The annotation is a string: inside this class `list` is the method above, so a bare `list[str]` fails
    # at import on Python 3.12, which evaluates annotations eagerly (the worker image's version).
    async def core_resources(self) -> "list[str]":
        return await asyncio.to_thread(self._core_resources_sync)


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
