"""One retry policy for every outbound HTTP call this service makes.

Before this, each REST client set `timeout=30` on its `httpx.AsyncClient`
and nothing else: a single connection reset, a GitHub 502 or a rate-limit
429 propagated straight out as a failed capability. The only retry in the
system was Temporal's, which re-runs the *whole capability* — so one
transient 502 from `POST /pulls` re-ran `git add` + `git commit` +
`git push` as well, and in `publish_pr` it did worse than that: every
failure inside its one big `try` drops to the opencode fallback (an extra
subprocess and several LLM-mediated tool calls, measured as the dominant
cost of that step), so a blip that one retried POST would have absorbed
instead bought an LLM round trip.

Retrying is only safe when the request is one we can afford to repeat, so
this deliberately does *not* retry everything:

- Transport-level failures (connect/read timeouts, resets) and the status
  codes in `RETRYABLE_STATUS` are retried. These say "the server did not
  process this, or could not tell us whether it did".
- Every other 4xx — 401, 403, 404, 422 — returns immediately. They are
  verdicts, not weather; retrying burns the budget and delays the real
  error reaching the caller.

`Retry-After` wins over our own backoff when the server sends it, because
the server knows its own rate-limit window and we do not (GitHub sends it
on 429 and on secondary rate limits). It is capped anyway, so a server
asking us to sleep for an hour cannot park a Temporal activity until its
`start_to_close_timeout` fires.

This returns the response rather than raising on a final non-2xx: callers
already produce error messages with their own API's context in them (see
`github_rest.create_pull_request`), and this layer has nothing better to
say than they do. It raises only when there is no response at all.
"""

import asyncio
import email.utils
import os
import random
from datetime import datetime, timezone

import httpx

# 3 attempts = the original call plus two retries. Enough to ride out a
# single bad upstream instance or one rate-limit window; short enough that
# the worst case still fits inside a capability's own timeout.
_MAX_ATTEMPTS = int(os.getenv("HTTP_MAX_ATTEMPTS", "3"))
_BACKOFF_BASE_SECONDS = float(os.getenv("HTTP_BACKOFF_BASE_SECONDS", "1"))
_BACKOFF_CAP_SECONDS = float(os.getenv("HTTP_BACKOFF_CAP_SECONDS", "20"))
# A server-supplied Retry-After is honoured up to this much. GitHub's
# secondary rate limit has been known to ask for minutes.
_RETRY_AFTER_CAP_SECONDS = float(os.getenv("HTTP_RETRY_AFTER_CAP_SECONDS", "60"))

# 408/425 are included for completeness; 429 and the 5xx family are the
# ones actually seen from GitHub and Jenkins. 501 and 505 are absent on
# purpose — "not implemented" does not become implemented on a retry.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def retry_after_seconds(response: httpx.Response) -> float | None:
    """`Retry-After` per RFC 9110: either delta-seconds or an HTTP-date.

    Returns None when the header is absent or unparseable — a malformed
    header must fall back to our own backoff, never abort the retry.
    """
    raw = (response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None

    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    # An HTTP-date without a zone is UTC by definition (RFC 9110 §5.6.7).
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def backoff_seconds(attempt: int) -> float:
    """Exponential backoff with equal jitter, capped.

    `attempt` is 1-based. Jitter matters more than it looks: the worker
    runs several capabilities concurrently against the same GitHub org, so
    un-jittered backoff would line their retries up into one burst against
    an API that is already rate-limiting us.
    """
    window = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    return window / 2 + random.uniform(0, window / 2)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    what: str,
    max_attempts: int | None = None,
    **kwargs,
) -> httpx.Response:
    """`client.request()` retried on transport errors and `RETRYABLE_STATUS`.

    `what` names the operation for the error message, the same way
    `git_ops._communicate` does — a bare `ConnectTimeout` in a Temporal
    failure tells nobody which API gave up.
    """
    attempts = max_attempts or _MAX_ATTEMPTS
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            # No response at all: connect/read timeout, reset, DNS. The
            # request may or may not have reached the server, which is why
            # callers of non-idempotent endpoints pair this with their own
            # idempotency check (see github_rest.create_pull_request).
            last_error = exc
            if attempt == attempts:
                raise RuntimeError(
                    f"{what} failed after {attempts} attempt(s): {type(exc).__name__}: {exc}"
                ) from exc
            await asyncio.sleep(backoff_seconds(attempt))
            continue

        if response.status_code not in RETRYABLE_STATUS or attempt == attempts:
            return response

        delay = retry_after_seconds(response)
        if delay is None:
            delay = backoff_seconds(attempt)
        else:
            delay = min(delay, _RETRY_AFTER_CAP_SECONDS)
        await asyncio.sleep(delay)

    # Unreachable: the loop either returns or raises on its last attempt.
    raise RuntimeError(f"{what} failed after {attempts} attempt(s)") from last_error
