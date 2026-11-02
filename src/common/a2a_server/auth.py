"""`x-api-key` auth, enforced twice.

`ApiKeyMiddleware` answers first, before any route runs, and is what gives
a wrong or missing key its 401. It has to sit in front of the routes: the
SDK wraps every REST handler in `rest_error_handler`, which turns any
exception that is not an `A2AError` — a Starlette `HTTPException`
included — into `500 unknown exception`. So a check inside the request
path could only ever produce a 401 on JSON-RPC, which has no such wrapper
(found 2026-10-16: `POST /message:send` with a bad key answered 500 while
`POST /` answered 401).

`ApiKeyCallContextBuilder`, the SDK's own extension point (`a2a.server.
routes.common`), keeps the same check inside the request path, so a route
the middleware was not told about still refuses to run without a key —
with a 500 on REST, which is wrong but not open.
"""

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match, Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from a2a.server.context import ServerCallContext
from a2a.server.routes.common import DefaultServerCallContextBuilder, ServerCallContextBuilder

from common.settings import get_settings

# `DefaultServerCallContextBuilder` isn't just boilerplate to skip: it
# populates `state['headers']`, which the SDK's own version-validation
# decorator reads (`context.state.get('headers', {})` in
# `a2a.utils.version_validator`) to check the `A2A-Version` request
# header. Building a bare `ServerCallContext()` here (as an earlier
# version of this file did) silently broke every request with
# "A2A version '0.3' is not supported" — found by actually calling the
# endpoint, not by reading the SDK source. Delegating to the default
# builder for everything except the actual key check avoids losing this
# again if the SDK's context ever grows another such field.
_default_builder = DefaultServerCallContextBuilder()


_UNAUTHORIZED = "Invalid or missing x-api-key"


def _authorized(api_key: str | None) -> bool:
    return bool(api_key) and api_key in get_settings().api_key_set


class ApiKeyCallContextBuilder(ServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        if not _authorized(request.headers.get("x-api-key")):
            raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
        return _default_builder.build(request)


def _matches(routes: list[BaseRoute], scope: Scope) -> bool:
    """Whether a request reaches one of `routes`. A `Mount` matches on its prefix alone, and the SDK
    mounts its REST routes again under `/{tenant}` — a prefix that `/.well-known/agent-card.json`
    also has — so a mount counts only when one of its own routes matches what is left of the path."""
    for route in routes:
        match, child = route.matches(scope)
        if match is Match.NONE:
            continue
        if not isinstance(route, Mount):
            return True
        if _matches(route.routes, {**scope, **child}):
            return True
    return False


class ApiKeyMiddleware:
    """Refuses a request for any of `protected` without a valid key, with a 401. Only those routes:
    the agent card, `/healthz` and FastAPI's own pages stay as open as they were. A pure ASGI
    middleware rather than `BaseHTTPMiddleware`, which buffers through a task group and is not
    something to put in front of `message:stream`'s server-sent events."""

    def __init__(self, app: ASGIApp, protected: list[BaseRoute]):
        self.app = app
        self.protected = list(protected)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _matches(self.protected, scope):
            if not _authorized(Request(scope).headers.get("x-api-key")):
                await JSONResponse({"detail": _UNAUTHORIZED}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)
