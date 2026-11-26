import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("mcp")

pytestmark = pytest.mark.asyncio

from chat_gateway.bridge import Bridge
from chat_gateway.session import SessionStore
from mcp_gateway.service import AriService, UserFacingError
from mcp_gateway.settings import McpSettings
from tests.test_chat_gateway_bridge import FakeClient, _paused, _task

SETTINGS_FIELDS = [name.upper() for name in McpSettings.model_fields]


@pytest.fixture
def settings(monkeypatch):
    for name in SETTINGS_FIELDS:
        monkeypatch.delenv(name, raising=False)
    return McpSettings(
        llm_api_key="sk-test",
        github_pat="ghp_test",
        default_repo_url="https://github.com/org/default",
        mcp_approvers="Carol",
        poll_seconds=0,
    )


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def service(client, settings):
    return AriService(Bridge(client, settings, SessionStore()))


def field(text, name):
    for line in text.splitlines():
        if line.startswith(f"{name}: "):
            return line.split(": ", 1)[1]
    return ""


async def accept(summary):
    accept.seen = summary
    return "accept"


async def unsupported(summary):
    return "unsupported"


async def declined(summary):
    return "decline"


async def propose(service, client, actor="alice"):
    client.final["t1"] = _paused("t1", "act-1")
    return await service.ask(actor, "add a SECURITY.md")


async def test_ask_starts_a_conversation_and_returns_its_id(service, client):
    text = await service.ask("alice", "what is a deadlock?")
    conversation_id = field(text, "conversation_id")
    assert len(conversation_id) == 32
    assert field(text, "state") == "done"
    assert client.sent[0]["prompt"] == "what is a deadlock?"
    assert client.sent[0]["config"]["options"]["lane"] == "interactive"


async def test_repo_ref_and_base_become_directives(service, client):
    await service.ask("alice", "fix the NPE", repo="https://github.com/org/x", ref="dev", base="staging")
    sent = client.sent[0]
    assert sent["config"]["repo"]["url"] == "https://github.com/org/x"
    assert sent["config"]["repo"]["ref"] == "dev"
    assert "open it against the staging branch" in sent["prompt"]


async def test_disallowed_repo_host_is_refused_before_sending(service, client):
    with pytest.raises(UserFacingError, match="not allowed"):
        await service.ask("alice", "fix", repo="https://evil.example/x")
    assert client.sent == []


async def test_proposal_shows_proposal_id_and_approval_hint(service, client):
    text = await propose(service, client)
    assert field(text, "state") == "waiting for approval"
    assert field(text, "proposal_id") == "act-1"
    assert "never approve on your own" in text


async def test_typed_yes_cannot_approve_through_ari_ask(service, client):
    conversation_id = field(await propose(service, client), "conversation_id")
    with pytest.raises(UserFacingError, match="ari_approve"):
        await service.ask("alice", "yes", conversation_id=conversation_id)
    assert client.confirmed == []


async def test_approve_needs_the_proposal_id_that_is_waiting(service, client):
    conversation_id = field(await propose(service, client), "conversation_id")
    with pytest.raises(UserFacingError, match="does not match"):
        await service.approve("alice", conversation_id, "act-other", accept)
    assert client.confirmed == []


async def test_approve_confirms_and_reports_the_pull_request(service, client):
    conversation_id = field(await propose(service, client), "conversation_id")
    client.final["t1"] = _task(
        "t1",
        "TASK_STATE_COMPLETED",
        artifacts=[{"name": "pull_request", "parts": [{"data": {"url": "https://github.com/org/default/pull/7"}}]}],
        text="opened",
    )
    text = await service.approve("alice", conversation_id, "act-1", accept)
    assert client.confirmed[0]["id"] == "act-1"
    assert client.confirmed[0]["text"] == "approved by alice"
    assert "pull/7" in text
    assert "T" in accept.seen


async def test_declined_confirmation_leaves_the_proposal_waiting(service, client):
    conversation_id = field(await propose(service, client), "conversation_id")
    text = await service.approve("alice", conversation_id, "act-1", declined)
    assert client.confirmed == []
    assert "Not approved" in text
    assert field(text, "proposal_id") == "act-1"


async def test_required_confirmation_refuses_clients_that_cannot_ask(service, client):
    conversation_id = field(await propose(service, client), "conversation_id")
    with pytest.raises(UserFacingError, match="Slack, Teams or the ari CLI"):
        await service.approve("alice", conversation_id, "act-1", unsupported, require_confirmation=True)
    assert client.confirmed == []


