import base64
import hashlib
import json
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("mcp")

from starlette.testclient import TestClient

from chat_gateway.bridge import Bridge
from chat_gateway.session import SessionStore
from mcp_gateway.oauth import GitHubLogin, LoginRefused, StateFile
from mcp_gateway.server import build_app
from mcp_gateway.settings import McpSettings
from tests.test_chat_gateway_bridge import FakeClient, _paused, _task

BASE = "http://localhost:8080"
REDIRECT = "http://localhost:8787/callback"
PROTOCOL = "2025-11-25"


class FakeGitHub(GitHubLogin):
    users = {"alice-code": "alice", "carol-code": "carol"}

    def authorize_url(self, state):
        return f"https://github.example/login/oauth/authorize?state={state}"

    async def identify(self, code):
        if code in self.users:
            return self.users[code]
        raise LoginRefused("GitHub user mallory is not allowed to use this agent")


@pytest.fixture
def settings(monkeypatch, tmp_path):
    for name in McpSettings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    return McpSettings(
        mcp_public_url=BASE,
        mcp_port=8080,
        mcp_state_file=str(tmp_path / "oauth.json"),
        mcp_allowed_users="alice,carol",
        mcp_approvers="carol",
        github_oauth_client_id="gh-id",
        github_oauth_client_secret="gh-secret",
        llm_api_key="sk-test",
        github_pat="ghp_test",
        default_repo_url="https://github.com/org/default",
        poll_seconds=0,
    )


@pytest.fixture
def agent():
    return FakeClient()


@pytest.fixture
def http(settings, agent):
    app, parts = build_app(settings, login=FakeGitHub(settings), bridge=Bridge(agent, settings, SessionStore()))
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        client.parts = parts
        yield client


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(http, redirect=REDIRECT):
    response = http.post(
        "/register",
        json={
            "redirect_uris": [redirect],
            "client_name": "Test App",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def start_login(http, client_id, challenge, resource=f"{BASE}/mcp"):
    response = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "resource": resource,
        },
    )
    return response


def consent(http, login_response, decision="allow"):
    location = login_response.headers["location"]
    assert location.startswith(f"{BASE}/oauth/consent?request=")
    page = http.get(location.removeprefix(BASE))
    assert page.status_code == 200
    assert "Test App" in page.text and REDIRECT in page.text
    assert page.headers["x-frame-options"] == "DENY"
    request_id = parse_qs(urlsplit(location).query)["request"][0]
    submitted = http.post("/oauth/consent", data={"request": request_id, "decision": decision})
    return request_id, submitted


def sign_in(http, user_code="alice-code"):
    client_id = register(http)
    verifier, challenge = pkce()
    request_id, submitted = consent(http, start_login(http, client_id, challenge))
    assert submitted.status_code == 303
    assert submitted.headers["location"].startswith("https://github.example/")
    callback = http.get("/oauth/github/callback", params={"code": user_code, "state": request_id})
    assert callback.status_code == 302, callback.text
    back = urlsplit(callback.headers["location"])
    query = parse_qs(back.query)
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
    assert query["state"] == ["client-state"]
    token = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": f"{BASE}/mcp",
        },
    )
    assert token.status_code == 200, token.text
    return client_id, query["code"][0], verifier, token.json()


