import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chat_gateway import teams  # noqa: E402
from chat_gateway.a2a_client import AgentClient  # noqa: E402
from chat_gateway.bridge import Bridge  # noqa: E402
from chat_gateway.config import ConfigError, GatewaySettings  # noqa: E402
from chat_gateway.rendering import Button, Rendered  # noqa: E402
from chat_gateway.session import SessionStore  # noqa: E402
from tests.ari_cli_fakes import PENDING, PR, FakeAgent, _task  # noqa: E402

TENANT = "11111111-2222-3333-4444-555555555555"
APP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
THREAD = "19:abc@thread.tacv2;messageid=1700000000000"
REQUESTER = "0000aaaa-0000-0000-0000-000000000001"
OTHER = "0000aaaa-0000-0000-0000-000000000002"
BOT_NAME = "ARI Agent Test"


class FakePoster:
    def __init__(self):
        self.sent = []
        self.updates = []

    async def send(self, text="", card=None):
        activity_id = f"m{len(self.sent) + 1}"
        self.sent.append({"id": activity_id, "text": text, "card": card})
        return activity_id

    async def update(self, activity_id, text="", card=None):
        self.updates.append({"id": activity_id, "text": text, "card": card})

    def texts(self):
        return [s["text"] for s in self.sent if s["text"]]

    def cards(self):
        return [s for s in self.sent if s["card"]]

    def last_status(self, activity_id):
        found = [u["text"] for u in self.updates if u["id"] == activity_id and u["text"]]
        return found[-1] if found else None


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr("common.http_retry.backoff_seconds", lambda attempt: 0)
    return FakeAgent()


def make_gateway(agent, *, approvers="", allowed_teams="", allowed_users="", requester_can_approve=True, session_file=""):
    settings = GatewaySettings(
        ari_agent_url="http://agent.test",
        llm_api_key="sk-test",
        github_pat="ghp_test",
        default_repo_url="https://github.com/org/repo",
        poll_seconds=0,
        approvers=approvers,
        requester_can_approve=requester_can_approve,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(agent.handler), timeout=5)
    store = SessionStore(path=session_file or None)
    bridge = Bridge(AgentClient(settings.ari_agent_url, "", http=http), settings, store)
    return teams.TeamsGateway(
        bridge,
        teams.TeamsSettings(
            teams_app_id=APP,
            teams_app_password="secret",
            teams_tenant_id=TENANT,
            teams_progress_seconds=0,
            teams_allowed_teams=allowed_teams,
            teams_allowed_users=allowed_users,
        ),
    )


def message(text, *, user=REQUESTER, name="Alice", tenant=TENANT, conversation=THREAD, team="19:team@thread.tacv2", **extra):
    activity = {
        "type": "message",
        "text": f"<at>{BOT_NAME}</at> {text}",
        "from": {"id": "29:x", "aadObjectId": user, "name": name},
        "recipient": {"id": f"28:{APP}", "name": BOT_NAME},
        "conversation": {"id": conversation, "tenantId": tenant},
        "channelData": {"tenant": {"id": tenant}, "team": {"id": team, "aadGroupId": "group-1"}},
    }
    activity.update(extra)
    return activity


def click(poster, action, *, user=REQUESTER, name="Alice"):
    card_message = poster.cards()[-1]
    data = next(a["data"] for a in card_message["card"]["actions"] if a.get("data", {}).get("ari") == action)
    activity = message("", user=user, name=name, value=data, replyToId=card_message["id"])
    activity["text"] = ""
    return activity


def test_teams_markup_becomes_plain_text_for_the_agent():
    text = f"<at>{BOT_NAME}</at> fix &amp; ship<br>it for <at>Bob Smith</at>&nbsp;now <b>please</b>"
    assert teams.to_plain(text, BOT_NAME) == "fix & ship\nit for @Bob Smith now please"


def test_single_line_breaks_survive_teams_markdown_but_code_is_left_alone():
    text = "line one\nline two\n\npara\n```\na\nb\n```"
    assert teams.to_teams(text) == "line one\n\nline two\n\npara\n\n```\na\nb\n```"


def test_settings_refuse_ids_that_cannot_work():
    with pytest.raises(ConfigError, match="TEAMS_APP_ID"):
        teams.TeamsSettings(teams_app_id="bot", teams_app_password="x", teams_tenant_id=TENANT).check()
    with pytest.raises(ConfigError, match="TEAMS_APP_PASSWORD"):
        teams.TeamsSettings(teams_app_id=APP, teams_app_password=" ", teams_tenant_id=TENANT).check()
    with pytest.raises(ConfigError, match="TEAMS_TENANT_ID"):
        teams.TeamsSettings(teams_app_id=APP, teams_app_password="x", teams_tenant_id="contoso").check()
    teams.TeamsSettings(teams_app_id=APP, teams_app_password="x", teams_tenant_id=TENANT).check()


