import hashlib
import html
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .settings import McpSettings

logger = logging.getLogger("mcp_gateway.oauth")

PENDING_SECONDS = 600
CODE_SECONDS = 300
CONSENT_PATH = "/oauth/consent"
GITHUB_CALLBACK_PATH = "/oauth/github/callback"
STATE_VERSION = 1


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginRefused(Exception):
    pass


@dataclass
class PendingLogin:
    request_id: str
    client_id: str
    params: AuthorizationParams
    created_at: float
    binding: str = ""
    consented: bool = False


@dataclass
class TokenState:
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    access: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh: dict[str, dict[str, Any]] = field(default_factory=dict)


class StateFile:
    def __init__(self, path: str | os.PathLike | None, clock: Callable[[], float] = time.time):
        self._path = Path(path) if path else None
        self._clock = clock
        self.state = TokenState()
        if self._path is not None:
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("could not read OAuth state from %s, starting empty: %s", self._path, exc)
            return
        if not isinstance(data, dict):
            return
        for name in ("clients", "access", "refresh"):
            value = data.get(name)
            if isinstance(value, dict):
                setattr(self.state, name, {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, dict)})
        self.prune()

    def prune(self) -> bool:
        now = self._clock()
        changed = False
        for table in (self.state.access, self.state.refresh):
            for key in [k for k, v in table.items() if (v.get("expires_at") or 0) < now]:
                del table[key]
                changed = True
        return changed

    def save(self) -> None:
        if self._path is None:
            return
        self.prune()
        data = {
            "version": STATE_VERSION,
            "clients": self.state.clients,
            "access": self.state.access,
            "refresh": self.state.refresh,
        }
        temporary = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(data, out)
            os.replace(temporary, self._path)
        except OSError as exc:
            logger.warning("could not save OAuth state to %s: %s", self._path, exc)

    def revoke_subject(self, subject: str) -> int:
        subject = subject.strip().lower()
        removed = 0
        for table in (self.state.access, self.state.refresh):
            for key in [k for k, v in table.items() if str(v.get("subject") or "").lower() == subject]:
                del table[key]
                removed += 1
        if removed:
            self.save()
        return removed


