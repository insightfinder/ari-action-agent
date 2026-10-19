import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chat_gateway import slack  # noqa: E402
from chat_gateway.a2a_client import AgentClient  # noqa: E402
from chat_gateway.bridge import Bridge  # noqa: E402
from chat_gateway.config import ConfigError, GatewaySettings  # noqa: E402
from chat_gateway.rendering import Button, Rendered  # noqa: E402
from tests.ari_cli_fakes import PENDING, PR, FakeAgent, _task  # noqa: E402

CHANNEL = "C1"
BOT = "UBOT"
REQUESTER = "U1"


class FakeSlack:
    def __init__(self):
        self.posts = []
        self.updates = []
        self.ephemeral = []
        self.counter = 0

    async def chat_postMessage(self, **kwargs):
        self.counter += 1
        ts = f"200.{self.counter}"
        self.posts.append({**kwargs, "ts": ts})
        return {"ok": True, "ts": ts}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs):
        self.ephemeral.append(kwargs)
        return {"ok": True}

    async def conversations_replies(self, channel, ts, limit=100):
        self.reads = getattr(self, "reads", 0) + 1
        bot_posts = [{"user": BOT, "ts": p["ts"]} for p in self.posts if p.get("channel") == channel and p.get("thread_ts") == ts]
        return {"ok": True, "messages": [{"user": REQUESTER, "ts": ts}, *bot_posts]}

    def texts(self):
        return [p["text"] for p in self.posts]

    def last_status(self, ts):
        found = [u["text"] for u in self.updates if u["ts"] == ts]
        return found[-1] if found else None


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr("common.http_retry.backoff_seconds", lambda attempt: 0)
    return FakeAgent()


def make_gateway(agent, *, approvers="", allowed_channels="", allowed_users="", requester_can_approve=True):
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
    bridge = Bridge(AgentClient(settings.ari_agent_url, "", http=http), settings)
    gateway = slack.SlackGateway(
        bridge,
        slack.SlackSettings(
            slack_app_token="xapp-1",
            slack_bot_token="xoxb-1",
            slack_progress_seconds=0,
            slack_allowed_channels=allowed_channels,
            slack_allowed_users=allowed_users,
        ),
    )
    gateway.bot_user_id = BOT
    return gateway


def mention(text, ts="100.1", user=REQUESTER, thread_ts=None):
    event = {"type": "app_mention", "channel": CHANNEL, "user": user, "text": f"<@{BOT}> {text}", "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def click(client, action_id, user=REQUESTER):
    message = next(p for p in reversed(client.posts) if p.get("blocks"))
    actions = next(b for b in message["blocks"] if b["type"] == "actions")
    element = next(e for e in actions["elements"] if e.get("action_id") == action_id)
    return {
        "actions": [{"action_id": action_id, "value": element["value"]}],
        "user": {"id": user},
        "channel": {"id": CHANNEL},
        "message": {"ts": message["ts"], "thread_ts": message.get("thread_ts"), "text": message["text"], "blocks": message["blocks"]},
    }


def test_slack_markup_becomes_plain_text_for_the_agent():
    text = (
        f"<@{BOT}> fix <https://example.com/a?b=1&amp;c=2|the page> for <@U2|alice> in <#C9|ops> "
        "repo=<https://github.com/org/x|github.com/org/x> &lt;b&gt; <!here>"
    )
    assert slack.to_plain(text, BOT) == (
        "fix https://example.com/a?b=1&c=2 for @alice in #ops repo=github.com/org/x <b> @here"
    )


@pytest.mark.asyncio
async def test_a_repository_slack_turned_into_a_link_still_reaches_the_agent_as_https(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="ok"))
    gateway, client = make_gateway(agent), FakeSlack()
    gateway.bridge.settings.allowed_repo_hosts = "github.com"
    await gateway.handle_mention(mention("look repo=<http://github.com/org/x|github.com/org/x>"), client)
    sent = agent.sends[0]["message"]
    assert sent["parts"][0]["text"] == "look"
    assert sent["parts"][1]["data"]["repo"]["url"] == "https://github.com/org/x"


