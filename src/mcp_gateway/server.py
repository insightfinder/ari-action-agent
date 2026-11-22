import json
import logging
import time
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import NoBackChannelError
from mcp.types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from chat_gateway.a2a_client import AgentClient
from chat_gateway.bridge import Bridge
from chat_gateway.session import SessionStore

from .approval import APPROVAL_PATH, LINK_SECONDS, ApprovalPages
from .oauth import CONSENT_PATH, GITHUB_CALLBACK_PATH, AriOAuthProvider, GitHubLogin, StateFile
from .service import AriService, UserFacingError
from .settings import McpSettings

logger = logging.getLogger("mcp_gateway.server")

INSTRUCTIONS = """\
ARI is an operations agent that does real work: it finds the root cause of a problem in a code repository, \
prepares a fix on a branch, and opens a pull request; and it checks and changes Kubernetes workloads. \
Every change is proposed first and carried out only after a person approves it.

How to use it:
- Call ari_ask with the user's request in plain language. Keep the conversation_id it returns and pass it \
to every later call about the same work.
- A call returns within about half a minute. If the state is "working", call ari_status with the \
conversation_id until it is done or a proposal is waiting.
- When a proposal is waiting, show it to the user in full and let the user decide. Call ari_approve only \
when the user explicitly says to approve that proposal, and pass the proposal_id you were shown.
- Name a repository with the repo argument (https URL) when the request is about code; ref is the branch \
to work from and base the branch a pull request targets.
"""

APPROVE_DESCRIPTION = (
    "Approve the proposal that is waiting in a conversation, so the agent opens the pull request or applies "
    "the change. Only call this after the user has explicitly told you to approve this exact proposal. "
    "The user may be asked to confirm."
)


class ApprovalConfirmation(BaseModel):
    approve: bool = Field(description="Approve this proposal and let the agent carry it out")


def make_bridge(settings: McpSettings) -> Bridge:
    session_settings = settings.model_copy(
        update={"wait_seconds": settings.mcp_tool_wait_seconds, "poll_seconds": settings.mcp_poll_seconds}
    )
    client = AgentClient(session_settings.ari_agent_url, session_settings.ari_api_key, timeout=session_settings.request_timeout_seconds)
    store = SessionStore(path=settings.mcp_session_file or None)
    return Bridge(client, session_settings, store)


def current_actor() -> str:
    token = get_access_token()
    subject = (token.subject or "").strip().lower() if token is not None else ""
    if not subject:
        raise ToolError("not signed in")
    return subject


def progress_reporter(ctx: Context | None):
    if ctx is None:
        return None
    step = {"n": 0}

    async def report(message: str) -> None:
        step["n"] += 1
        try:
            await ctx.report_progress(step["n"], None, message)
        except Exception as exc:
            logger.debug("progress notification failed: %s", exc)

    return report


APPROVAL_INPUT_KEY = "approve"
INPUT_REQUIRED_PROTOCOL = "2026-07-28"
APPROVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "approve": {
            "type": "boolean",
            "title": "Approve",
            "description": "Approve this proposal and let the agent carry it out",
        }
    },
    "required": ["approve"],
}


def approval_message(summary: str) -> str:
    return f"Approve this proposal from the ARI agent?\n\n{summary}"


def approval_state(conversation_id: str, proposal_id: str, actor: str) -> str:
    return json.dumps({"conversation": conversation_id.strip(), "proposal": proposal_id.strip(), "actor": actor}, sort_keys=True)


def answer_of(response: Any) -> str:
    action = getattr(response, "action", None)
    content = getattr(response, "content", None) or {}
    if action == "accept" and isinstance(content, dict) and content.get("approve") is True:
        return "accept"
    return "decline" if action == "decline" else "cancel"


def asks_by_input_required(ctx: Context) -> bool:
    version = ctx.protocol_version or ""
    return version >= INPUT_REQUIRED_PROTOCOL


_asked_at: dict[str, float] = {}


