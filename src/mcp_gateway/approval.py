import html
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable

import httpx
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from .oauth import GitHubLogin, LoginRefused, _page
from .service import AriService, UserFacingError
from .settings import McpSettings

logger = logging.getLogger("mcp_gateway.approval")

APPROVAL_PATH = "/approve"
STATE_PREFIX = "approve."
LINK_SECONDS = 900
SESSION_SECONDS = 600
MAX_PENDING = 1000


@dataclass
class PendingApproval:
    approval_id: str
    conversation_id: str
    proposal_id: str
    requested_by: str
    created_at: float
    binding: str = ""
    login_state: str = ""
    approver: str = ""
    session: str = ""
    session_at: float = 0.0
    csrf: str = ""


class ApprovalPages:
    def __init__(
        self,
        settings: McpSettings,
        service: AriService,
        login: GitHubLogin,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.service = service
        self.login = login
        self._clock = clock
        self._pending: dict[str, PendingApproval] = {}

    @property
    def secure(self) -> bool:
        return self.settings.public_url().startswith("https://")

    def _cookie(self, name: str) -> str:
        return f"__Host-{name}" if self.secure else name

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, p in self._pending.items() if now - p.created_at > LINK_SECONDS]:
            del self._pending[key]

    def create(self, conversation_id: str, proposal_id: str, requested_by: str) -> str:
        self._expire()
        for key in [k for k, p in self._pending.items() if p.conversation_id == conversation_id]:
            del self._pending[key]
        if len(self._pending) >= MAX_PENDING:
            oldest = min(self._pending.values(), key=lambda p: p.created_at)
            del self._pending[oldest.approval_id]
        approval_id = secrets.token_urlsafe(32)
        self._pending[approval_id] = PendingApproval(
            approval_id, conversation_id.strip(), proposal_id.strip(), requested_by, self._clock()
        )
        logger.info("approval link created for conversation %s by %s", conversation_id, requested_by)
        return f"{self.settings.public_url()}{APPROVAL_PATH}/{approval_id}"

    def _find(self, approval_id: str) -> PendingApproval | None:
        self._expire()
        return self._pending.get(approval_id or "")

    def _signed_in(self, request: Request, pending: PendingApproval) -> bool:
        cookie = request.cookies.get(self._cookie("ari_mcp_approver"), "")
        fresh = self._clock() - pending.session_at <= SESSION_SECONDS
        return bool(pending.session) and fresh and secrets.compare_digest(cookie, pending.session)

    def _set_cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(self._cookie(name), value, max_age=max_age, path="/", secure=self.secure, httponly=True, samesite="lax")

    def _expired_page(self) -> Response:
        return _page(
            "Link expired",
            "<h1>This approval link has expired or was already used</h1>"
            "<p>Ask your app to approve the proposal again to get a new link.</p>",
            404,
        )

    async def page(self, request: Request) -> Response:
        pending = self._find(request.path_params.get("approval_id", ""))
        if pending is None:
            return self._expired_page()
        if not self._signed_in(request, pending):
            pending.binding = secrets.token_urlsafe(32)
            pending.login_state = STATE_PREFIX + secrets.token_urlsafe(32)
            response = RedirectResponse(self.login.authorize_url(pending.login_state), status_code=303)
            self._set_cookie(response, "ari_mcp_approve_login", pending.binding, LINK_SECONDS)
            return response
        return await self._proposal_page(pending)

    async def _proposal_page(self, pending: PendingApproval, error: str = "") -> Response:
        try:
            summary, current = await self.service.prepare_approval(pending.approver, pending.conversation_id, pending.proposal_id)
        except UserFacingError as exc:
            return _page("Nothing to approve", f"<h1>Nothing to approve</h1><p>{html.escape(str(exc))}</p>", 409)
        if current is None:
            return _page("Not allowed", f"<h1>You cannot approve this</h1><p>{html.escape(summary.removeprefix('Not done: ').splitlines()[0])}</p>", 403)
        pending.csrf = secrets.token_urlsafe(32)
        lines = [html.escape(line) for line in summary.splitlines()]
        details = "<br>".join(_link(line) for line in lines)
        notice = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        body = (
            "<h1>Approve this change from the ARI agent?</h1>"
            f"<p class='muted'>Signed in as <strong>{html.escape(pending.approver)}</strong>. "
            f"Requested by <strong>{html.escape(pending.requested_by)}</strong>.</p>"
            f"<p>{details}</p>"
            "<p class='muted'>Approving lets the agent open this pull request or apply this change. "
            "It never merges a pull request.</p>"
            f"{notice}"
            f"<form method='post' action='{APPROVAL_PATH}/{html.escape(pending.approval_id)}'>"
            f"<input type='hidden' name='csrf' value='{html.escape(pending.csrf)}'>"
            "<p><label><input type='checkbox' name='reviewed' value='yes' required> I have reviewed this change</label></p>"
            "<p class='actions'>"
            "<button class='primary' name='decision' value='approve' type='submit'>Approve</button>"
            "<button class='danger' name='decision' value='decline' type='submit' formnovalidate>Decline</button>"
            "</p>"
            "</form>"
        )
        return _page("Approve this change", body, 400 if error else 200)

    async def decide(self, request: Request) -> Response:
        pending = self._find(request.path_params.get("approval_id", ""))
        if pending is None:
            return self._expired_page()
        form = await request.form()
        csrf = str(form.get("csrf") or "")
        if not self._signed_in(request, pending) or not pending.csrf or not secrets.compare_digest(csrf, pending.csrf):
            return _page("Refused", "<h1>Refused</h1><p>Open the approval link again in this browser.</p>", 400)
        decision = form.get("decision")
        if decision == "approve" and form.get("reviewed") != "yes":
            return await self._proposal_page(
                pending, error="Tick \"I have reviewed this change\" before approving. Nothing was changed."
            )
        self._pending.pop(pending.approval_id, None)
        try:
            if decision == "approve":
                summary, current = await self.service.prepare_approval(pending.approver, pending.conversation_id, pending.proposal_id)
                if current is None:
                    return _page("Not allowed", "<h1>You cannot approve this</h1>", 403)
                result = await self.service.finish_approval(
                    pending.approver, pending.conversation_id, pending.proposal_id, "accept", current
                )
                logger.info("approved in the browser by %s for conversation %s", pending.approver, pending.conversation_id)
                title = "Approved"
            elif decision == "decline":
                result = await self.service.decline(pending.approver, pending.conversation_id)
                logger.info("declined in the browser by %s for conversation %s", pending.approver, pending.conversation_id)
                title = "Declined"
            else:
                return _page("Refused", "<h1>Refused</h1><p>Unknown decision.</p>", 400)
        except UserFacingError as exc:
            return _page("Not done", f"<h1>Not done</h1><p>{html.escape(str(exc))}</p>", 409)
        lines = [html.escape(_plain(line)) for line in result.splitlines() if line.strip()]
        body = (
            f"<h1>{title}</h1>"
            f"<p>{'<br>'.join(_link(line) for line in lines)}</p>"
            "<p class='muted'>You can close this page and continue in your app.</p>"
        )
        response = _page(title, body)
        response.delete_cookie(self._cookie("ari_mcp_approver"), path="/", secure=self.secure, httponly=True, samesite="lax")
        return response

    def owns(self, state: str) -> bool:
        return (state or "").startswith(STATE_PREFIX)

    async def github_callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        self._expire()
        pending = next((p for p in self._pending.values() if p.login_state and secrets.compare_digest(p.login_state, state)), None)
        if pending is None:
            return self._expired_page()
        binding = request.cookies.get(self._cookie("ari_mcp_approve_login"), "")
        if not pending.binding or not secrets.compare_digest(binding, pending.binding):
            return _page("Sign-in refused", "<h1>Sign-in refused</h1><p>Finish the sign-in in the browser where you opened the link.</p>", 400)
        pending.login_state = ""
        pending.binding = ""
        if request.query_params.get("error"):
            return _page("Sign-in cancelled", "<h1>Sign-in cancelled</h1><p>Nothing was approved.</p>", 400)
        try:
            login = await self.login.identify(request.query_params.get("code", ""))
        except LoginRefused as exc:
            return _page("Not allowed", f"<h1>Not allowed</h1><p>{html.escape(str(exc))}</p>", 403)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("GitHub sign-in for an approval failed: %s", exc)
            return _page("Sign-in failed", "<h1>Sign-in failed</h1><p>GitHub could not be reached. Try again.</p>", 502)
        pending.approver = login
        pending.session = secrets.token_urlsafe(32)
        pending.session_at = self._clock()
        response = RedirectResponse(f"{self.settings.public_url()}{APPROVAL_PATH}/{pending.approval_id}", status_code=303)
        self._set_cookie(response, "ari_mcp_approver", pending.session, SESSION_SECONDS)
        response.delete_cookie(self._cookie("ari_mcp_approve_login"), path="/", secure=self.secure, httponly=True, samesite="lax")
        return response


URL_RE = re.compile(r"https://[^\s<>'\"]+")


def _plain(line: str) -> str:
    return line.replace("**", "").replace("`", "")


def _link(escaped_line: str) -> str:
    def anchor(match: re.Match) -> str:
        url = match.group(0).rstrip(".,;)")
        tail = match.group(0)[len(url):]
        return f"<a href='{url}' rel='noopener noreferrer' target='_blank'>{url}</a>{tail}"

    return URL_RE.sub(anchor, escaped_line)
