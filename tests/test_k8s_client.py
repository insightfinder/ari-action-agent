"""The real `Kube` client over a stubbed HTTP transport.

Every other k8s test replaces `Kube` whole with `k8s_fixtures.FakeClient`, so this is the only place the
request path itself runs: REST paths from the catalog, query parameters (dry run, field manager, paging),
patch content types, and how a non-2xx answer becomes an `ApiException` carrying `.status` — which
`api_status`, the read retries, the KEDA-not-installed 404 and the RBAC-403-as-gap rule all depend on.
"""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kubernetes import client as k8s  # noqa: E402
from kubernetes.client import rest  # noqa: E402

from k8s_agent.tools import catalog, client  # noqa: E402

pytestmark = pytest.mark.asyncio


class _Raw:
    def __init__(self, status, payload, reason="OK"):
        self.status = status
        self.reason = reason
        self.data = json.dumps(payload).encode() if payload is not None else b""
        self.headers = {"content-type": "application/json"}


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, body=None, post_params=None, _request_timeout=None):
        parts = urlsplit(url)
        self.calls.append(
            {"method": method, "path": parts.path, "query": parse_qs(parts.query), "headers": headers or {}, "body": body}
        )
        status, payload = self.responses.pop(0) if self.responses else (200, {})
        return rest.RESTResponse(_Raw(status, payload, "OK" if status < 300 else "Forbidden"))


@pytest.fixture
def kube():
    config = k8s.Configuration(host="https://k8s.test")
    config.api_key = {"authorization": "test-token"}
    config.api_key_prefix = {"authorization": "Bearer"}
    api = k8s.ApiClient(config)
    transport = Transport()
    api.rest_client = transport
    return client.Kube(api), transport


async def test_get_namespaced_and_cluster_scoped_paths(kube):
    kube_, t = kube
    t.responses = [(200, {"metadata": {"name": "checkout"}}), (200, {"metadata": {"name": "node-a"}})]
    assert (await kube_.get("Deployment", "checkout", "shop"))["metadata"]["name"] == "checkout"
    await kube_.get("Node", "node-a")
    assert [c["path"] for c in t.calls] == ["/apis/apps/v1/namespaces/shop/deployments/checkout", "/api/v1/nodes/node-a"]
    assert t.calls[0]["headers"]["authorization"] == "Bearer test-token"


async def test_list_follows_continue_tokens_and_passes_selectors(kube):
    kube_, t = kube
    t.responses = [
        (200, {"items": [{"n": 1}], "metadata": {"continue": "tok-1"}}),
        (200, {"items": [{"n": 2}], "metadata": {}}),
    ]
    items = await kube_.list("Pod", "shop", "app=checkout", "status.phase=Running")
    assert items == [{"n": 1}, {"n": 2}]
    first, second = t.calls
    assert first["path"] == "/api/v1/namespaces/shop/pods"
    assert first["query"]["labelSelector"] == ["app=checkout"]
    assert first["query"]["fieldSelector"] == ["status.phase=Running"]
    assert "continue" not in first["query"] and second["query"]["continue"] == ["tok-1"]


async def test_list_all_namespaces_and_cluster_scoped_kind_ignores_namespace(kube):
    kube_, t = kube
    t.responses = [(200, {"items": []}), (200, {"items": []})]
    await kube_.list("Event")
    await kube_.list("Node", "shop")
    assert [c["path"] for c in t.calls] == ["/api/v1/events", "/api/v1/nodes"]


async def test_patch_scale_subresource_with_dry_run_and_content_type(kube):
    kube_, t = kube
    t.responses = [(200, {"spec": {"replicas": 4}})]
    await kube_.patch("Deployment", "checkout", "shop", {"spec": {"replicas": 4}}, client.PATCH_MERGE, True, "scale")
    call = t.calls[0]
    assert call["method"] == "PATCH"
    assert call["path"] == "/apis/apps/v1/namespaces/shop/deployments/checkout/scale"
    assert call["query"]["dryRun"] == ["All"] and call["query"]["fieldManager"] == [client.FIELD_MANAGER]
    assert call["headers"]["Content-Type"] == client.PATCH_MERGE


