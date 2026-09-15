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
    AgentSkill,
    APIKeySecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils import TransportProtocol

from ..settings import Settings

_API_KEY_SCHEME_NAME = "apiKeyAuth"


def build_agent_card(settings: Settings) -> AgentCard:
    return AgentCard(
        name=settings.agent_name,
        description=(
            "On-call agent that takes an incident/prompt plus repo + LLM "
            "config, analyzes the problem in an isolated sandbox using "
            "opencode, generates and verifies a fix, and opens a pull "
            "request. Every request is independent and self-contained."
        ),
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
        skills=[
            AgentSkill(
                id="code_analysis_fix",
                name="Code Analysis & Fix",
                description=(
                    "Given a prompt describing a bug/incident (e.g. a stack "
                    "trace) and a repo config, clone the repo, analyze root "
                    "cause, generate + verify a fix, and open a PR. Also "
                    "answers plain questions directly when no repo action "
                    "is needed."
                ),
                tags=["code", "incident", "pull-request", "opencode"],
                examples=[
                    "null pointer exception in the code <stacktrace>, please analyse and create a fix",
                    "check the repo in the dev branch, fix the memory leak, and raise a PR against staging",
                    "same issue as before but skip tests and just open the PR",
                ],
            )
        ],
    )