def test_agent_markdown_becomes_slack_mrkdwn():
    text = "## Root cause\n**Null** check in [the PR](https://github.com/o/r/pull/1) for a<b & c\n```\n**not bold** <x>\n```"
    assert slack.to_mrkdwn(text) == (
        "*Root cause*\n*Null* check in <https://github.com/o/r/pull/1|the PR> for a&lt;b &amp; c\n"
        "```\n**not bold** &lt;x&gt;\n```"
    )


def test_long_text_is_split_on_lines_without_losing_any():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = slack.chunks(text, 1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text
    assert slack.chunks("", 10) == [""]
    assert slack.chunks("a" * 25, 10) == ["a" * 10, "a" * 10, "a" * 5]


def test_a_proposal_renders_as_sections_and_decision_buttons():
    rendered = Rendered(
        text="Ready.\n\n" + "y" * 5000,
        state="TASK_STATE_INPUT_REQUIRED",
        buttons=(Button("approve", "Approve", "task-1", "act-1"), Button("decline", "Decline", "task-1", "act-1")),
        links=("https://github.com/o/r/compare/a...b",),
    )
    blocks = slack.blocks_for(rendered, "C1:100.1")
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == 2 and all(len(b["text"]["text"]) <= 3000 for b in sections)
    approve, decline, link = blocks[-1]["elements"]
    assert approve["action_id"] == "ari_approve" and approve["style"] == "primary" and "confirm" in approve
    assert decline["action_id"] == "ari_decline" and decline["style"] == "danger"
    assert link["url"] == "https://github.com/o/r/compare/a...b"
    assert slack.read_button_value(approve["value"]) == {"k": "C1:100.1", "t": "task-1", "p": "act-1", "r": ""}
    assert slack.read_button_value("not json") == {}


@pytest.mark.asyncio
async def test_a_question_is_answered_in_the_thread(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_WORKING"), _task("task-1", "TASK_STATE_COMPLETED", text="A **deadlock** is..."))
    gateway, client = make_gateway(agent), FakeSlack()
    await gateway.handle_mention(mention("What is a deadlock?"), client)
    assert agent.sends[0]["message"]["parts"][0]["text"] == "What is a deadlock?"
    assert [p["thread_ts"] for p in client.posts] == ["100.1", "100.1"]
    assert client.texts() == [":hourglass_flowing_sand: Working on it...", "A *deadlock* is..."]
    assert client.last_status("200.1") == ":white_check_mark: Done (task task-1)"


@pytest.mark.asyncio
async def test_a_proposal_waits_for_the_requester_to_approve_with_a_button(agent):
    agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready to open a pull request."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened the pull request."),
    )
    gateway, client = make_gateway(agent), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    assert client.last_status("200.1") == ":raised_hand: Waiting for approval (task task-1)"
    proposal_post = client.posts[-1]
    assert proposal_post["text"] == (
        "Ready to open a pull request.\n\n"
        "*Repository:* https://github.com/org/other\n"
        "*Branch:* ari/fix-it → dev"
    )
    assert "Full diff:" not in json.dumps(proposal_post["blocks"])
    await gateway.handle_action(click(client, "ari_approve"), client)
    confirm = agent.sends[1]["message"]
    assert confirm["task_id"] == "task-1"
    assert confirm["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}
    closed = next(u for u in client.updates if u.get("blocks"))
    assert closed["ts"] == "200.2"
    assert not [b for b in closed["blocks"] if b["type"] == "actions"]
    assert closed["blocks"][-1]["elements"][0]["text"] == f"Approved by <@{REQUESTER}>"
    assert "Pull request: https://github.com/org/repo/pull/9" in client.posts[-1]["text"]


@pytest.mark.asyncio
async def test_someone_else_cannot_approve_and_the_buttons_stay(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    gateway, client = make_gateway(agent), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    await gateway.handle_action(click(client, "ari_approve", user="U2"), client)
    assert len(agent.sends) == 1
    assert not [u for u in client.updates if u.get("blocks")]
    assert "only U1 or a configured approver can approve this" in client.updates[-1]["text"]


@pytest.mark.asyncio
async def test_a_configured_approver_can_approve_someone_elses_proposal(agent):
    agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    gateway, client = make_gateway(agent, approvers="U9"), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    await gateway.handle_action(click(client, "ari_approve", user="U9"), client)
    assert len(agent.sends) == 2
    assert "Pull request: https://github.com/org/repo/pull/9" in client.posts[-1]["text"]


@pytest.mark.asyncio
async def test_decline_cancels_the_task_and_closes_the_buttons(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    gateway, client = make_gateway(agent), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    await gateway.handle_action(click(client, "ari_decline"), client)
    assert agent.cancels == ["task-1"]
    closed = next(u for u in client.updates if u.get("blocks"))
    assert closed["blocks"][-1]["elements"][0]["text"] == f"Declined by <@{REQUESTER}>"


@pytest.mark.asyncio
async def test_replies_in_the_thread_continue_the_conversation_without_a_mention(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="first answer"))
    agent.script("task-2", _task("task-2", "TASK_STATE_COMPLETED", text="second answer"))
    gateway, client = make_gateway(agent), FakeSlack()
    await gateway.handle_mention(mention("first question"), client)
    reply = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "text": "and then?", "ts": "100.5", "thread_ts": "100.1"}
    await gateway.handle_message(reply, client)
    assert len(agent.sends) == 2
    assert agent.sends[1]["message"]["context_id"] == "ctx-1"
    assert client.posts[-1]["text"] == "second answer"


@pytest.mark.asyncio
async def test_a_yes_in_the_thread_approves_the_waiting_proposal(agent):
    agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    gateway, client = make_gateway(agent), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    reply = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "text": "yes", "ts": "100.6", "thread_ts": "100.1"}
    await gateway.handle_message(reply, client)
    assert agent.sends[1]["message"]["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}


@pytest.mark.asyncio
async def test_messages_the_gateway_must_not_answer(agent):
    gateway, client = make_gateway(agent), FakeSlack()
    base = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "ts": "100.9"}
    await gateway.handle_message({**base, "text": "chatting with colleagues"}, client)
    await gateway.handle_message({**base, "text": "unknown thread", "thread_ts": "99.1"}, client)
    await gateway.handle_message({**base, "text": f"<@{BOT}> handled by app_mention", "thread_ts": "99.1"}, client)
    await gateway.handle_message({**base, "text": "edited", "subtype": "message_changed"}, client)
    await gateway.handle_message({**base, "text": "from a bot", "bot_id": "B1"}, client)
    assert client.posts == [] and agent.sends == []