class GitHubLogin:
    def __init__(self, settings: McpSettings, http_factory: Callable[[], httpx.AsyncClient] | None = None):
        self.settings = settings
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=15))

    def callback_url(self) -> str:
        return self.settings.public_url() + GITHUB_CALLBACK_PATH

    def scope(self) -> str:
        return "read:org" if self.settings.allowed_org_list() else ""

    def authorize_url(self, state: str) -> str:
        query = {
            "client_id": self.settings.github_oauth_client_id,
            "redirect_uri": self.callback_url(),
            "state": state,
            "allow_signup": "false",
        }
        if self.scope():
            query["scope"] = self.scope()
        return f"{self.settings.github_web_url.rstrip('/')}/login/oauth/authorize?{urlencode(query)}"

    async def identify(self, code: str) -> str:
        api = self.settings.github_api_url.rstrip("/")
        async with self._http_factory() as http:
            response = await http.post(
                f"{self.settings.github_web_url.rstrip('/')}/login/oauth/access_token",
                data={
                    "client_id": self.settings.github_oauth_client_id,
                    "client_secret": self.settings.github_oauth_client_secret,
                    "code": code,
                    "redirect_uri": self.callback_url(),
                },
                headers={"Accept": "application/json"},
            )
            body = response.json() if response.content else {}
            github_token = body.get("access_token") if isinstance(body, dict) else None
            if response.status_code != 200 or not github_token:
                error = body.get("error_description") or body.get("error") if isinstance(body, dict) else ""
                raise LoginRefused(f"GitHub did not complete the sign-in ({error or response.status_code})")
            headers = {"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"}
            user = await http.get(f"{api}/user", headers=headers)
            login = user.json().get("login") if user.status_code == 200 else None
            if not isinstance(login, str) or not login:
                raise LoginRefused(f"GitHub did not return the signed-in user (HTTP {user.status_code})")
            login = login.lower()
            if login in self.settings.allowed_user_set():
                return login
            for org in self.settings.allowed_org_list():
                membership = await http.get(f"{api}/user/memberships/orgs/{org}", headers=headers)
                if membership.status_code == 200 and membership.json().get("state") == "active":
                    return login
            raise LoginRefused(
                f"GitHub user {login} is not allowed to use this agent; ask an administrator to add you"
            )


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    document = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>"
        "body{font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#1f2328;background:#fff}"
        "h1{font-size:20px}code{background:#f6f8fa;padding:2px 4px;border-radius:4px;word-break:break-all}"
        "button{font-size:15px;padding:8px 16px;margin-right:8px;border-radius:6px;border:1px solid #d0d7de;cursor:pointer}"
        ".primary{background:#1f883d;color:#fff;border-color:#1f883d}.muted{color:#59636e;font-size:14px}"
        ".danger{background:#fff;color:#cf222e;border-color:#cf222e}"
        ".actions{display:flex;gap:24px;align-items:center}.actions button{margin-right:0;min-width:110px}"
        ".error{color:#cf222e;background:#ffebe9;border:1px solid #ff818266;border-radius:6px;padding:8px 12px}"
        "</style></head><body>"
        f"{body}</body></html>"
    )
    response = HTMLResponse(document, status_code=status_code)
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class AriOAuthProvider:
    def __init__(
        self,
        settings: McpSettings,
        state: StateFile,
        login: GitHubLogin,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.store = state
        self.login = login
        self._clock = clock
        self._pending: dict[str, PendingLogin] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    @property
    def cookie_name(self) -> str:
        return "__Host-ari_mcp_login" if self.settings.public_url().startswith("https://") else "ari_mcp_login"

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, p in self._pending.items() if now - p.created_at > PENDING_SECONDS]:
            del self._pending[key]
        for key in [k for k, c in self._codes.items() if c.expires_at < now]:
            del self._codes[key]

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.state.clients.get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if len(self.store.state.clients) >= self.settings.mcp_max_clients:
            raise RegistrationError("invalid_client_metadata", "this server has reached its limit of registered clients")
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_redirect_uri", "at least one redirect_uri is required")
        self.store.state.clients[client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
        self.store.save()
        logger.info("registered OAuth client %s (%s)", client_info.client_id, client_info.client_name or "unnamed")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.settings.resource_url().rstrip("/"):
            raise AuthorizeError("invalid_target", "tokens are only issued for this MCP server")
        self._expire()
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = PendingLogin(request_id, client.client_id, params, self._clock())
        return f"{self.settings.public_url()}{CONSENT_PATH}?{urlencode({'request': request_id})}"

    def _pending_for(self, request_id: str) -> PendingLogin | None:
        self._expire()
        return self._pending.get(request_id or "")

    async def consent_page(self, request: Request) -> Response:
        pending = self._pending_for(request.query_params.get("request", ""))
        if pending is None:
            return _page("Sign-in expired", "<h1>This sign-in link has expired</h1><p>Start the connection again from your app.</p>", 400)
        client = await self.get_client(pending.client_id)
        if client is None:
            return _page("Unknown app", "<h1>Unknown app</h1><p>Start the connection again from your app.</p>", 400)
        pending.binding = secrets.token_urlsafe(32)
        redirect = str(pending.params.redirect_uri)
        app_name = client.client_name or "An app"
        body = (
            "<h1>Connect to the ARI agent</h1>"
            f"<p><strong>{html.escape(app_name)}</strong> wants to use the ARI agent on your behalf. "
            "It will be able to send requests to the agent, see your conversations with it, and approve or decline "
            "changes the agent proposes to you.</p>"
            f"<p class='muted'>After you sign in, you will be sent back to:<br><code>{html.escape(redirect)}</code></p>"
            "<p class='muted'>Continue only if you started this connection yourself.</p>"
            f"<form method='post' action='{CONSENT_PATH}'>"
            f"<input type='hidden' name='request' value='{html.escape(pending.request_id)}'>"
            "<button class='primary' name='decision' value='allow' type='submit'>Continue with GitHub</button>"
            "<button name='decision' value='deny' type='submit'>Cancel</button>"
            "</form>"
        )
        response = _page("Connect to the ARI agent", body)
        response.set_cookie(
            self.cookie_name,
            pending.binding,
            max_age=PENDING_SECONDS,
            path="/",
            secure=self.cookie_name.startswith("__Host-"),
            httponly=True,
            samesite="lax",
        )
        return response

    def _bound(self, request: Request, pending: PendingLogin) -> bool:
        cookie = request.cookies.get(self.cookie_name, "")
        return bool(pending.binding) and secrets.compare_digest(cookie, pending.binding)

    def _client_error(self, pending: PendingLogin, error: str, description: str) -> Response:
        location = construct_redirect_uri(
            str(pending.params.redirect_uri), error=error, error_description=description, state=pending.params.state
        )
        return RedirectResponse(location, status_code=302)

    async def consent_submit(self, request: Request) -> Response:
        form = await request.form()
        pending = self._pending_for(str(form.get("request") or ""))
        if pending is None:
            return _page("Sign-in expired", "<h1>This sign-in link has expired</h1><p>Start the connection again from your app.</p>", 400)
        if not self._bound(request, pending):
            return _page("Sign-in refused", "<h1>Sign-in refused</h1><p>Open the link from your app in this browser and try again.</p>", 400)
        if form.get("decision") != "allow":
            self._pending.pop(pending.request_id, None)
            return self._client_error(pending, "access_denied", "the user cancelled the connection")
        pending.consented = True
        return RedirectResponse(self.login.authorize_url(pending.request_id), status_code=303)

    async def github_callback(self, request: Request) -> Response:
        pending = self._pending_for(request.query_params.get("state", ""))
        if pending is None or not pending.consented:
            return _page("Sign-in expired", "<h1>This sign-in has expired</h1><p>Start the connection again from your app.</p>", 400)
        if not self._bound(request, pending):
            return _page("Sign-in refused", "<h1>Sign-in refused</h1><p>Finish the sign-in in the browser where you started it.</p>", 400)
        self._pending.pop(pending.request_id, None)
        if request.query_params.get("error"):
            return self._client_error(pending, "access_denied", "GitHub sign-in was cancelled")
        try:
            subject = await self.login.identify(request.query_params.get("code", ""))
        except LoginRefused as exc:
            logger.info("sign-in refused: %s", exc)
            return _page("Not allowed", f"<h1>Not allowed</h1><p>{html.escape(str(exc))}</p>", 403)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("GitHub sign-in failed: %s", exc)
            return _page("Sign-in failed", "<h1>Sign-in failed</h1><p>GitHub could not be reached. Try again.</p>", 502)
        code = secrets.token_urlsafe(32)
        self._codes[token_hash(code)] = AuthorizationCode(
            code=code,
            scopes=pending.params.scopes or [],
            expires_at=self._clock() + CODE_SECONDS,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=self.settings.resource_url(),
            subject=subject,
        )
        logger.info("signed in %s for client %s", subject, pending.client_id)
        response = RedirectResponse(
            construct_redirect_uri(str(pending.params.redirect_uri), code=code, state=pending.params.state),
            status_code=302,
        )
        response.delete_cookie(self.cookie_name, path="/", secure=self.cookie_name.startswith("__Host-"), httponly=True, samesite="lax")
        return response

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        self._expire()
        code = self._codes.get(token_hash(authorization_code))
        if code is None or code.client_id != client.client_id:
            return None
        return code

    def _issue(self, client_id: str, scopes: list[str], subject: str) -> OAuthToken:
        now = int(self._clock())
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        access_key = token_hash(access)
        self.store.state.access[access_key] = {
            "client_id": client_id,
            "scopes": scopes,
            "subject": subject,
            "expires_at": now + self.settings.mcp_access_token_seconds,
        }
        self.store.state.refresh[token_hash(refresh)] = {
            "client_id": client_id,
            "scopes": scopes,
            "subject": subject,
            "access": access_key,
            "expires_at": now + self.settings.mcp_refresh_token_seconds,
        }
        self.store.save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.settings.mcp_access_token_seconds,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if self._codes.pop(token_hash(authorization_code.code), None) is None:
            raise TokenError("invalid_grant", "authorization code already used or expired")
        if not authorization_code.subject:
            raise TokenError("invalid_grant", "authorization code has no user")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        record = self.store.state.refresh.get(token_hash(refresh_token))
        if record is None or record.get("client_id") != client.client_id or record.get("expires_at", 0) < self._clock():
            return None
        if not self._still_allowed(str(record.get("subject") or "")):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=list(record.get("scopes") or []),
            expires_at=int(record["expires_at"]),
            resource=self.settings.resource_url(),
            subject=record.get("subject"),
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        record = self.store.state.refresh.pop(token_hash(refresh_token.token), None)
        if record is None:
            raise TokenError("invalid_grant", "refresh token already used or revoked")
        self.store.state.access.pop(str(record.get("access") or ""), None)
        granted = list(record.get("scopes") or [])
        if scopes and not set(scopes) <= set(granted):
            raise TokenError("invalid_scope", "cannot widen the granted scopes")
        return self._issue(client.client_id, scopes or granted, str(record.get("subject") or ""))

    def _still_allowed(self, subject: str) -> bool:
        if not subject:
            return False
        if self.settings.allowed_org_list():
            return True
        return subject.lower() in self.settings.allowed_user_set()

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = self.store.state.access.get(token_hash(token))
        if record is None or record.get("expires_at", 0) < self._clock():
            return None
        subject = str(record.get("subject") or "")
        if not self._still_allowed(subject):
            return None
        return AccessToken(
            token=token,
            client_id=str(record.get("client_id") or ""),
            scopes=list(record.get("scopes") or []),
            expires_at=int(record["expires_at"]),
            resource=self.settings.resource_url(),
            subject=subject,
            claims={"iss": self.settings.public_url()},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        key = token_hash(token.token)
        if self.store.state.access.pop(key, None) is not None:
            for refresh_key in [k for k, v in self.store.state.refresh.items() if v.get("access") == key]:
                del self.store.state.refresh[refresh_key]
        record = self.store.state.refresh.pop(key, None)
        if record is not None:
            self.store.state.access.pop(str(record.get("access") or ""), None)
        self.store.save()
