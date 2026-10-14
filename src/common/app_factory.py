import httpx
import structlog
from fastapi import FastAPI

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.server.tasks import (
    BasePushNotificationSender,
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
)

from common.a2a_server.agent_card import build_agent_card
from common.a2a_server.auth import ApiKeyCallContextBuilder
from common.a2a_server.executor import AriAgentExecutor
from common.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(settings.log_level.upper()))

    agent_card = build_agent_card(settings)

    # In-memory by design, not an oversight: this agent stores no request
    # state of its own (see docs/design.md) — the TaskStore only gives
    # `tasks/get` polling and best-effort per-task history something to
    # read within this process's lifetime, and the push-notification
    # config store only needs to survive for that same lifetime (a
    # restart means callers re-register, same as they'd re-submit a lost
    # request against any stateless service).
    task_store = InMemoryTaskStore()
    push_config_store = InMemoryPushNotificationConfigStore()
    push_sender = BasePushNotificationSender(
        # The A2A SDK owns the send loop here, so `common.http_retry`
        # can't wrap the call — but the transport can still absorb the
        # failure mode that matters for a webhook to a caller's endpoint:
        # a connection reset or refused connect while that caller is
        # rolling its pods. `retries` covers connection establishment
        # only, not a 5xx, which is the most this layer can do without
        # re-implementing the SDK's sender.
        httpx_client=httpx.AsyncClient(timeout=30, transport=httpx.AsyncHTTPTransport(retries=3)),
        config_store=push_config_store,
    )

    request_handler = DefaultRequestHandler(
        agent_executor=AriAgentExecutor(),
        task_store=task_store,
        agent_card=agent_card,
        push_config_store=push_config_store,
        push_sender=push_sender,
    )

    context_builder = ApiKeyCallContextBuilder()

    app = FastAPI(
        title=settings.agent_name,
        description=agent_card.description,
        version="0.1.0",
    )

    # JSON-RPC and REST both mounted, per the scope decided in
    # docs/design.md: full A2A spec compliance for these two transports.
    # gRPC needs its own server process/port and protobuf codegen
    # dependencies — a deliberate, separate follow-up phase, not bundled
    # in here (see docs/design.md's transport section).
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url="/", context_builder=context_builder),
        rest_routes=create_rest_routes(request_handler, context_builder=context_builder),
    )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app