@pytest.mark.asyncio
async def test_a_direct_message_is_a_conversation_of_its_own(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="hello there"))
    gateway, client = make_gateway(agent), FakeSlack()
    dm = {"type": "message", "channel": "D1", "channel_type": "im", "user": REQUESTER, "text": f"<@{BOT}> hi", "ts": "100.2"}
    await gateway.handle_message(dm, client)
    assert agent.sends[0]["message"]["parts"][0]["text"] == "hi"
    assert all("thread_ts" not in p for p in client.posts)
    assert gateway.bridge.store.get("D1:dm") is not None


@pytest.mark.asyncio
async def test_channel_and_user_allow_lists(agent):
    gateway, client = make_gateway(agent, allowed_channels="C7", allowed_users="U1"), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    assert client.texts() == [":warning: I am not enabled for you in this channel."]
    gateway2, client2 = make_gateway(agent, allowed_channels="C1", allowed_users="U5"), FakeSlack()
    await gateway2.handle_mention(mention("fix the bug"), client2)
    assert client2.texts() == [":warning: I am not enabled for you in this channel."]
    assert agent.sends == []


@pytest.mark.asyncio
async def test_help_and_status_are_answered_without_starting_a_task(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    gateway, client = make_gateway(agent), FakeSlack()
    await gateway.handle_mention(mention(""), client)
    await gateway.handle_mention(mention("help"), client)
    assert client.texts() == [slack.HELP_TEXT, slack.HELP_TEXT]
    await gateway.handle_mention(mention("status"), client)
    assert client.texts()[-1] == ":warning: no task in this conversation yet"
    await gateway.handle_mention(mention("fix the bug"), client)
    sends = len(agent.sends)
    await gateway.handle_mention(mention("status", ts="100.7", thread_ts="100.1"), client)
    assert len(agent.sends) == sends
    assert client.posts[-1].get("blocks")


@pytest.mark.asyncio
async def test_a_second_message_while_the_thread_is_busy_is_turned_away(agent):
    gateway, client = make_gateway(agent), FakeSlack()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_start(thread_key, text, requester, on_update=None):
        started.set()
        await release.wait()
        return Rendered(text="done", state="TASK_STATE_COMPLETED")

    gateway.bridge.start = slow_start
    first = asyncio.create_task(gateway.handle_mention(mention("one"), client))
    await started.wait()
    await gateway.handle_mention(mention("two", ts="100.3", thread_ts="100.1"), client)
    release.set()
    await first
    assert ":hourglass_flowing_sand: I am still on the previous message in this thread." in client.texts()


@pytest.mark.asyncio
async def test_errors_are_reported_in_the_thread_not_raised(agent, monkeypatch):
    gateway, client = make_gateway(agent), FakeSlack()

    async def config_error(*args, **kwargs):
        raise ConfigError("no repository: pass repo=<url> in the message or set DEFAULT_REPO_URL")

    gateway.bridge.start = config_error
    await gateway.handle_mention(mention("fix it"), client)
    assert client.last_status("200.1") == ":warning: no repository: pass repo=&lt;url&gt; in the message or set DEFAULT_REPO_URL"

    async def boom(*args, **kwargs):
        raise ValueError("secret detail")

    gateway.bridge.start = boom
    await gateway.handle_mention(mention("fix it", ts="300.1"), client)
    assert "secret detail" not in json.dumps(client.updates)
    assert "something went wrong in the gateway" in client.updates[-1]["text"]


@pytest.mark.asyncio
async def test_progress_edits_are_throttled_and_skip_repeats():
    client = FakeSlack()
    now = [0.0]
    progress = slack.ProgressMessage(client, "C1", "200.1", interval=5, clock=lambda: now[0])
    await progress.set("a")
    await progress.set("b")
    now[0] = 6
    await progress.set("b")
    await progress.set("b")
    await progress.set("c", force=True)
    assert [u["text"] for u in client.updates] == ["a", "b", "c"]


def test_tokens_are_checked_by_their_prefix():
    slack.SlackSettings(slack_app_token=" xapp-1 ", slack_bot_token="xoxb-1").check()
    with pytest.raises(ConfigError, match="xapp-"):
        slack.SlackSettings(slack_app_token="xoxb-1", slack_bot_token="xoxb-1").check()
    with pytest.raises(ConfigError, match="xoxb-"):
        slack.SlackSettings(slack_app_token="xapp-1", slack_bot_token="xoxp-1").check()


def test_the_bolt_app_wires_every_listener():
    pytest.importorskip("slack_bolt")
    pytest.importorskip("aiohttp")
    gateway = slack.SlackGateway(None, slack.SlackSettings(slack_app_token="xapp-1", slack_bot_token="xoxb-1"))
    app = slack.build_app(gateway, gateway.settings)
    assert app is not None


def test_the_entry_point_explains_a_missing_slack_extra(monkeypatch, capsys):
    from chat_gateway import __main__ as entry

    monkeypatch.setitem(sys.modules, "slack_bolt", None)
    assert entry.main([]) == 2
    assert "needs the 'slack' extra" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_leftover_decline_button_cannot_cancel_the_next_task(agent):
    agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    gateway, client = make_gateway(agent), FakeSlack()
    await gateway.handle_mention(mention("fix bug A"), client)
    old_decline = click(client, "ari_decline")
    reply = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "ts": "100.6", "thread_ts": "100.1"}
    await gateway.handle_message({**reply, "text": "yes"}, client)
    second = [{"name": "pending_action", "parts": [{"data": {"id": "act-2", "title": "Fix B", "compare_url": "https://x/c"}}]}]
    agent.script("task-2", _task("task-2", "TASK_STATE_INPUT_REQUIRED", second, "Ready again."))
    await gateway.handle_message({**reply, "ts": "100.7", "text": "now fix bug B"}, client)
    await gateway.handle_action(old_decline, client)
    assert agent.cancels == []
    assert "it may be stale" in client.updates[-1]["text"]