async def test_unsupported_confirmation_is_allowed_when_not_required(service, client):
    conversation_id = field(await propose(service, client), "conversation_id")
    await service.approve("alice", conversation_id, "act-1", unsupported)
    assert client.confirmed[0]["id"] == "act-1"


async def test_another_user_cannot_see_or_act_on_a_conversation(service, client):
    conversation_id = field(await propose(service, client), "conversation_id")
    with pytest.raises(UserFacingError, match="belongs to alice"):
        await service.status("mallory", conversation_id)
    with pytest.raises(UserFacingError, match="belongs to alice"):
        await service.approve("mallory", conversation_id, "act-1", accept)
    with pytest.raises(UserFacingError, match="belongs to alice"):
        await service.ask("mallory", "also delete the tests", conversation_id=conversation_id)
    with pytest.raises(UserFacingError, match="belongs to alice"):
        await service.decline("mallory", conversation_id)
    assert client.confirmed == [] and client.canceled == []


async def test_configured_approver_can_approve_someone_elses_proposal(service, client):
    conversation_id = field(await propose(service, client), "conversation_id")
    await service.approve("carol", conversation_id, "act-1", accept)
    assert client.confirmed[0]["text"] == "approved by carol"


async def test_four_eyes_rule_stops_the_requester(client, settings):
    settings = settings.model_copy(update={"requester_can_approve": False})
    service = AriService(Bridge(client, settings, SessionStore()))
    conversation_id = field(await propose(service, client), "conversation_id")
    text = await service.approve("alice", conversation_id, "act-1", accept)
    assert text.startswith("Not done:")
    assert client.confirmed == []
    await service.approve("carol", conversation_id, "act-1", accept)
    assert client.confirmed[0]["text"] == "approved by carol"


async def test_decline_cancels_the_task(service, client):
    conversation_id = field(await propose(service, client), "conversation_id")
    text = await service.decline("alice", conversation_id)
    assert client.canceled == ["t1"]
    assert field(text, "state") == "canceled"


async def test_unknown_conversation_is_refused(service):
    with pytest.raises(UserFacingError, match="unknown or has expired"):
        await service.status("alice", "0" * 32)


async def test_long_run_returns_working_and_status_follows_it(service, client):
    async def slow_wait(task_id, **kwargs):
        raise TimeoutError("still working")

    client.wait = slow_wait
    text = await service.ask("alice", "fix the NPE")
    conversation_id = field(text, "conversation_id")
    assert field(text, "state") == "queued"
    assert "Call ari_status" in text
    del client.wait
    client.final["t1"] = _task("t1", "TASK_STATE_COMPLETED", text="fixed")
    followed = await service.status("alice", conversation_id)
    assert field(followed, "state") == "done"
    assert "fixed" in followed


async def test_progress_is_reported_while_following(service, client):
    seen = []

    async def progress(message):
        seen.append(message)

    await service.ask("alice", "hello", progress=progress)
    assert seen and seen[0].startswith("working")


async def test_a_second_call_while_one_runs_is_refused(service, client):
    import asyncio

    gate = asyncio.Event()
    original = client.wait

    async def blocked_wait(task_id, **kwargs):
        await gate.wait()
        return await original(task_id, **kwargs)

    client.wait = blocked_wait
    first = asyncio.create_task(service.ask("alice", "slow one"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    conversation_id = next(iter(service.bridge.store._conversations)).removeprefix("mcp:")
    with pytest.raises(UserFacingError, match="still running"):
        await service.decline("alice", conversation_id)
    gate.set()
    await first


async def test_agent_restart_is_reported_as_temporary(service, client):
    from chat_gateway.a2a_client import AgentError

    async def unavailable(*args, **kwargs):
        raise AgentError("message/send", 503, "no available server")

    client.send = unavailable
    with pytest.raises(UserFacingError, match="temporarily unavailable"):
        await service.ask("alice", "hello")


async def test_other_agent_errors_are_reported_as_refused(service, client):
    from chat_gateway.a2a_client import AgentError

    async def bad_request(*args, **kwargs):
        raise AgentError("message/send", 400, "bad config")

    client.send = bad_request
    with pytest.raises(UserFacingError, match="refused the request"):
        await service.ask("alice", "hello")
