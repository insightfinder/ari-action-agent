import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common import http_retry  # noqa: E402
from common.http_retry import (  # noqa: E402
    RETRYABLE_STATUS,
    backoff_seconds,
    request_with_retry,
    retry_after_seconds,
)


@pytest.fixture
def slept(monkeypatch):
    """Records every sleep instead of performing it, so the retry tests
    exercise the real delay arithmetic without spending the delay."""
    recorded: list[float] = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(http_retry.asyncio, "sleep", fake_sleep)
    return recorded


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- what gets retried -------------------------------------------------


@pytest.mark.asyncio
async def test_retries_a_transport_error_and_returns_the_eventual_success(slept):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        resp = await request_with_retry(client, "GET", "https://example.test/x", what="thing")

    assert resp.status_code == 200
    assert len(calls) == 3
    assert len(slept) == 2


@pytest.mark.asyncio
async def test_transport_error_on_every_attempt_raises_naming_the_operation(slept):
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    async with _client(handler) as client:
        with pytest.raises(RuntimeError, match=r"GitHub API create pull request failed after 3 attempt"):
            await request_with_retry(
                client, "POST", "https://example.test/x", what="GitHub API create pull request"
            )

    # The underlying httpx error is preserved for the traceback, not swallowed.
    assert len(slept) == 2


@pytest.mark.asyncio
async def test_retries_a_502_then_succeeds(slept):
    codes = iter([502, 503, 201])

    def handler(request):
        return httpx.Response(next(codes), json={})

    async with _client(handler) as client:
        resp = await request_with_retry(client, "POST", "https://example.test/x", what="thing")

    assert resp.status_code == 201
    assert len(slept) == 2


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
@pytest.mark.asyncio
async def test_every_declared_retryable_status_is_actually_retried(status, slept):
    seen = []

    def handler(request):
        seen.append(status)
        return httpx.Response(status if len(seen) == 1 else 200)

    async with _client(handler) as client:
        resp = await request_with_retry(client, "GET", "https://example.test/x", what="thing")

    assert resp.status_code == 200
    assert len(seen) == 2


# --- what does NOT get retried ----------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
@pytest.mark.asyncio
async def test_client_errors_are_verdicts_and_return_immediately(status, slept):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "no"})

    async with _client(handler) as client:
        resp = await request_with_retry(client, "POST", "https://example.test/x", what="thing")

    assert resp.status_code == status
    assert len(calls) == 1, "a verdict must not be retried"
    assert slept == []


@pytest.mark.asyncio
async def test_exhausted_retries_return_the_response_rather_than_raising(slept):
    """Callers own their own error text; this layer has nothing better to
    say, so a final 503 comes back as a response, not an exception."""

    def handler(request):
        return httpx.Response(503, text="upstream down")

    async with _client(handler) as client:
        resp = await request_with_retry(client, "GET", "https://example.test/x", what="thing")

    assert resp.status_code == 503
    assert resp.text == "upstream down"
    assert len(slept) == 2


@pytest.mark.asyncio
async def test_max_attempts_is_honoured(slept):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    async with _client(handler) as client:
        await request_with_retry(client, "GET", "https://example.test/x", what="thing", max_attempts=5)

    assert len(calls) == 5
    assert len(slept) == 4


# --- Retry-After -------------------------------------------------------


def test_retry_after_reads_delta_seconds():
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "17"})) == 17.0


def test_retry_after_reads_an_http_date():
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    seconds = retry_after_seconds(httpx.Response(429, headers={"Retry-After": format_datetime(when)}))
    assert 25 <= seconds <= 31


def test_retry_after_in_the_past_is_clamped_to_zero():
    when = datetime.now(timezone.utc) - timedelta(hours=1)
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": format_datetime(when)})) == 0.0


@pytest.mark.parametrize("raw", ["", "   ", "soon", "not-a-date", "-"])
def test_a_malformed_retry_after_falls_back_rather_than_blowing_up(raw):
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": raw})) is None


def test_no_retry_after_header_is_none():
    assert retry_after_seconds(httpx.Response(429)) is None


@pytest.mark.asyncio
async def test_retry_after_is_preferred_over_our_own_backoff(slept):
    codes = iter([429, 200])

    def handler(request):
        code = next(codes)
        return httpx.Response(code, headers={"Retry-After": "7"} if code == 429 else {})

    async with _client(handler) as client:
        await request_with_retry(client, "GET", "https://example.test/x", what="thing")

    assert slept == [7.0]


@pytest.mark.asyncio
async def test_an_absurd_retry_after_is_capped(slept, monkeypatch):
    monkeypatch.setattr(http_retry, "_RETRY_AFTER_CAP_SECONDS", 60.0)
    codes = iter([429, 200])

    def handler(request):
        code = next(codes)
        return httpx.Response(code, headers={"Retry-After": "3600"} if code == 429 else {})

    async with _client(handler) as client:
        await request_with_retry(client, "GET", "https://example.test/x", what="thing")

    assert slept == [60.0], "a server must not be able to park an activity past its timeout"


# --- backoff -----------------------------------------------------------


def test_backoff_grows_and_stays_within_its_cap():
    for attempt in range(1, 12):
        window = min(http_retry._BACKOFF_CAP_SECONDS, http_retry._BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        for _ in range(50):
            delay = backoff_seconds(attempt)
            assert window / 2 <= delay <= window, f"attempt {attempt} produced {delay}"


def test_backoff_is_jittered():
    """Un-jittered backoff would line concurrent capabilities' retries up
    into one burst against an API that is already rate-limiting us."""
    assert len({backoff_seconds(4) for _ in range(50)}) > 1


# --- source-level guards ----------------------------------------------
#
# The same shape as tests/test_git_ops.py's guard against a bare
# `communicate()` creeping back in: the point of this module is that
# *every* outbound call goes through it, and a new REST client added later
# is exactly how that stops being true.

_SRC = Path(__file__).resolve().parents[1] / "src"


def _source_lines(path):
    """Code only — a docstring or comment mentioning `client.post` is not
    a call site, and was what made the first version of this guard cry
    wolf on `http_retry`'s own module docstring."""
    inside_docstring = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.count('"""') == 1:
            inside_docstring = not inside_docstring
            continue
        if inside_docstring or stripped.startswith("#") or stripped.startswith('"""'):
            continue
        yield line


def test_every_httpx_client_in_the_tree_sets_a_timeout():
    offenders = [
        f"{path.relative_to(_SRC)}: {line.strip()}"
        for path in _SRC.rglob("*.py")
        for line in _source_lines(path)
        if "httpx.AsyncClient(" in line and "timeout=" not in line
    ]
    assert not offenders, "httpx defaults to no timeout at all:\n" + "\n".join(offenders)


def test_no_module_calls_httpx_directly_instead_of_going_through_this_one():
    verbs = (".get(", ".post(", ".put(", ".patch(", ".delete(", ".request(")
    offenders = [
        f"{path.relative_to(_SRC)}: {line.strip()}"
        for path in _SRC.rglob("*.py")
        if path.name != "http_retry.py"
        for line in _source_lines(path)
        if "await client" in line and any(verb in line for verb in verbs)
    ]
    assert not offenders, "these bypass the retry policy:\n" + "\n".join(offenders)