@pytest.mark.asyncio
async def test_people_outside_the_allow_list_chatting_in_a_bot_thread_are_ignored_quietly(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="answer"))
    gateway, client = make_gateway(agent, allowed_users="U1"), FakeSlack()
    await gateway.handle_mention(mention("question"), client)
    posts = len(client.posts)
    chatter = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": "U2", "text": "thanks!", "ts": "100.8", "thread_ts": "100.1"}
    await gateway.handle_message(chatter, client)
    assert len(client.posts) == posts and len(agent.sends) == 1


@pytest.mark.asyncio
async def test_a_message_with_a_file_or_sent_to_the_channel_too_is_still_answered(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="first"))
    agent.script("task-2", _task("task-2", "TASK_STATE_COMPLETED", text="second"))
    gateway, client = make_gateway(agent), FakeSlack()
    dm = {"type": "message", "subtype": "file_share", "channel": "D1", "channel_type": "im", "user": REQUESTER, "text": "look at this log", "ts": "1.1"}
    await gateway.handle_message(dm, client)
    await gateway.handle_mention(mention("question"), client)
    broadcast = {"type": "message", "subtype": "thread_broadcast", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "text": "more", "ts": "100.9", "thread_ts": "100.1"}
    agent.script("task-3", _task("task-3", "TASK_STATE_COMPLETED", text="third"))
    await gateway.handle_message(broadcast, client)
    assert len(agent.sends) == 3


