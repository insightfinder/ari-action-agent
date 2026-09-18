"""`x-api-key` auth, wired in via the SDK's own extension point
(`ServerCallContextBuilder` — see `a2a.server.routes.common`), not a
FastAPI dependency, since the SDK's routes aren't declared with `Depends`.
Raising a Starlette `HTTPException` here still produces a proper 401, not
a generic 500: `add_a2a_routes_to_fastapi` re-registers the SDK's routes
as real `APIRoute`s on our own FastAPI app (see main.py), so they share
its default exception-handling middleware like any other route.
"""

from fastapi import HTTPException
from starlette.requests import Request

from a2a.server.context import ServerCallContext
from a2a.server.routes.common import DefaultServerCallContextBuilder, ServerCallContextBuilder

from ..settings import get_settings

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


class ApiKeyCallContextBuilder(ServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        settings = get_settings()
        api_key = request.headers.get("x-api-key")
        if not api_key or api_key not in settings.api_key_set:
            raise HTTPException(status_code=401, detail="Invalid or missing x-api-key")
        return _default_builder.build(request)