async def test_real_patch_has_no_dry_run(kube):
    kube_, t = kube
    await kube_.patch("StatefulSet", "db", "shop", {"spec": {"replicas": 2}}, client.PATCH_MERGE, False, "scale")
    assert "dryRun" not in t.calls[0]["query"]
    assert t.calls[0]["path"] == "/apis/apps/v1/namespaces/shop/statefulsets/db/scale"


async def test_pod_resize_is_a_strategic_patch_on_the_resize_subresource(kube):
    kube_, t = kube
    body = {"spec": {"containers": [{"name": "app", "resources": {"limits": {"memory": "4Gi"}}}]}}
    await kube_.patch("Pod", "checkout-1", "shop", body, client.PATCH_STRATEGIC, True, "resize")
    call = t.calls[0]
    assert call["method"] == "PATCH" and call["path"] == "/api/v1/namespaces/shop/pods/checkout-1/resize"
    assert call["headers"]["Content-Type"] == client.PATCH_STRATEGIC
    assert call["query"]["dryRun"] == ["All"] and call["query"]["fieldManager"] == [client.FIELD_MANAGER]


async def test_server_version_and_core_resources_read_fixed_paths(kube):
    kube_, t = kube
    t.responses = [
        (200, {"major": "1", "minor": "36", "gitVersion": "v1.36.5-eks-91f89a9"}),
        (200, {"kind": "APIResourceList", "resources": [{"name": "pods"}, {"name": "pods/resize"}, {"name": "nodes"}]}),
    ]
    assert (await kube_.server_version())["gitVersion"] == "v1.36.5-eks-91f89a9"
    assert await kube_.core_resources() == ["pods", "pods/resize", "nodes"]
    assert [c["path"] for c in t.calls] == ["/version", "/api/v1"]


async def test_eviction_is_a_create_on_the_pod_subresource(kube):
    kube_, t = kube
    body = {"apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": "p1", "namespace": "shop"}}
    await kube_.create("Pod", "shop", body, True, name="p1", subresource="eviction")
    call = t.calls[0]
    assert call["method"] == "POST" and call["path"] == "/api/v1/namespaces/shop/pods/p1/eviction"
    assert call["query"]["dryRun"] == ["All"] and "fieldManager" not in call["query"]


async def test_delete_is_background_propagation(kube):
    kube_, t = kube
    await kube_.delete("Pod", "p1", "shop", False)
    call = t.calls[0]
    assert call["method"] == "DELETE" and call["path"] == "/api/v1/namespaces/shop/pods/p1"
    assert call["body"] == {"propagationPolicy": "Background"}


@pytest.mark.parametrize("status", [403, 404, 409, 500])
async def test_non_2xx_raises_api_exception_with_status(kube, status):
    kube_, t = kube
    t.responses = [(status, {"kind": "Status", "message": f"deployments.apps is forbidden ({status})"})]
    with pytest.raises(Exception) as caught:
        await kube_.get("Deployment", "checkout", "shop")
    assert client.api_status(caught.value) == status
    assert f"({status})" in client.safe_error(caught.value)


@pytest.mark.parametrize(
    "namespace,name",
    [("shop", "../../secrets/db"), ("shop", "x?dryRun=None"), ("../kube-system", "a"), ("shop", "a/b"), ("shop", "..")],
)
async def test_path_segments_are_validated_before_any_request(kube, namespace, name):
    kube_, t = kube
    with pytest.raises(ValueError):
        await kube_.get("Pod", name, namespace)
    assert t.calls == []


async def test_undeclared_subresource_is_refused(kube):
    kube_, t = kube
    with pytest.raises(ValueError):
        await kube_.patch("DaemonSet", "fluentd", "logging", {}, client.PATCH_MERGE, True, "scale")
    with pytest.raises(ValueError):
        catalog.custom("evil/../x", "v1", "things", "Thing", True)
    assert t.calls == []


async def test_custom_kind_from_a_crd(kube):
    kube_, t = kube
    kind = catalog.custom("cert-manager.io", "v1", "certificates", "Certificate", True)
    await kube_.list(kind, "shop")
    assert t.calls[0]["path"] == "/apis/cert-manager.io/v1/namespaces/shop/certificates"
