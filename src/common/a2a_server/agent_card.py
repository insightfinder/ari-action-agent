"""Builds this agent's real, wire-compliant Agent Card — the actual
`a2a.types.AgentCard` protobuf message the official `a2a-sdk` serializes at
`/.well-known/agent-card.json` (confirmed the exact path via
`a2a.utils.AGENT_CARD_WELL_KNOWN_PATH`) and serves to any genuine A2A
client. This replaces an earlier vendored `a2a/types.py`/`agent_card.py`
pair that only mimicked A2A's vocabulary (same field names, incompatible
wire format) — see docs/MEMORY.md for why that wasn't actually
interoperable and what changed.
"""

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    APIKeySecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils import TransportProtocol

from common.agent_meta import get_agent_meta
from common.settings import Settings

_API_KEY_SCHEME_NAME = "apiKeyAuth"


def build_agent_card(settings: Settings) -> AgentCard:
    meta = get_agent_meta(settings.agent_kind)
    return AgentCard(
        name=settings.agent_name,
        description=meta.description,
        version="0.1.0",
        # Both JSON-RPC and REST are served from the same URL (FastAPI
        # dispatches by path/content-type — see main.py); gRPC is a
        # deliberately separate, later phase (needs its own server
        # process/port — see docs/design.md).
        supported_interfaces=[
            AgentInterface(
                url=settings.agent_public_url,
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version="1.0",
            ),
            AgentInterface(
                url=settings.agent_public_url,
                protocol_binding=TransportProtocol.HTTP_JSON,
                protocol_version="1.0",
            ),
        ],
        capabilities=AgentCapabilities(
            streaming=False,  # message/stream (SSE) not implemented — every request is a Task either way
            push_notifications=True,  # tasks/pushNotificationConfig/* — real, SDK-backed (see a2a_server/wiring.py)
        ),
        security_schemes={
            _API_KEY_SCHEME_NAME: SecurityScheme(
                api_key_security_scheme=APIKeySecurityScheme(
                    location="header",
                    name="x-api-key",
                    description="One of the keys configured in this agent's AGENT_API_KEYS.",
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={_API_KEY_SCHEME_NAME: StringList(list=[])})],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=[meta.skill],
    )