def test_a_proposal_card_carries_the_decision_and_the_diff_link():
    rendered = Rendered(
        text="Ready.",
        state="TASK_STATE_INPUT_REQUIRED",
        buttons=(Button("approve", "Approve", "task-1", "act-1"), Button("decline", "Decline", "task-1", "act-1")),
        links=("https://github.com/o/r/compare/a...b",),
    )
    card = teams.proposal_card(rendered, THREAD, REQUESTER)
    approve, decline, link = card["actions"]
    assert approve["type"] == "Action.Submit" and approve["style"] == "positive"
    assert teams.read_decision(approve["data"]) == {"ari": "approve", "k": THREAD, "t": "task-1", "p": "act-1", "r": REQUESTER}
    assert decline["data"]["ari"] == "decline" and decline["style"] == "destructive"
    assert link == {"type": "Action.OpenUrl", "title": "Full diff", "url": "https://github.com/o/r/compare/a...b"}
    assert teams.read_decision({"ari": "merge"}) == {}
    assert teams.read_decision("approve") == {}
    closed = teams.closed_card(card, "Approved by Alice")
    assert [a["type"] for a in closed["actions"]] == ["Action.OpenUrl"]
    assert closed["body"][-1]["text"] == "Approved by Alice"


@pytest.mark.asyncio
async def test_a_question_is_answered_in_the_conversation(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_WORKING"), _task("task-1", "TASK_STATE_COMPLETED", text="A **deadlock** is...\nmore"))
    gateway, poster = make_gateway(agent), FakePoster()
    await gateway.handle(message("What is a deadlock?"), poster)
    assert agent.sends[0]["message"]["parts"][0]["text"] == "What is a deadlock?"
    assert poster.texts() == ["⏳ Working on it...", "A **deadlock** is...\n\nmore"]
    assert poster.last_status("m1") == "✅ Done (task task-1)"


@pytest.mark.asyncio
async def test_another_tenant_and_other_activity_types_are_ignored(agent):
    gateway, poster = make_gateway(agent), FakePoster()
    await gateway.handle(message("hi", tenant="99999999-2222-3333-4444-555555555555"), poster)
    await gateway.handle({**message("hi"), "type": "conversationUpdate"}, poster)
    assert poster.sent == [] and agent.sends == []


@pytest.mark.asyncio
async def test_help_and_status_answer_without_starting_a_task(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="answer"))
    gateway, poster = make_gateway(agent), FakePoster()
    await gateway.handle(message(""), poster)
    assert poster.texts() == [teams.HELP_TEXT]
    await gateway.handle(message("question"), poster)
    await gateway.handle(message("status"), poster)
    assert poster.texts()[-1] == "✅ Done (task task-1)"
    assert len(agent.sends) == 1