@pytest.mark.asyncio
async def test_a_wait_that_runs_out_tells_the_user_how_to_check_later(agent):
    gateway, client = make_gateway(agent), FakeSlack()

    async def too_long(*args, **kwargs):
        raise TimeoutError("task task-1 still TASK_STATE_WORKING after 3600s; the run continues on the agent")

    gateway.bridge.start = too_long
    await gateway.handle_mention(mention("big job"), client)
    assert client.updates[-1]["text"] == (
        ":warning: task task-1 still TASK_STATE_WORKING after 3600s; the run continues on the agent. Say `status` to check on it."
    )


@pytest.mark.asyncio
async def test_buttons_in_a_direct_message_answer_in_the_conversation_not_a_new_thread(agent):
    agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    gateway, client = make_gateway(agent), FakeSlack()
    dm = {"type": "message", "channel": "D1", "channel_type": "im", "user": REQUESTER, "text": "fix it", "ts": "1.1"}
    await gateway.handle_message(dm, client)
    message = next(p for p in reversed(client.posts) if p.get("blocks"))
    element = next(e for e in next(b for b in message["blocks"] if b["type"] == "actions")["elements"] if e.get("action_id") == "ari_approve")
    body = {"actions": [{"action_id": "ari_approve", "value": element["value"]}], "user": {"id": REQUESTER}, "channel": {"id": "D1"}, "message": {"ts": message["ts"], "text": message["text"], "blocks": message["blocks"]}}
    await gateway.handle_action(body, client)
    assert all("thread_ts" not in p for p in client.posts)
    assert "Pull request: https://github.com/org/repo/pull/9" in client.posts[-1]["text"]


def test_text_slack_turned_into_links_keeps_its_original_words():
    assert slack.to_plain("check <http://setup.py|setup.py> and mail <mailto:a@b.co|a@b.co>") == "check setup.py and mail a@b.co"
    assert slack.to_plain("<https://github.com/o/r|the repo>") == "https://github.com/o/r"


def test_inline_code_is_left_as_written():
    assert slack.to_mrkdwn("run `a**b**c` then **go** with `<x>`") == "run `a**b**c` then *go* with `&lt;x&gt;`"


def test_a_code_block_split_across_messages_is_closed_and_reopened():
    text = "intro\n```\n" + "\n".join(f"line {i}" for i in range(300)) + "\n```\nend"
    parts = slack.chunks(text, 500)
    assert len(parts) > 2
    assert all(part.count("```") % 2 == 0 for part in parts)
    assert all(len(part) <= 504 for part in parts)


def test_help_text_shows_its_placeholders():
    assert "<" not in slack.HELP_TEXT and "repo=&lt;url&gt;" in slack.HELP_TEXT


@pytest.mark.asyncio
async def test_status_after_an_answer_is_one_line_not_the_answer_again(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="A long answer about certificates."))
    gateway, client = make_gateway(agent), FakeSlack()
    await gateway.handle_mention(mention("is github.com ok?"), client)
    await gateway.handle_mention(mention("status", ts="100.4", thread_ts="100.1"), client)
    assert client.texts()[-1] == ":white_check_mark: Done (task task-1)"
    assert client.texts().count("A long answer about certificates.") == 1


