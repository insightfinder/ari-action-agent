import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_oncall_agent.tools.github_rest import create_pull_request  # noqa: E402

_RealAsyncClient = httpx.AsyncClient


def _mock_async_client_factory(handler):
    """Returns a stand-in for `httpx.AsyncClient` that always talks to
    `handler` instead of the real network — captures the real class first
    so the stand-in doesn't recursively call itself once monkeypatched
    over `httpx.AsyncClient` (the module attribute `github_rest.httpx` is
    the same shared `httpx` module, not a separate copy)."""

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


@pytest.mark.asyncio
async def test_create_pull_request_posts_the_expected_payload_and_auth(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = request.read()
        return httpx.Response(201, json={"html_url": "https://github.com/x/y/pull/5"})

    monkeypatch.setattr(
        "ari_oncall_agent.tools.github_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    pr = await create_pull_request(
        {"url": "https://github.com/x/y", "pat": "ghp_secret"},
        "x/y",
        "Fix the bug",
        "Because it was broken.",
        "ari/fix-1",
        "main",
    )

    assert pr == {"url": "https://github.com/x/y/pull/5", "branch": "ari/fix-1"}
    assert captured["url"] == "https://api.github.com/repos/x/y/pulls"
    assert captured["headers"]["authorization"] == "Bearer ghp_secret"
    body = json.loads(captured["body"])
    assert body == {"title": "Fix the bug", "body": "Because it was broken.", "head": "ari/fix-1", "base": "main"}


@pytest.mark.asyncio
async def test_create_pull_request_raises_with_response_body_on_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"message": "Validation Failed", "errors": [{"message": "A pull request already exists"}]}
        )

    monkeypatch.setattr(
        "ari_oncall_agent.tools.github_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    with pytest.raises(RuntimeError, match="422"):
        await create_pull_request({"url": "https://github.com/x/y", "pat": "ghp_x"}, "x/y", "t", "b", "h", "main")


@pytest.mark.asyncio
async def test_create_pull_request_requires_a_pat():
    with pytest.raises(RuntimeError, match="requires repo.pat"):
        await create_pull_request({"url": "https://github.com/x/y"}, "x/y", "t", "b", "h", "main")
