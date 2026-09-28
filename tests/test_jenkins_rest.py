import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jenkins_agent.tools.jenkins_rest import job_url, stop_build  # noqa: E402

_RealAsyncClient = httpx.AsyncClient

_JENKINS = {
    "base_url": "https://jenkins.test/",
    "auth": {"type": "api_token", "user": "svc", "token": "tok"},
}


def _mock_async_client_factory(handler):
    def factory(**kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("common.http_retry.asyncio.sleep", no_sleep)


def test_job_url_uses_jenkins_nested_job_convention():
    assert job_url("https://jenkins.test/", "folder/sub/build-it") == (
        "https://jenkins.test/job/folder/job/sub/job/build-it"
    )


@pytest.mark.asyncio
async def test_stop_build_attaches_the_crumb_and_posts_to_the_build(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), dict(request.headers)))
        if request.method == "GET":
            return httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "abc123"})
        return httpx.Response(200)

    monkeypatch.setattr(
        "jenkins_agent.tools.jenkins_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    await stop_build(_JENKINS, "deploy/checkout-service", 42)

    method, url, headers = seen[-1]
    assert method == "POST"
    assert url == "https://jenkins.test/job/deploy/job/checkout-service/42/stop"
    assert headers["jenkins-crumb"] == "abc123"
    assert headers["authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_a_flaky_crumb_issuer_is_retried_rather_than_losing_the_crumb(monkeypatch):
    attempts = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            attempts["get"] += 1
            if attempts["get"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "late"})
        assert request.headers["jenkins-crumb"] == "late"
        return httpx.Response(200)

    monkeypatch.setattr(
        "jenkins_agent.tools.jenkins_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    await stop_build(_JENKINS, "job", 1)

    assert attempts["get"] == 2


@pytest.mark.asyncio
async def test_a_transient_502_on_stop_is_retried(monkeypatch):
    attempts = {"post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)  # no crumb issuer; harmless
        attempts["post"] += 1
        return httpx.Response(502) if attempts["post"] == 1 else httpx.Response(200)

    monkeypatch.setattr(
        "jenkins_agent.tools.jenkins_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    await stop_build(_JENKINS, "job", 1)

    assert attempts["post"] == 2


@pytest.mark.asyncio
async def test_a_persistent_failure_still_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404) if request.method == "GET" else httpx.Response(500)

    monkeypatch.setattr(
        "jenkins_agent.tools.jenkins_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    with pytest.raises(httpx.HTTPStatusError):
        await stop_build(_JENKINS, "job", 1)
