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


# --- idempotency: opening a PR is the one call a retry could duplicate ---


def _routing_handler(*, existing, on_post):
    """Serves the pre-check GET from `existing` (a list, re-read on every
    call so a test can mutate it mid-flight) and the create POST from
    `on_post`, which is called with the attempt number."""
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=list(existing))
        posts["n"] += 1
        return on_post(posts["n"], request)

    handler.posts = posts
    return handler


@pytest.mark.asyncio
async def test_an_existing_open_pr_for_the_branch_is_returned_without_creating_another(monkeypatch):
    handler = _routing_handler(
        existing=[{"html_url": "https://github.com/x/y/pull/9"}],
        on_post=lambda n, r: pytest.fail("must not POST when the PR already exists"),
    )
    monkeypatch.setattr(
        "ari_oncall_agent.tools.github_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    pr = await create_pull_request(
        {"url": "https://github.com/x/y", "pat": "ghp_x"}, "x/y", "t", "b", "ari/fix-1", "main"
    )

    assert pr == {"url": "https://github.com/x/y/pull/9", "branch": "ari/fix-1"}
    assert handler.posts["n"] == 0


@pytest.mark.asyncio
async def test_the_precheck_asks_for_this_head_branch_only_and_open_prs_only(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            seen["url"] = str(request.url)
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"html_url": "https://github.com/x/y/pull/1"})

    monkeypatch.setattr(
        "ari_oncall_agent.tools.github_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    await create_pull_request(
        {"url": "https://github.com/x/y", "pat": "ghp_x"}, "x/y", "t", "b", "ari/fix-1", "main"
    )

    assert "head=x%3Aari%2Ffix-1" in seen["url"]
    assert "state=open" in seen["url"]


@pytest.mark.asyncio
async def test_a_422_after_the_pr_was_created_by_a_lost_attempt_returns_that_pr(monkeypatch):
    """The race the pre-check cannot close: a previous attempt's POST
    reached GitHub, its response was lost, so this attempt still saw no
    open PR. GitHub answers 422 — and the PR the caller wanted exists."""
    existing = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=list(existing))
        existing.append({"html_url": "https://github.com/x/y/pull/42"})
        return httpx.Response(422, json={"errors": [{"message": "A pull request already exists"}]})

    monkeypatch.setattr(
        "ari_oncall_agent.tools.github_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    pr = await create_pull_request(
        {"url": "https://github.com/x/y", "pat": "ghp_x"}, "x/y", "t", "b", "ari/fix-1", "main"
    )

    assert pr == {"url": "https://github.com/x/y/pull/42", "branch": "ari/fix-1"}


@pytest.mark.asyncio
async def test_a_422_with_no_pr_anywhere_still_raises(monkeypatch):
    handler = _routing_handler(
        existing=[],
        on_post=lambda n, r: httpx.Response(422, json={"message": "head sha cannot be blank"}),
    )
    monkeypatch.setattr(
        "ari_oncall_agent.tools.github_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    with pytest.raises(RuntimeError, match="422"):
        await create_pull_request(
            {"url": "https://github.com/x/y", "pat": "ghp_x"}, "x/y", "t", "b", "ari/fix-1", "main"
        )


@pytest.mark.asyncio
async def test_a_broken_precheck_never_blocks_creating_the_pr(monkeypatch):
    """The lookup exists to avoid a duplicate; it must never become the
    reason no PR gets opened at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(403, json={"message": "token lacks read scope"})
        return httpx.Response(201, json={"html_url": "https://github.com/x/y/pull/7"})

    monkeypatch.setattr(
        "ari_oncall_agent.tools.github_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    pr = await create_pull_request(
        {"url": "https://github.com/x/y", "pat": "ghp_x"}, "x/y", "t", "b", "ari/fix-1", "main"
    )
    assert pr["url"] == "https://github.com/x/y/pull/7"


@pytest.mark.asyncio
async def test_a_transient_502_on_create_is_retried_rather_than_failing_the_capability(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("common.http_retry.asyncio.sleep", no_sleep)

    handler = _routing_handler(
        existing=[],
        on_post=lambda n, r: httpx.Response(502, text="bad gateway")
        if n == 1
        else httpx.Response(201, json={"html_url": "https://github.com/x/y/pull/3"}),
    )
    monkeypatch.setattr(
        "ari_oncall_agent.tools.github_rest.httpx.AsyncClient", _mock_async_client_factory(handler)
    )

    pr = await create_pull_request(
        {"url": "https://github.com/x/y", "pat": "ghp_x"}, "x/y", "t", "b", "ari/fix-1", "main"
    )

    assert pr == {"url": "https://github.com/x/y/pull/3", "branch": "ari/fix-1"}
    assert handler.posts["n"] == 2