@pytest.mark.asyncio
async def test_status_while_running_says_what_the_agent_is_doing(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_WORKING"))
    gateway, client = make_gateway(agent), FakeSlack()
    gateway.bridge.store.start("C1:100.1", REQUESTER)
    gateway.bridge.store.record_task("C1:100.1", _task("task-1", "TASK_STATE_WORKING"))
    await gateway.handle_mention(mention("status", ts="100.4", thread_ts="100.1"), client)
    assert client.texts() == [":hourglass_flowing_sand: working (task task-1)"]


@pytest.mark.asyncio
async def test_declining_does_not_repeat_the_canceled_line_as_a_second_message(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    gateway, client = make_gateway(agent), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    posts_before = len(client.posts)
    await gateway.handle_action(click(client, "ari_decline"), client)
    new_posts = client.texts()[posts_before:]
    assert new_posts == [f":hourglass_flowing_sand: <@{REQUESTER}> declined. Canceling the task..."]
    assert client.last_status(client.posts[posts_before]["ts"]) == ":no_entry_sign: Canceled (task task-1)"


def test_a_proposal_without_the_agents_own_words_falls_back_to_the_full_card():
    task = _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING)
    rendered = Rendered(text="The agent is waiting for your approval.\n\nProposed: Open pull request", state="TASK_STATE_INPUT_REQUIRED", task=task)
    assert slack.proposal_text(rendered) == rendered.text


def test_proposal_details_list_files_and_test_status():
    action = [{"name": "pending_action", "parts": [{"data": {
        "id": "a", "repo": "org/r", "branch": "b", "base_ref": "main",
        "changed_files": [f"f{i}.py" for i in range(10)], "verification": {"status": "passed"},
    }}]}]
    rendered = Rendered(text="x", state="TASK_STATE_INPUT_REQUIRED", task=_task("t", "TASK_STATE_INPUT_REQUIRED", action, "Ready."))
    assert slack.proposal_text(rendered) == (
        "Ready.\n\n**Repository:** org/r\n**Branch:** b → main\n"
        "**Files:** f0.py, f1.py, f2.py, f3.py, f4.py, f5.py, f6.py, f7.py (+2 more)\n**Tests:** passed"
    )


def proposal_then_pr(agent):
    agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )


