import asyncio
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from google.protobuf import json_format

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.server.tasks import BasePushNotificationSender, InMemoryPushNotificationConfigStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    APIKeySecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils import TransportProtocol

from common.a2a_server.auth import ApiKeyCallContextBuilder
from common.a2a_server.task_store import build_task_store
from common.settings import get_settings

from .executor import CoordinatorExecutor
from .reconciler import reconcile_unfinished_tasks
from .registry import Registry, load_registry
from .router import Complete
from .settings import CoordinatorSettings, get_coordinator_settings
from .store import RouteStore

logger = structlog.get_logger()

API_KEY_SCHEME = "apiKeyAuth"


def build_card(settings: CoordinatorSettings) -> AgentCard:
    return AgentCard(
        name=settings.coordinator_name,
        description=settings.coordinator_description,
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(url=settings.coordinator_public_url, protocol_binding=TransportProtocol.JSONRPC, protocol_version="1.0"),
            AgentInterface(url=settings.coordinator_public_url, protocol_binding=TransportProtocol.HTTP_JSON, protocol_version="1.0"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=True),
        security_schemes={
            API_KEY_SCHEME: SecurityScheme(
                api_key_security_scheme=APIKeySecurityScheme(
                    location="header", name="x-api-key", description="One of the keys configured in AGENT_API_KEYS."
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={API_KEY_SCHEME: StringList(list=[])})],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
    )


def refresh_card_skills(card: AgentCard, registry: Registry) -> None:
    skills = []
    seen: set[str] = set()
    for agent in registry.available():
        for raw in (agent.card or {}).get("skills") or []:
            if not isinstance(raw, dict):
                continue
            skill = AgentSkill()
            try:
                json_format.ParseDict(raw, skill, ignore_unknown_fields=True)
            except json_format.ParseError:
                continue
            skill.id = f"{agent.name}.{skill.id or skill.name or 'skill'}"
            if skill.id in seen:
                continue
            seen.add(skill.id)
            skills.append(skill)
    del card.skills[:]
    card.skills.extend(skills)


def create_app(
    *,
    registry: Registry | None = None,
    store: RouteStore | None = None,
    settings: CoordinatorSettings | None = None,
    complete: Complete | None = None,
    sleep=asyncio.sleep,
) -> FastAPI:
    common = get_settings()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(common.log_level.upper()))
    cs = settings or get_coordinator_settings()
    registry = registry or load_registry(
        cs.coordinator_agents_file or None,
        timeout=cs.coordinator_request_timeout_seconds,
        card_timeout=cs.coordinator_card_timeout_seconds,
    )
    store = store if store is not None else RouteStore(common.task_store_root)
    card = build_card(cs)
    task_store = build_task_store(common.task_store_root)
    push_config_store = InMemoryPushNotificationConfigStore()
    push_sender = BasePushNotificationSender(
        httpx_client=httpx.AsyncClient(timeout=30, transport=httpx.AsyncHTTPTransport(retries=3)),
        config_store=push_config_store,
    )
    executor = CoordinatorExecutor(registry, store, cs, complete=complete, sleep=sleep)
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=card,
        push_config_store=push_config_store,
        push_sender=push_sender,
    )

    async def refresh() -> None:
        await registry.refresh()
        refresh_card_skills(card, registry)

    async def refresh_forever() -> None:
        while True:
            await asyncio.sleep(cs.coordinator_card_refresh_seconds)
            try:
                await refresh()
            except Exception as exc:
                logger.warning("coordinator_refresh_failed", error=str(exc)[:300])

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await refresh()
        logger.info("coordinator_started", agents=registry.status(), default_agent=registry.default_agent)
        background = [asyncio.create_task(refresh_forever()), asyncio.create_task(reconcile_unfinished_tasks(task_store, executor))]
        try:
            yield
        finally:
            for task in background:
                task.cancel()

    app = FastAPI(title=cs.coordinator_name, description=cs.coordinator_description, version="0.1.0", lifespan=lifespan)
    app.state.registry = registry
    app.state.refresh = refresh
    app.state.executor = executor
    app.state.task_store = task_store
    context_builder = ApiKeyCallContextBuilder()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/", context_builder=context_builder),
        rest_routes=create_rest_routes(handler, context_builder=context_builder),
    )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "agents": registry.status()}

    return app