def rpc_body(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        messages = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:") and line[5:].strip()]
        return [m for m in messages if "id" in m][-1]
    return response.json()


class McpSession:
    def __init__(self, http, token):
        self.http = http
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self.counter = 0
        init = self.post("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        assert init.status_code == 200, init.text
        session_id = init.headers.get("mcp-session-id")
        if session_id:
            self.headers["mcp-session-id"] = session_id
        self.headers["mcp-protocol-version"] = rpc_body(init)["result"]["protocolVersion"]
        notified = self.http.post("/mcp", headers=self.headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert notified.status_code in (200, 202)

    def post(self, method, params):
        self.counter += 1
        return self.http.post("/mcp", headers=self.headers, json={"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params})

    def call(self, name, **arguments):
        response = self.post("tools/call", {"name": name, "arguments": arguments})
        assert response.status_code == 200, response.text
        result = rpc_body(response)["result"]
        return result.get("isError", False), "\n".join(c.get("text", "") for c in result["content"])


def test_metadata_points_clients_at_this_server(http):
    resource = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == f"{BASE}/mcp"
    assert resource["authorization_servers"][0].rstrip("/") == BASE
    server = http.get("/.well-known/oauth-authorization-server").json()
    assert server["registration_endpoint"] == f"{BASE}/register"
    assert "S256" in server["code_challenge_methods_supported"]


def test_mcp_endpoint_requires_a_token(http):
    response = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Accept": "application/json, text/event-stream"})
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]


def test_full_sign_in_and_tool_calls(http, agent):
    _, _, _, tokens = sign_in(http)
    session = McpSession(http, tokens["access_token"])
    listed = rpc_body(session.post("tools/list", {}))["result"]["tools"]
    assert {t["name"] for t in listed} == {"ari_ask", "ari_status", "ari_approve", "ari_decline", "ari_whoami"}
    failed, text = session.call("ari_ask", request="what is a deadlock?")
    assert not failed, text
    assert "state: done" in text
    assert agent.sent[0]["config"]["llm"]["api_key"] == "sk-test"


def test_requester_identity_comes_from_the_token(http, agent):
    _, _, _, alice = sign_in(http)
    alice_session = McpSession(http, alice["access_token"])
    agent.final["t1"] = _paused("t1", "act-1")
    _, text = alice_session.call("ari_ask", request="add a SECURITY.md")
    conversation_id = next(line.split(": ")[1] for line in text.splitlines() if line.startswith("conversation_id: "))
    assert "proposal_id: act-1" in text
    failed, text = alice_session.call("ari_approve", conversation_id=conversation_id, proposal_id="act-1")
    assert not failed, text
    assert agent.confirmed == []
    approve_in_browser(http, approval_link(text), "alice-code")
    assert agent.confirmed[0]["text"] == "approved by alice"


def test_another_signed_in_user_cannot_touch_the_conversation(http, agent):
    _, _, _, alice = sign_in(http)
    _, _, _, carol_tokens = sign_in(http, "carol-code")
    agent.final["t1"] = _paused("t1", "act-1")
    _, text = McpSession(http, alice["access_token"]).call("ari_ask", request="add a SECURITY.md")
    conversation_id = next(line.split(": ")[1] for line in text.splitlines() if line.startswith("conversation_id: "))
    http.parts["service"].bridge.settings = http.parts["service"].bridge.settings.model_copy(update={"mcp_approvers": ""})
    failed, text = McpSession(http, carol_tokens["access_token"]).call("ari_approve", conversation_id=conversation_id, proposal_id="act-1")
    assert failed and "belongs to alice" in text
    assert agent.confirmed == []


def test_code_cannot_be_used_twice(http):
    client_id, code, verifier, _ = sign_in(http)
    again = http.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": verifier},
    )
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"


def test_refresh_rotates_and_the_old_refresh_token_dies(http):
    client_id, _, _, tokens = sign_in(http)
    refreshed = http.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert refreshed.status_code == 200, refreshed.text
    new = refreshed.json()
    assert new["access_token"] != tokens["access_token"]
    reused = http.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert reused.status_code == 400
    old = http.post("/mcp", headers={"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert old.status_code == 401
    McpSession(http, new["access_token"])


def test_state_file_holds_no_usable_token(http, settings):
    _, _, _, tokens = sign_in(http)
    stored = Path(settings.mcp_state_file).read_text(encoding="utf-8")
    assert tokens["access_token"] not in stored
    assert tokens["refresh_token"] not in stored
    assert "gh-secret" not in stored and "sk-test" not in stored and "ghp_test" not in stored


def test_tokens_survive_a_restart_and_revoke_removes_them(http, settings, agent):
    _, _, _, tokens = sign_in(http)
    app, _ = build_app(settings, login=FakeGitHub(settings), bridge=Bridge(agent, settings, SessionStore()))
    with TestClient(app, base_url=BASE, follow_redirects=False) as restarted:
        McpSession(restarted, tokens["access_token"])
    assert StateFile(settings.mcp_state_file).revoke_subject("Alice") == 2
    app, _ = build_app(settings, login=FakeGitHub(settings), bridge=Bridge(agent, settings, SessionStore()))
    with TestClient(app, base_url=BASE, follow_redirects=False) as revoked:
        response = revoked.post("/mcp", headers={"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401


def test_user_removed_from_the_allow_list_loses_access(http, settings, agent):
    _, _, _, tokens = sign_in(http)
    narrowed = settings.model_copy(update={"mcp_allowed_users": "carol"})
    app, _ = build_app(narrowed, login=FakeGitHub(narrowed), bridge=Bridge(agent, narrowed, SessionStore()))
    with TestClient(app, base_url=BASE, follow_redirects=False) as restarted:
        response = restarted.post("/mcp", headers={"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401


def test_cancel_on_the_consent_page_sends_access_denied(http):
    client_id = register(http)
    _, challenge = pkce()
    _, submitted = consent(http, start_login(http, client_id, challenge), decision="deny")
    query = parse_qs(urlsplit(submitted.headers["location"]).query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["client-state"]


def test_consent_without_the_browser_cookie_is_refused(http):
    client_id = register(http)
    _, challenge = pkce()
    login = start_login(http, client_id, challenge)
    request_id = parse_qs(urlsplit(login.headers["location"]).query)["request"][0]
    http.get(login.headers["location"].removeprefix(BASE))
    http.cookies.clear()
    submitted = http.post("/oauth/consent", data={"request": request_id, "decision": "allow"})
    assert submitted.status_code == 400


def test_github_callback_from_another_browser_is_refused(http):
    client_id = register(http)
    _, challenge = pkce()
    request_id, _ = consent(http, start_login(http, client_id, challenge))
    http.cookies.clear()
    callback = http.get("/oauth/github/callback", params={"code": "alice-code", "state": request_id})
    assert callback.status_code == 400


def test_callback_without_consent_is_refused(http):
    client_id = register(http)
    _, challenge = pkce()
    login = start_login(http, client_id, challenge)
    request_id = parse_qs(urlsplit(login.headers["location"]).query)["request"][0]
    http.get(login.headers["location"].removeprefix(BASE))
    callback = http.get("/oauth/github/callback", params={"code": "alice-code", "state": request_id})
    assert callback.status_code == 400


def test_user_not_on_the_list_is_refused(http):
    client_id = register(http)
    _, challenge = pkce()
    request_id, _ = consent(http, start_login(http, client_id, challenge))
    callback = http.get("/oauth/github/callback", params={"code": "mallory-code", "state": request_id})
    assert callback.status_code == 403
    assert "not allowed" in callback.text


def test_tokens_for_another_resource_are_not_issued(http):
    client_id = register(http)
    _, challenge = pkce()
    response = start_login(http, client_id, challenge, resource="https://other.example/mcp")
    location = response.headers.get("location", "")
    assert "invalid_target" in location or response.status_code == 400


def test_healthz_needs_no_token(http):
    assert http.get("/healthz").json() == {"status": "ok"}


def test_settings_refuse_an_open_server(settings):
    from chat_gateway.config import ConfigError

    with pytest.raises(ConfigError, match="MCP_ALLOWED_USERS or MCP_ALLOWED_GITHUB_ORGS"):
        settings.model_copy(update={"mcp_allowed_users": ""}).check()
    with pytest.raises(ConfigError, match="https"):
        settings.model_copy(update={"mcp_public_url": "http://ari.example.com"}).check()
    with pytest.raises(ConfigError, match="GITHUB_OAUTH_CLIENT_ID"):
        settings.model_copy(update={"github_oauth_client_secret": ""}).check()
    settings.check()


def test_settings_refuse_a_jenkins_address_without_its_token(settings):
    from chat_gateway.config import ConfigError

    with pytest.raises(ConfigError, match="JENKINS_TOKEN"):
        settings.model_copy(update={"jenkins_url": "http://jenkins.example:8080", "jenkins_user": "ci"}).check()


MODERN = "2026-07-28"


class ModernSession:
    def __init__(self, http, token, elicitation=True):
        self.http = http
        self.meta = {
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}} if elicitation else {},
            "io.modelcontextprotocol/clientInfo": {"name": "modern-test", "version": "1"},
        }
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "mcp-protocol-version": MODERN,
            "mcp-method": "tools/call",
        }

    def call(self, name, arguments, **extra):
        params = {"name": name, "arguments": arguments, "_meta": self.meta, **extra}
        response = self.http.post(
            "/mcp", headers={**self.headers, "mcp-name": name}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        )
        assert response.status_code == 200, response.text
        return rpc_body(response)


def modern_proposal(http, agent, user_code="alice-code", elicitation=True):
    _, _, _, tokens = sign_in(http, user_code)
    session = ModernSession(http, tokens["access_token"], elicitation)
    agent.final["t1"] = _paused("t1", "act-1")
    text = session.call("ari_ask", {"request": "add a SECURITY.md"})["result"]["content"][0]["text"]
    conversation_id = next(line.split(": ")[1] for line in text.splitlines() if line.startswith("conversation_id: "))
    return session, conversation_id


def test_new_protocol_asks_the_person_before_approving(http, agent):
    session, conversation_id = modern_proposal(http, agent)
    first = session.call("ari_approve", {"conversation_id": conversation_id, "proposal_id": "act-1"})["result"]
    assert first["resultType"] == "input_required"
    ask = first["inputRequests"]["approve"]
    assert ask["method"] == "elicitation/create"
    assert "Approve this proposal" in ask["params"]["message"]
    assert agent.confirmed == []
    session.call(
        "ari_approve",
        {"conversation_id": conversation_id, "proposal_id": "act-1"},
        inputResponses={"approve": {"action": "accept", "content": {"approve": True}}},
        requestState=first["requestState"],
    )
    assert [c["text"] for c in agent.confirmed] == ["approved by alice"]


def test_new_protocol_decline_keeps_the_proposal_waiting(http, agent):
    session, conversation_id = modern_proposal(http, agent)
    first = session.call("ari_approve", {"conversation_id": conversation_id, "proposal_id": "act-1"})["result"]
    result = session.call(
        "ari_approve",
        {"conversation_id": conversation_id, "proposal_id": "act-1"},
        inputResponses={"approve": {"action": "decline"}},
        requestState=first["requestState"],
    )["result"]
    assert "Not approved" in result["content"][0]["text"]
    assert agent.confirmed == []


def test_new_protocol_accept_without_ticking_approve_is_not_an_approval(http, agent):
    session, conversation_id = modern_proposal(http, agent)
    first = session.call("ari_approve", {"conversation_id": conversation_id, "proposal_id": "act-1"})["result"]
    session.call(
        "ari_approve",
        {"conversation_id": conversation_id, "proposal_id": "act-1"},
        inputResponses={"approve": {"action": "accept", "content": {"approve": False}}},
        requestState=first["requestState"],
    )
    assert agent.confirmed == []


def test_new_protocol_answer_without_the_servers_state_is_refused(http, agent):
    session, conversation_id = modern_proposal(http, agent)
    session.call("ari_approve", {"conversation_id": conversation_id, "proposal_id": "act-1"})
    response = session.http.post(
        "/mcp",
        headers={**session.headers, "mcp-name": "ari_approve"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ari_approve",
                "arguments": {"conversation_id": conversation_id, "proposal_id": "act-1"},
                "_meta": session.meta,
                "inputResponses": {"approve": {"action": "accept", "content": {"approve": True}}},
                "requestState": "v1.forged",
            },
        },
    )
    assert agent.confirmed == []
    assert response.status_code in (200, 400)


def test_new_protocol_answer_cannot_be_replayed_by_another_user(http, agent, settings):
    session, conversation_id = modern_proposal(http, agent)
    first = session.call("ari_approve", {"conversation_id": conversation_id, "proposal_id": "act-1"})["result"]
    _, _, _, carol_tokens = sign_in(http, "carol-code")
    carol = ModernSession(http, carol_tokens["access_token"])
    response = http.post(
        "/mcp",
        headers={**carol.headers, "mcp-name": "ari_approve"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ari_approve",
                "arguments": {"conversation_id": conversation_id, "proposal_id": "act-1"},
                "_meta": carol.meta,
                "inputResponses": {"approve": {"action": "accept", "content": {"approve": True}}},
                "requestState": first["requestState"],
            },
        },
    )
    assert response.status_code == 400
    assert "requestState" in response.text
    assert agent.confirmed == []


def test_new_protocol_without_elicitation_gets_a_browser_link(http, agent):
    session, conversation_id = modern_proposal(http, agent, elicitation=False)
    result = session.call("ari_approve", {"conversation_id": conversation_id, "proposal_id": "act-1"})["result"]
    text = result["content"][0]["text"]
    assert "Do not open, fetch or follow the link yourself" in text
    assert agent.confirmed == []
    approve_in_browser(http, approval_link(text), "alice-code")
    assert [c["text"] for c in agent.confirmed] == ["approved by alice"]


def app_with(settings, agent, **changes):
    changed = settings.model_copy(update=changes)
    app, _ = build_app(changed, login=FakeGitHub(changed), bridge=Bridge(agent, changed, SessionStore()))
    return app


def test_app_mode_skips_the_dialog_and_relies_on_the_apps_prompt(settings, agent):
    with TestClient(app_with(settings, agent, mcp_approval_confirmation="app"), base_url=BASE, follow_redirects=False) as client:
        session, conversation_id = modern_proposal(client, agent)
        result = session.call("ari_approve", {"conversation_id": conversation_id, "proposal_id": "act-1"})["result"]
        assert result.get("resultType") != "input_required"
        assert [c["text"] for c in agent.confirmed] == ["approved by alice"]


def test_browser_mode_never_asks_in_the_app(settings, agent):
    with TestClient(app_with(settings, agent, mcp_approval_confirmation="browser"), base_url=BASE, follow_redirects=False) as client:
        session, conversation_id = modern_proposal(client, agent)
        result = session.call("ari_approve", {"conversation_id": conversation_id, "proposal_id": "act-1"})["result"]
        assert result.get("resultType") != "input_required"
        assert "Approval link" in result["content"][0]["text"]
        assert agent.confirmed == []


def test_unknown_confirmation_mode_is_refused_at_start(settings):
    from chat_gateway.config import ConfigError

    with pytest.raises(ConfigError, match="MCP_APPROVAL_CONFIRMATION"):
        settings.model_copy(update={"mcp_approval_confirmation": "maybe"}).check()


def approval_link(text):
    for line in text.splitlines():
        if line.startswith("Approval link"):
            return line.rsplit(" ", 1)[1]
    raise AssertionError(f"no approval link in: {text}")


def open_approval(http, link, user_code):
    path = link.removeprefix(BASE)
    start = http.get(path)
    assert start.status_code == 303, start.text
    github = urlsplit(start.headers["location"])
    assert github.netloc == "github.example"
    state = parse_qs(github.query)["state"][0]
    back = http.get("/oauth/github/callback", params={"code": user_code, "state": state})
    return path, back


def page_csrf(page):
    return page.text.split("name='csrf' value='", 1)[1].split("'", 1)[0]


def approve_in_browser(http, link, user_code, decision="approve"):
    path, back = open_approval(http, link, user_code)
    assert back.status_code == 303, back.text
    page = http.get(path)
    assert page.status_code == 200, page.text
    assert page.headers["x-frame-options"] == "DENY"
    return http.post(path, data={"csrf": page_csrf(page), "decision": decision, "reviewed": "yes"})


def browser_proposal(http, agent):
    _, _, _, tokens = sign_in(http)
    session = ModernSession(http, tokens["access_token"], elicitation=False)
    agent.final["t1"] = _paused("t1", "act-1")
    text = session.call("ari_ask", {"request": "add a SECURITY.md"})["result"]["content"][0]["text"]
    conversation_id = next(line.split(": ")[1] for line in text.splitlines() if line.startswith("conversation_id: "))
    result = session.call("ari_approve", {"conversation_id": conversation_id, "proposal_id": "act-1"})["result"]
    return session, conversation_id, approval_link(result["content"][0]["text"])


def test_browser_approval_opens_the_pull_request(http, agent):
    _, _, link = browser_proposal(http, agent)
    done = approve_in_browser(http, link, "alice-code")
    assert done.status_code == 200
    assert "Approved" in done.text
    assert [c["text"] for c in agent.confirmed] == ["approved by alice"]


def test_browser_decline_cancels_the_task(http, agent):
    _, _, link = browser_proposal(http, agent)
    done = approve_in_browser(http, link, "alice-code", decision="decline")
    assert "Declined" in done.text
    assert agent.confirmed == [] and agent.canceled == ["t1"]


def test_approval_link_works_once(http, agent):
    _, _, link = browser_proposal(http, agent)
    approve_in_browser(http, link, "alice-code")
    again = http.get(link.removeprefix(BASE))
    assert again.status_code == 404
    assert len(agent.confirmed) == 1


def test_link_without_github_sign_in_shows_nothing(http, agent):
    _, _, link = browser_proposal(http, agent)
    response = http.get(link.removeprefix(BASE))
    assert response.status_code == 303
    assert "csrf" not in response.text
    posted = http.post(link.removeprefix(BASE), data={"csrf": "x", "decision": "approve"})
    assert posted.status_code == 400
    assert agent.confirmed == []


def test_sign_in_finished_in_another_browser_is_refused(http, agent):
    _, _, link = browser_proposal(http, agent)
    start = http.get(link.removeprefix(BASE))
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    http.cookies.clear()
    back = http.get("/oauth/github/callback", params={"code": "alice-code", "state": state})
    assert back.status_code == 400
    assert agent.confirmed == []


def test_post_without_the_pages_csrf_token_is_refused(http, agent):
    _, _, link = browser_proposal(http, agent)
    path, _ = open_approval(http, link, "alice-code")
    assert http.get(path).status_code == 200
    posted = http.post(path, data={"csrf": "forged", "decision": "approve"})
    assert posted.status_code == 400
    assert agent.confirmed == []


def test_someone_else_cannot_approve_through_the_link(http, agent):
    http.parts["service"].bridge.settings = http.parts["service"].bridge.settings.model_copy(update={"mcp_approvers": ""})
    _, _, link = browser_proposal(http, agent)
    path, _ = open_approval(http, link, "carol-code")
    page = http.get(path)
    assert page.status_code in (403, 409)
    assert "csrf" not in page.text
    assert agent.confirmed == []


def test_configured_approver_can_approve_through_a_forwarded_link(http, agent):
    _, _, link = browser_proposal(http, agent)
    approve_in_browser(http, link, "carol-code")
    assert [c["text"] for c in agent.confirmed] == ["approved by carol"]


def test_github_user_not_on_the_list_is_refused_on_the_link(http, agent):
    _, _, link = browser_proposal(http, agent)
    _, back = open_approval(http, link, "mallory-code")
    assert back.status_code == 403
    assert agent.confirmed == []


def test_declined_dialog_offers_the_browser_link(http, agent):
    session, conversation_id = modern_proposal(http, agent)
    first = session.call("ari_approve", {"conversation_id": conversation_id, "proposal_id": "act-1"})["result"]
    result = session.call(
        "ari_approve",
        {"conversation_id": conversation_id, "proposal_id": "act-1"},
        inputResponses={"approve": {"action": "decline"}},
        requestState=first["requestState"],
    )["result"]
    text = result["content"][0]["text"]
    assert "Not approved in the app" in text
    assert "Approval link" in text
    assert agent.confirmed == []


def test_approve_without_ticking_reviewed_changes_nothing_and_keeps_the_link(http, agent):
    _, _, link = browser_proposal(http, agent)
    path, _ = open_approval(http, link, "alice-code")
    page = http.get(path)
    assert "name='reviewed' value='yes' required" in page.text
    assert "value='decline' type='submit' formnovalidate" in page.text
    unticked = http.post(path, data={"csrf": page_csrf(page), "decision": "approve"})
    assert unticked.status_code == 400
    assert "class='error'" in unticked.text
    assert "Approve this change from the ARI agent?" in unticked.text
    assert agent.confirmed == []
    again = http.get(path)
    assert again.status_code == 200
    http.post(path, data={"csrf": page_csrf(again), "decision": "approve", "reviewed": "yes"})
    assert [c["text"] for c in agent.confirmed] == ["approved by alice"]


def test_decline_needs_no_tick(http, agent):
    _, _, link = browser_proposal(http, agent)
    path, _ = open_approval(http, link, "alice-code")
    page = http.get(path)
    done = http.post(path, data={"csrf": page_csrf(page), "decision": "decline"})
    assert "Declined" in done.text
    assert agent.canceled == ["t1"]


def test_result_page_shows_plain_text_and_clickable_links(http, agent):
    _, _, link = browser_proposal(http, agent)
    agent.final["t1"] = _task(
        "t1",
        "TASK_STATE_COMPLETED",
        artifacts=[{"name": "pull_request", "parts": [{"data": {"url": "https://github.com/org/default/pull/9"}}]}],
        text="**Branch:** `ari/x`",
    )
    done = approve_in_browser(http, link, "alice-code")
    assert "**" not in done.text and "`" not in done.text
    assert "<a href='https://github.com/org/default/pull/9'" in done.text