async def confirmation(ctx: Context | None, state: str, summary: str) -> str | InputRequiredResult:
    capabilities = ctx.client_capabilities if ctx is not None else None
    if capabilities is None or getattr(capabilities, "elicitation", None) is None:
        logger.info("approval confirmation: client declares no elicitation, relying on its own tool prompt")
        return "unsupported"
    responses = ctx.input_responses
    if responses is not None:
        if ctx.request_state != state:
            logger.warning("approval confirmation refused: it was given for a different proposal or user")
            return "cancel"
        response = responses.get(APPROVAL_INPUT_KEY)
        answer = answer_of(response)
        asked = _asked_at.pop(state, None)
        waited = f"{time.monotonic() - asked:.2f}s" if asked is not None else "unknown"
        logger.info(
            "approval confirmation answered by input_required: %s (action=%s, after %s, protocol %s)",
            answer, getattr(response, "action", None), waited, ctx.protocol_version,
        )
        return answer
    ask = InputRequiredResult(
        input_requests={
            APPROVAL_INPUT_KEY: ElicitRequest(
                params=ElicitRequestFormParams(message=approval_message(summary), requested_schema=APPROVAL_SCHEMA)
            )
        },
        request_state=state,
    )
    if asks_by_input_required(ctx):
        _asked_at[state] = time.monotonic()
        logger.info("approval confirmation asked by input_required (protocol %s)", ctx.protocol_version)
        return ask
    started = time.monotonic()
    try:
        result = await ctx.elicit(approval_message(summary), ApprovalConfirmation)
    except NoBackChannelError:
        _asked_at[state] = time.monotonic()
        logger.info("approval confirmation asked by input_required: no back-channel (protocol %s)", ctx.protocol_version)
        return ask
    except Exception as exc:
        logger.warning("approval confirmation failed: %s", exc)
        return "cancel"
    answer = "accept" if result.action == "accept" and result.data.approve else ("decline" if result.action != "cancel" else "cancel")
    logger.info(
        "approval confirmation answered by elicitation: %s (action=%s, after %.2fs, protocol %s)",
        answer, result.action, time.monotonic() - started, ctx.protocol_version,
    )
    return answer


def approval_link_text(conversation_id: str, proposal_id: str, link: str, *, asked_in_app: bool) -> str:
    opening = (
        "Not approved in the app: the confirmation was declined or not shown."
        if asked_in_app
        else "Not approved yet: this proposal is approved in the browser."
    )
    return "\n".join(
        [
            f"conversation_id: {conversation_id}",
            f"proposal_id: {proposal_id}",
            "state: waiting for approval",
            "",
            opening,
            f"Approval link (valid {LINK_SECONDS // 60} minutes, single use): {link}",
            "",
            "Give the user this link exactly as it is. The user opens it in their own browser, signs in with GitHub, "
            "reviews the proposal and approves or declines it there. Do not open, fetch or follow the link yourself; "
            "an approval only counts when a person makes it in the browser. Afterwards call ari_status with this "
            "conversation_id to follow the result.",
        ]
    )


def confirmer(ctx: Context | None, state: str = ""):
    async def confirm(summary: str) -> str:
        answer = await confirmation(ctx, state, summary)
        return "cancel" if isinstance(answer, InputRequiredResult) else answer

    return confirm