@pytest.mark.asyncio
async def test_with_four_eyes_the_requester_cannot_approve_their_own_proposal(agent):
    proposal_then_pr(agent)
    gateway, client = make_gateway(agent, approvers="U1,U9", requester_can_approve=False), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    proposal = next(p for p in client.posts if p.get("blocks"))
    context = next(b for b in proposal["blocks"] if b["type"] == "context")
    assert context["elements"][0]["text"] == "Needs approval from <@U9>"
    await gateway.handle_action(click(client, "ari_approve", user=REQUESTER), client)
    assert len(agent.sends) == 1
    assert "you asked for this change, so someone else has to approve it" in client.updates[-1]["text"]
    reply = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "text": "yes", "ts": "100.6", "thread_ts": "100.1"}
    await gateway.handle_message(reply, client)
    assert len(agent.sends) == 1
    await gateway.handle_action(click(client, "ari_approve", user="U9"), client)
    assert agent.sends[1]["message"]["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}
    assert "Pull request: https://github.com/org/repo/pull/9" in client.posts[-1]["text"]


@pytest.mark.asyncio
async def test_with_four_eyes_someone_outside_the_approvers_still_cannot_approve(agent):
    proposal_then_pr(agent)
    gateway, client = make_gateway(agent, approvers="U9", requester_can_approve=False), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    await gateway.handle_action(click(client, "ari_approve", user="U5"), client)
    assert len(agent.sends) == 1
    assert "only a configured approver can approve this" in client.updates[-1]["text"]


@pytest.mark.asyncio
async def test_with_four_eyes_the_requester_can_still_withdraw_their_request(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    gateway, client = make_gateway(agent, approvers="U9", requester_can_approve=False), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    await gateway.handle_action(click(client, "ari_decline", user=REQUESTER), client)
    assert agent.cancels == ["task-1"]


@pytest.mark.asyncio
async def test_with_four_eyes_a_restart_does_not_let_the_requester_approve(agent):
    proposal_then_pr(agent)
    gateway, client = make_gateway(agent, approvers="U1,U9", requester_can_approve=False), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    body = click(client, "ari_approve", user=REQUESTER)
    gateway.bridge.store.forget("C1:100.1")
    await gateway.handle_action(body, client)
    assert len(agent.sends) == 1
    assert "someone else has to approve it" in client.updates[-1]["text"]


@pytest.mark.asyncio
async def test_with_four_eyes_and_no_other_approver_the_proposal_says_so(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    gateway, client = make_gateway(agent, approvers="U1", requester_can_approve=False), FakeSlack()
    await gateway.handle_mention(mention("fix the bug"), client)
    proposal = next(p for p in client.posts if p.get("blocks"))
    context = next(b for b in proposal["blocks"] if b["type"] == "context")
    assert context["elements"][0]["text"].startswith("No one other than the requester is configured to approve this")


def test_four_eyes_without_any_approver_refuses_to_start():
    GatewaySettings(requester_can_approve=False, approvers="U9").check_approval_policy()
    GatewaySettings().check_approval_policy()
    with pytest.raises(ConfigError, match="REQUESTER_CAN_APPROVE is false but APPROVERS is empty"):
        GatewaySettings(requester_can_approve=False).check_approval_policy()


def restarted(agent, path, client=None):
    gateway = make_gateway(agent)
    gateway.bridge.store = slack.SessionStore(path=path)
    return gateway


@pytest.mark.asyncio
async def test_a_restarted_gateway_picks_up_the_thread_where_it_left_off(agent, tmp_path):
    path = tmp_path / "sessions.json"
    agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    first, client = restarted(agent, path), FakeSlack()
    await first.handle_mention(mention("fix the bug"), client)
    saved = json.loads(path.read_text())
    assert saved["conversations"][0]["task_id"] == "task-1"
    assert "config" not in saved["conversations"][0]
    assert "sk-test" not in path.read_text() and "ghp_test" not in path.read_text()
    second = restarted(agent, path)
    reply = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "text": "yes", "ts": "100.6", "thread_ts": "100.1"}
    await second.handle_message(reply, client)
    confirm = agent.sends[1]["message"]
    assert confirm["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}
    assert confirm["parts"][1]["data"]["repo"]["pat"] == "ghp_test"
    assert "Pull request: https://github.com/org/repo/pull/9" in client.posts[-1]["text"]


@pytest.mark.asyncio
async def test_status_works_in_a_thread_after_a_restart(agent, tmp_path):
    path = tmp_path / "sessions.json"
    agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="answer"))
    first, client = restarted(agent, path), FakeSlack()
    await first.handle_mention(mention("question"), client)
    second = restarted(agent, path)
    reply = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "text": "status", "ts": "100.6", "thread_ts": "100.1"}
    await second.handle_message(reply, client)
    assert client.texts()[-1] == ":white_check_mark: Done (task task-1)"


@pytest.mark.asyncio
async def test_a_thread_the_gateway_forgot_gets_one_hint_not_silence(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="answer"))
    first, client = make_gateway(agent), FakeSlack()
    await first.handle_mention(mention("question"), client)
    forgetful = make_gateway(agent)
    reply = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "text": "what is the status?", "ts": "100.6", "thread_ts": "100.1"}
    await forgetful.handle_message(reply, client)
    await forgetful.handle_message({**reply, "ts": "100.7"}, client)
    assert client.texts().count(slack.FORGOTTEN_TEXT) == 1
    assert client.posts[-1]["thread_ts"] == "100.1"
    assert len(agent.sends) == 1


@pytest.mark.asyncio
async def test_threads_the_bot_never_joined_stay_silent_and_are_read_once(agent):
    gateway, client = make_gateway(agent), FakeSlack()
    reply = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "text": "team chatter", "ts": "300.2", "thread_ts": "300.1"}
    await gateway.handle_message(reply, client)
    await gateway.handle_message({**reply, "ts": "300.3"}, client)
    assert client.posts == []
    assert client.reads == 1


@pytest.mark.asyncio
async def test_a_failing_thread_read_stays_silent(agent):
    gateway, client = make_gateway(agent), FakeSlack()

    async def broken(**kwargs):
        raise RuntimeError("ratelimited")

    client.conversations_replies = broken
    reply = {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": REQUESTER, "text": "hi", "ts": "300.2", "thread_ts": "300.1"}
    await gateway.handle_message(reply, client)
    assert client.posts == []