@pytest.mark.asyncio
async def test_a_proposal_is_approved_with_the_card_button(agent):
    agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready to open a pull request."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened the pull request."),
    )
    gateway, poster = make_gateway(agent), FakePoster()
    await gateway.handle(message("fix the bug"), poster)
    assert poster.last_status("m1") == "✋ Waiting for approval (task task-1)"
    card = poster.cards()[-1]["card"]
    assert "**Repository:** https://github.com/org/other" in card["body"][1]["text"]
    await gateway.handle(click(poster, "approve"), poster)
    confirm = agent.sends[1]["message"]
    assert confirm["task_id"] == "task-1"
    assert confirm["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}
    closed = next(u for u in poster.updates if u["card"])
    assert closed["id"] == "m2"
    assert not [a for a in closed["card"]["actions"] if a["type"] == "Action.Submit"]
    assert closed["card"]["body"][-1]["text"] == "Approved by Alice"
    assert "Pull request: https://github.com/org/repo/pull/9" in poster.texts()[-1]


@pytest.mark.asyncio
async def test_a_proposal_can_be_declined(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    gateway, poster = make_gateway(agent), FakePoster()
    await gateway.handle(message("fix the bug"), poster)
    agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."), _task("task-1", "TASK_STATE_CANCELED"))
    await gateway.handle(click(poster, "decline"), poster)
    assert agent.cancels == ["task-1"]
    closed = next(u for u in poster.updates if u["card"])
    assert closed["card"]["body"][-1]["text"] == "Declined by Alice"


@pytest.mark.asyncio
async def test_four_eyes_refuses_the_requester_and_names_who_must_approve(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    gateway, poster = make_gateway(agent, approvers=OTHER, requester_can_approve=False), FakePoster()
    await gateway.handle(message("fix the bug"), poster)
    card = poster.cards()[-1]["card"]
    assert card["body"][-1]["text"] == "Needs approval from a configured approver other than the person who asked."
    await gateway.handle(click(poster, "approve"), poster)
    assert len(agent.sends) == 1
    assert not [u for u in poster.updates if u["card"]]
    assert "someone else has to approve it" in json.dumps(poster.updates + poster.sent)


@pytest.mark.asyncio
async def test_people_outside_the_allow_lists_are_told_no(agent):
    gateway, poster = make_gateway(agent, allowed_users=OTHER), FakePoster()
    await gateway.handle(message("fix it"), poster)
    assert poster.texts() == ["⚠️ I am not enabled for you here."]
    gateway, poster = make_gateway(agent, allowed_teams="group-1", allowed_users=REQUESTER.upper()), FakePoster()
    agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="ok"))
    await gateway.handle(message("fix it"), poster)
    assert poster.texts()[-1] == "ok"
    gateway, poster = make_gateway(agent, allowed_teams="another-team"), FakePoster()
    await gateway.handle(message("fix it", conversation="a:personal-chat", team=None), poster)
    assert poster.texts() == ["⚠️ I am not enabled for you here."]


@pytest.mark.asyncio
async def test_a_restarted_gateway_continues_the_conversation(agent, tmp_path):
    path = tmp_path / "sessions.json"
    agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    first, poster = make_gateway(agent, session_file=str(path)), FakePoster()
    await first.handle(message("fix the bug"), poster)
    second = make_gateway(agent, session_file=str(path))
    await second.handle(message("yes"), poster)
    confirm = agent.sends[1]["message"]
    assert confirm["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}
    assert confirm["parts"][1]["data"]["repo"]["pat"] == "ghp_test"


@pytest.mark.asyncio
async def test_a_failure_inside_the_handler_is_logged_not_raised(agent, caplog):
    class Broken(FakePoster):
        async def send(self, text="", card=None):
            raise RuntimeError("teams is down")

    gateway = make_gateway(agent)
    await gateway.handle(message(""), Broken())
    assert "teams gateway error" in caplog.text


def test_the_entry_point_knows_both_platforms(monkeypatch, capsys):
    from chat_gateway import __main__ as entry

    assert entry.main(["discord"]) == 2
    assert "unknown chat platform 'discord'" in capsys.readouterr().err
    monkeypatch.setitem(sys.modules, "microsoft_agents.hosting.aiohttp", None)
    assert entry.main(["teams"]) == 2
    assert "needs the 'teams' extra" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_sdk_wiring_hands_message_activities_to_the_gateway(agent):
    pytest.importorskip("microsoft_agents.hosting.aiohttp")
    from microsoft_agents.activity import Activity

    gateway = make_gateway(agent)
    seen = []

    async def handle(data, poster):
        seen.append((data, poster))

    gateway.handle = handle
    sdk_agent = teams.TeamsAgent(gateway, adapter=object(), app_id=APP)

    class Context:
        def __init__(self, activity):
            self.activity = activity

    activity = Activity.model_validate({**message("hi"), "serviceUrl": "https://smba.trafficmanager.net/amer/", "channelId": "msteams"})
    await sdk_agent.on_turn(Context(activity))
    await sdk_agent.on_turn(Context(Activity.model_validate({**message("hi"), "type": "typing", "channelId": "msteams"})))
    for task in list(sdk_agent.tasks):
        await task
    assert len(seen) == 1
    data, poster = seen[0]
    assert data["from"]["aadObjectId"] == REQUESTER and data["conversation"]["id"] == THREAD
    assert poster.reference.conversation.id == THREAD
    sendable = teams.ProactivePoster._activity("hi", {"type": "AdaptiveCard"})
    assert sendable.attachments[0].content_type == "application/vnd.microsoft.card.adaptive"


def test_the_app_package_has_a_valid_manifest_and_icons(tmp_path):
    import struct
    import zipfile

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import teams_app_package

    assert teams_app_package.main(["--bot-id", "nope", "--out", str(tmp_path / "x.zip")]) == 2
    assert teams_app_package.main(["--bot-id", APP, "--out", str(tmp_path / "app.zip")]) == 0
    with zipfile.ZipFile(tmp_path / "app.zip") as archive:
        assert sorted(archive.namelist()) == ["color.png", "manifest.json", "outline.png"]
        manifest = json.loads(archive.read("manifest.json"))
        sizes = {name: struct.unpack(">II", archive.read(name)[16:24]) for name in ("color.png", "outline.png")}
    assert manifest["id"] == APP and manifest["bots"][0]["botId"] == APP
    assert len(manifest["name"]["short"]) <= 30 and len(manifest["description"]["short"]) <= 80
    assert sizes == {"color.png": (192, 192), "outline.png": (32, 32)}