def build_server(settings: McpSettings, service: AriService, provider: AriOAuthProvider, approvals: ApprovalPages) -> MCPServer:
    auth = AuthSettings(
        issuer_url=settings.public_url(),
        resource_server_url=settings.resource_url(),
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    server = MCPServer(
        name="ari",
        title="ARI Agent",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=auth,
    )

    async def run(coroutine):
        try:
            return await coroutine
        except UserFacingError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        title="Ask the ARI agent",
        description=(
            "Send a request to the ARI agent: investigate an incident or error, fix code and prepare a pull request, "
            "check or change Kubernetes workloads, or ask a question. Pass conversation_id to continue earlier work."
        ),
        structured_output=False,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    )
    async def ari_ask(
        request: Annotated[str, Field(description="What you want the agent to do or answer, in plain language")],
        conversation_id: Annotated[str, Field(description="Continue this earlier conversation; leave empty to start a new one")] = "",
        repo: Annotated[str, Field(description="Repository to work on, as an https URL; empty uses the default")] = "",
        ref: Annotated[str, Field(description="Branch to work from")] = "",
        base: Annotated[str, Field(description="Branch a pull request should target")] = "",
        ctx: Context = None,
    ) -> str:
        return await run(service.ask(current_actor(), request, conversation_id, repo, ref, base, progress_reporter(ctx)))

    @server.tool(
        title="Follow an ARI conversation",
        description="Show where the work in a conversation stands, waiting briefly while the agent is still working.",
        structured_output=False,
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    async def ari_status(conversation_id: Annotated[str, Field(description="The conversation_id from ari_ask")], ctx: Context = None) -> str:
        return await run(service.status(current_actor(), conversation_id, progress_reporter(ctx)))

    @server.tool(
        title="Approve an ARI proposal",
        description=APPROVE_DESCRIPTION,
        structured_output=False,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
    )
    async def ari_approve(
        conversation_id: Annotated[str, Field(description="The conversation_id from ari_ask")],
        proposal_id: Annotated[str, Field(description="The proposal_id of the proposal the user approved")],
        ctx: Context = None,
    ) -> str | InputRequiredResult:
        actor = current_actor()
        summary, current = await run(service.prepare_approval(actor, conversation_id, proposal_id))
        if current is None:
            return summary
        mode = settings.approval_confirmation()
        if mode == "app":
            logger.info("approval confirmation skipped: MCP_APPROVAL_CONFIRMATION=app relies on the app's own tool prompt")
            answer = "unsupported"
        elif mode == "browser":
            answer = "browser"
        else:
            answer = await confirmation(ctx, approval_state(conversation_id, proposal_id, actor), summary)
        if isinstance(answer, InputRequiredResult):
            return answer
        if answer != "accept" and mode != "app":
            link = approvals.create(conversation_id, proposal_id, actor)
            return approval_link_text(conversation_id, proposal_id, link, asked_in_app=answer in ("decline", "cancel"))
        return await run(
            service.finish_approval(
                actor,
                conversation_id,
                proposal_id,
                answer,
                current,
                progress=progress_reporter(ctx),
            )
        )

    @server.tool(
        title="Decline or stop",
        description="Decline the proposal waiting in a conversation, or stop work that is still running.",
        structured_output=False,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True),
    )
    async def ari_decline(conversation_id: Annotated[str, Field(description="The conversation_id from ari_ask")]) -> str:
        return await run(service.decline(current_actor(), conversation_id))

    @server.tool(
        title="Who am I",
        description="Show who you are signed in as, whether you may approve other people's proposals, and what the agent can do.",
        structured_output=False,
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    async def ari_whoami() -> str:
        actor = current_actor()
        lines = [f"signed in as GitHub user: {actor}"]
        approvers = settings.approver_set()
        lines.append(f"may approve other people's proposals: {'yes' if actor in approvers else 'no'}")
        lines.append(f"may approve own proposals: {'yes' if settings.requester_can_approve else 'no'}")
        if settings.default_repo_url:
            lines.append(f"default repository: {settings.default_repo_url}" + (f" ({settings.default_repo_ref})" if settings.default_repo_ref else ""))
        try:
            card = await service.bridge.client.agent_card(max_attempts=1, timeout=10)
        except Exception as exc:
            lines.append(f"agent: unreachable ({exc})")
            return "\n".join(lines)
        lines.append(f"agent: {card.get('name') or 'unknown'}")
        for skill in card.get("skills") or []:
            if isinstance(skill, dict):
                lines.append(f"- {skill.get('name') or skill.get('id')}: {skill.get('description') or ''}".rstrip(": "))
        return "\n".join(lines)

    @server.custom_route(CONSENT_PATH, methods=["GET"], include_in_schema=False)
    async def consent_get(request: Request) -> Response:
        return await provider.consent_page(request)

    @server.custom_route(CONSENT_PATH, methods=["POST"], include_in_schema=False)
    async def consent_post(request: Request) -> Response:
        return await provider.consent_submit(request)

    @server.custom_route(GITHUB_CALLBACK_PATH, methods=["GET"], include_in_schema=False)
    async def github_callback(request: Request) -> Response:
        if approvals.owns(request.query_params.get("state", "")):
            return await approvals.github_callback(request)
        return await provider.github_callback(request)

    @server.custom_route(APPROVAL_PATH + "/{approval_id}", methods=["GET"], include_in_schema=False)
    async def approval_page(request: Request) -> Response:
        return await approvals.page(request)

    @server.custom_route(APPROVAL_PATH + "/{approval_id}", methods=["POST"], include_in_schema=False)
    async def approval_decision(request: Request) -> Response:
        return await approvals.decide(request)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return server


def transport_security(settings: McpSettings) -> TransportSecuritySettings:
    host = settings.public_host()
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "localhost:*", "127.0.0.1:*"],
        allowed_origins=[settings.public_url(), "https://claude.ai", "https://claude.com", "http://localhost:*", "http://127.0.0.1:*"],
    )


def build_app(settings: McpSettings, *, login: GitHubLogin | None = None, bridge: Bridge | None = None) -> tuple[Starlette, dict[str, Any]]:
    bridge = bridge or make_bridge(settings)
    state = StateFile(settings.mcp_state_file or None)
    login = login or GitHubLogin(settings)
    provider = AriOAuthProvider(settings, state, login)
    service = AriService(bridge)
    approvals = ApprovalPages(settings, service, login)
    server = build_server(settings, service, provider, approvals)
    app = server.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        transport_security=transport_security(settings),
        host=settings.mcp_host,
    )
    return app, {"server": server, "service": service, "provider": provider, "state": state, "approvals": approvals}
