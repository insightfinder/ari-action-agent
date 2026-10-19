import asyncio
import json
import logging
import re
import signal
import time
from typing import Any, Callable

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .a2a_client import AgentClient, AgentError
from .bridge import Bridge
from .config import ConfigError, GatewaySettings
from .rendering import Button, Rendered, chunks, proposal_text, render_refusal, says_nothing_new
from .session import SessionStore
from .tasks import INPUT_REQUIRED, NON_TERMINAL_STATES as RUNNING_STATES, short_id

logger = logging.getLogger("chat_gateway.slack")

APPROVE_ACTION = "ari_approve"
DECLINE_ACTION = "ari_decline"
LINK_ACTION = re.compile(r"^ari_link_\d+$")
MAX_SECTION_CHARS = 2900
MAX_MESSAGE_CHARS = 3500
MAX_BLOCKS = 45

USER_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|([^>]*))?>")
CHANNEL_MENTION = re.compile(r"<#([A-Z0-9]+)(?:\|([^>]*))?>")
SPECIAL_MENTION = re.compile(r"<!([a-z]+)(?:\^[^|>]*)?(?:\|([^>]*))?>")
SLACK_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]*))?>")
MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
PROTECTED = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)
ACCEPTED_SUBTYPES = frozenset({"file_share", "thread_broadcast"})

HELP_TEXT = (
    "Mention me with what you need, for example `@ARI fix the NPE in CustomerLookup`, "
    "or ask a question. Reply in the thread to continue the same conversation. "
    "Add `repo=&lt;url&gt;`, `ref=&lt;branch&gt;` or `base=&lt;branch&gt;` to choose the repository and branches. "
    "Say `status` to see where the current task is."
)

FORGOTTEN_TEXT = (
    ":information_source: I no longer have this conversation; I was restarted or it is more than a day old. "
    "Mention me in this thread to start again. If a proposal here is still waiting, its buttons still work."
)
MAX_REMEMBERED_THREADS = 5000

STATE_ICONS = {
    "TASK_STATE_COMPLETED": ":white_check_mark:",
    "TASK_STATE_FAILED": ":x:",
    "TASK_STATE_CANCELED": ":no_entry_sign:",
    INPUT_REQUIRED: ":raised_hand:",
    "refused": ":warning:",
    "error": ":warning:",
}


class SlackSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    slack_app_token: str = ""
    slack_bot_token: str = ""
    slack_allowed_channels: str = ""
    slack_allowed_users: str = ""
    slack_progress_seconds: float = 5.0
    slack_session_file: str = ""

    @field_validator("slack_app_token", "slack_bot_token", mode="before")
    @classmethod
    def strip_token(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    def check(self) -> None:
        if not self.slack_app_token.startswith("xapp-"):
            raise ConfigError("SLACK_APP_TOKEN must be the app-level token that starts with xapp- (Socket Mode)")
        if not self.slack_bot_token.startswith("xoxb-"):
            raise ConfigError("SLACK_BOT_TOKEN must be the bot token that starts with xoxb-")

    def allowed_channels(self) -> set[str]:
        return {c.strip() for c in self.slack_allowed_channels.split(",") if c.strip()}

    def allowed_users(self) -> set[str]:
        return {u.strip() for u in self.slack_allowed_users.split(",") if u.strip()}


def to_plain(text: str, bot_user_id: str = "") -> str:
    def user(match: re.Match) -> str:
        if bot_user_id and match.group(1) == bot_user_id:
            return ""
        return "@" + (match.group(2) or match.group(1))

    def channel(match: re.Match) -> str:
        return "#" + (match.group(2) or match.group(1))

    def special(match: re.Match) -> str:
        return "@" + (match.group(2) or match.group(1)).lstrip("@")

    value = USER_MENTION.sub(user, text or "")
    value = CHANNEL_MENTION.sub(channel, value)
    value = SPECIAL_MENTION.sub(special, value)
    value = SLACK_LINK.sub(_link_text, value)
    value = value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return re.sub(r"[ \t]{2,}", " ", value).strip()


def _link_text(match: re.Match) -> str:
    url, label = match.group(1), match.group(2) or ""
    if label and url.lower() in {f"http://{label.lower()}", f"https://{label.lower()}", f"mailto:{label.lower()}"}:
        return label
    return url


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_mrkdwn(text: str) -> str:
    pieces = []
    for index, piece in enumerate(PROTECTED.split(text or "")):
        if index % 2:
            pieces.append(_escape(piece))
            continue
        piece = _escape(piece)
        piece = MARKDOWN_LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", piece)
        piece = BOLD.sub(r"*\1*", piece)
        piece = HEADING.sub(r"*\1*", piece)
        pieces.append(piece)
    return "".join(pieces)




def button_value(thread_key: str, button: Button, requester: str = "") -> str:
    return json.dumps(
        {"k": thread_key, "t": button.task_id, "p": button.pending_action_id, "r": requester}, separators=(",", ":")
    )


def read_button_value(value: str) -> dict[str, str]:
    try:
        data = json.loads(value or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data.get(key) or "") for key in ("k", "t", "p", "r")}


def headline(rendered: Rendered) -> str:
    task = rendered.task or {}
    task_id = str(task.get("id") or "")
    icon = STATE_ICONS.get(rendered.state, ":hourglass_flowing_sand:")
    label = {
        "TASK_STATE_COMPLETED": "Done",
        "TASK_STATE_FAILED": "Failed",
        "TASK_STATE_CANCELED": "Canceled",
        INPUT_REQUIRED: "Waiting for approval",
    }.get(rendered.state, "")
    if not label:
        return f"{icon} {to_mrkdwn(rendered.text)}"
    return f"{icon} {label}" + (f" (task {short_id(task_id)})" if task_id else "")


def status_line(rendered: Rendered) -> str:
    if rendered.state in RUNNING_STATES:
        return f":hourglass_flowing_sand: {to_mrkdwn(rendered.text)}"
    return headline(rendered)






def blocks_for(
    rendered: Rendered, thread_key: str, requester: str = "", approvers: tuple[str, ...] | None = None
) -> list[dict[str, Any]]:
    body = to_mrkdwn(proposal_text(rendered) if rendered.state == INPUT_REQUIRED else rendered.text)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in chunks(body, MAX_SECTION_CHARS) if chunk
    ][:MAX_BLOCKS]
    if rendered.buttons and approvers is not None:
        if approvers:
            note = "Needs approval from " + " or ".join(f"<@{approver}>" for approver in approvers)
        else:
            note = "No one other than the requester is configured to approve this; an admin has to add an approver"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    elements: list[dict[str, Any]] = []
    for button in rendered.buttons:
        element: dict[str, Any] = {
            "type": "button",
            "text": {"type": "plain_text", "text": button.label},
            "action_id": APPROVE_ACTION if button.action == "approve" else DECLINE_ACTION,
            "value": button_value(thread_key, button, requester),
        }
        if button.action == "approve":
            element["style"] = "primary"
            element["confirm"] = {
                "title": {"type": "plain_text", "text": "Open the pull request?"},
                "text": {"type": "mrkdwn", "text": "The agent will open this pull request on GitHub."},
                "confirm": {"type": "plain_text", "text": "Approve"},
                "deny": {"type": "plain_text", "text": "Not yet"},
            }
        else:
            element["style"] = "danger"
        elements.append(element)
    for index, url in enumerate(rendered.links[:3]):
        elements.append(
            {"type": "button", "text": {"type": "plain_text", "text": "Full diff" if index == 0 else "Link"}, "url": url, "action_id": f"ari_link_{index}"}
        )
    if elements:
        blocks.append({"type": "actions", "block_id": "ari_decision", "elements": elements})
    return blocks


class ProgressMessage:
    def __init__(self, client: Any, channel: str, ts: str, *, interval: float, clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.channel = channel
        self.ts = ts
        self.interval = interval
        self.clock = clock
        self.last_text = ""
        self.last_at = float("-inf")

    async def set(self, text: str, *, force: bool = False) -> None:
        if not self.ts or text == self.last_text:
            return
        now = self.clock()
        if not force and now - self.last_at < self.interval:
            return
        try:
            await self.client.chat_update(channel=self.channel, ts=self.ts, text=text)
        except Exception as exc:
            logger.warning("progress update failed: %s", exc)
            return
        self.last_text = text
        self.last_at = now

    async def update(self, rendered: Rendered) -> None:
        await self.set(f":hourglass_flowing_sand: {to_mrkdwn(rendered.text)}")


class SlackGateway:
    def __init__(self, bridge: Bridge, settings: SlackSettings, *, clock: Callable[[], float] = time.monotonic):
        self.bridge = bridge
        self.settings = settings
        self.clock = clock
        self.bot_user_id = ""
        self._locks: dict[str, asyncio.Lock] = {}
        self._participation: dict[str, bool] = {}
        self._hinted: set[str] = set()

    def allowed(self, channel: str, user: str) -> bool:
        channels = self.settings.allowed_channels()
        users = self.settings.allowed_users()
        return (not channels or channel in channels) and (not users or user in users)

    async def post(self, client: Any, channel: str, thread_ts: str | None, text: str, blocks: list | None = None) -> str:
        kwargs: dict[str, Any] = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if blocks:
            kwargs["blocks"] = blocks
        response = await client.chat_postMessage(**kwargs)
        return str((response or {}).get("ts") or "")

    async def post_rendered(self, client: Any, channel: str, thread_ts: str | None, thread_key: str, rendered: Rendered) -> None:
        if rendered.buttons:
            text = to_mrkdwn(proposal_text(rendered))
            conversation = self.bridge.store.get(thread_key)
            requester = conversation.requester if conversation else ""
            settings = self.bridge.settings
            approvers = None if settings.requester_can_approve else tuple(sorted(settings.approver_set() - {requester}))
            blocks = blocks_for(rendered, thread_key, requester, approvers)
            await self.post(client, channel, thread_ts, text[:MAX_MESSAGE_CHARS], blocks)
            return
        for chunk in chunks(to_mrkdwn(rendered.text), MAX_MESSAGE_CHARS):
            if chunk:
                await self.post(client, channel, thread_ts, chunk)

    async def handle_mention(self, event: dict[str, Any], client: Any) -> None:
        channel = str(event.get("channel") or "")
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
        if not channel or not thread_ts:
            return
        await self.converse(client, channel, thread_ts, f"{channel}:{thread_ts}", str(event.get("user") or ""), str(event.get("text") or ""))

    async def handle_message(self, event: dict[str, Any], client: Any) -> None:
        if event.get("subtype") not in (None, *ACCEPTED_SUBTYPES) or event.get("bot_id") or not event.get("user"):
            return
        text = str(event.get("text") or "")
        channel = str(event.get("channel") or "")
        user = str(event["user"])
        if event.get("channel_type") == "im":
            await self.converse(client, channel, event.get("thread_ts"), f"{channel}:dm", user, text)
            return
        if self.bot_user_id and f"<@{self.bot_user_id}" in text:
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return
        thread_key = f"{channel}:{thread_ts}"
        if not self.allowed(channel, user):
            return
        if self.bridge.store.get(thread_key) is None:
            if thread_key not in self._hinted and await self.took_part(client, channel, str(thread_ts)):
                self._remember(self._hinted, thread_key)
                await self.post(client, channel, str(thread_ts), FORGOTTEN_TEXT)
            return
        await self.converse(client, channel, str(thread_ts), thread_key, user, text)

    async def took_part(self, client: Any, channel: str, thread_ts: str) -> bool:
        key = f"{channel}:{thread_ts}"
        if key in self._participation:
            return self._participation[key]
        try:
            response = await client.conversations_replies(channel=channel, ts=thread_ts, limit=100)
            messages = (response or {}).get("messages") or []
            found = bool(self.bot_user_id) and any(m.get("user") == self.bot_user_id for m in messages)
        except Exception as exc:
            logger.warning("could not read thread %s: %s", key, exc)
            return False
        if len(self._participation) >= MAX_REMEMBERED_THREADS:
            self._participation.clear()
        self._participation[key] = found
        return found

    @staticmethod
    def _remember(seen: set[str], key: str) -> None:
        if len(seen) >= MAX_REMEMBERED_THREADS:
            seen.clear()
        seen.add(key)

    async def converse(self, client: Any, channel: str, thread_ts: str | None, thread_key: str, user: str, raw_text: str) -> None:
        if not self.allowed(channel, user):
            await self.post(client, channel, thread_ts, ":warning: I am not enabled for you in this channel.")
            return
        text = to_plain(raw_text, self.bot_user_id)
        command = text.lower().strip(" .!?")
        if not text or command in ("help", "?"):
            await self.post(client, channel, thread_ts, HELP_TEXT)
            return
        if command == "status":
            rendered = await self.safely(self.bridge.status(thread_key))
            if rendered.state == INPUT_REQUIRED:
                await self.post_rendered(client, channel, thread_ts, thread_key, rendered)
            else:
                await self.post(client, channel, thread_ts, status_line(rendered))
            return
        lock = self._locks.setdefault(thread_key, asyncio.Lock())
        if lock.locked():
            await self.post(client, channel, thread_ts, ":hourglass_flowing_sand: I am still on the previous message in this thread.")
            return
        async with lock:
            ts = await self.post(client, channel, thread_ts, ":hourglass_flowing_sand: Working on it...")
            progress = ProgressMessage(client, channel, ts, interval=self.settings.slack_progress_seconds, clock=self.clock)
            rendered = await self.safely(self.bridge.start(thread_key, text, user, on_update=progress.update))
            await self.finish(client, channel, thread_ts, thread_key, progress, rendered)

    async def finish(
        self, client: Any, channel: str, thread_ts: str | None, thread_key: str, progress: ProgressMessage, rendered: Rendered
    ) -> None:
        await progress.set(headline(rendered), force=True)
        if rendered.state not in ("refused", "error") and not says_nothing_new(rendered):
            await self.post_rendered(client, channel, thread_ts, thread_key, rendered)

    async def safely(self, call: Any) -> Rendered:
        try:
            return await call
        except ConfigError as exc:
            return render_refusal(str(exc))
        except AgentError as exc:
            return Rendered(text=f"the agent answered with an error: {exc}", state="error")
        except (TimeoutError, RuntimeError) as exc:
            return Rendered(text=f"{exc}. Say `status` to check on it.", state="error")
        except Exception:
            logger.exception("gateway error")
            return Rendered(text="something went wrong in the gateway; the details are in its log", state="error")

    async def handle_action(self, body: dict[str, Any], client: Any) -> None:
        actions = body.get("actions") or []
        if not actions:
            return
        action = actions[0]
        action_id = str(action.get("action_id") or "")
        if action_id not in (APPROVE_ACTION, DECLINE_ACTION):
            return
        user = str((body.get("user") or {}).get("id") or "")
        channel = str((body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id") or "")
        message = body.get("message") or {}
        message_ts = str(message.get("ts") or (body.get("container") or {}).get("message_ts") or "")
        value = read_button_value(str(action.get("value") or ""))
        in_dm = value.get("k", "").endswith(":dm")
        thread_ts = str(message.get("thread_ts") or ("" if in_dm else message_ts)) or None
        thread_key = value.get("k") or f"{channel}:{thread_ts}"
        if not self.allowed(channel, user):
            await self.notice(client, channel, user, thread_ts, ":warning: I am not enabled for you in this channel.")
            return
        lock = self._locks.setdefault(thread_key, asyncio.Lock())
        if lock.locked():
            await self.notice(client, channel, user, thread_ts, ":hourglass_flowing_sand: This thread is busy; try again in a moment.")
            return
        async with lock:
            approving = action_id == APPROVE_ACTION
            verb = "Approved" if approving else "Declined"
            ts = await self.post(
                client, channel, thread_ts, f":hourglass_flowing_sand: <@{user}> {verb.lower()}. " + ("Opening the pull request..." if approving else "Canceling the task...")
            )
            progress = ProgressMessage(client, channel, ts, interval=self.settings.slack_progress_seconds, clock=self.clock)
            if approving:
                call = self.bridge.approve(
                    thread_key, user, value.get("p", ""), value.get("t", ""), on_update=progress.update, requester=value.get("r", "")
                )
            else:
                call = self.bridge.decline(thread_key, user, value.get("t", ""), value.get("p", ""), requester=value.get("r", ""))
            rendered = await self.safely(call)
            if rendered.state not in ("refused", "error"):
                await self.close_buttons(client, channel, message_ts, message, f"{verb} by <@{user}>")
            await self.finish(client, channel, thread_ts, thread_key, progress, rendered)

    async def close_buttons(self, client: Any, channel: str, ts: str, message: dict[str, Any], note: str) -> None:
        if not ts:
            return
        blocks = [b for b in message.get("blocks") or [] if b.get("type") != "actions"]
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
        fallback = str(message.get("text") or note)
        try:
            await client.chat_update(channel=channel, ts=ts, text=fallback, blocks=blocks)
        except Exception as exc:
            logger.warning("could not remove the buttons: %s", exc)

    async def notice(self, client: Any, channel: str, user: str, thread_ts: str | None, text: str) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "user": user, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            await client.chat_postEphemeral(**kwargs)
        except Exception as exc:
            logger.warning("could not send a notice: %s", exc)


def build_app(gateway: SlackGateway, settings: SlackSettings) -> Any:
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=settings.slack_bot_token)

    def remember(context: Any) -> None:
        bot = context.get("bot_user_id") if hasattr(context, "get") else None
        if bot:
            gateway.bot_user_id = str(bot)

    @app.event("app_mention")
    async def on_mention(event: dict, client: Any, context: Any) -> None:
        remember(context)
        await gateway.handle_mention(event, client)

    @app.event("message")
    async def on_message(event: dict, client: Any, context: Any) -> None:
        remember(context)
        await gateway.handle_message(event, client)

    @app.action(APPROVE_ACTION)
    async def on_approve(ack: Any, body: dict, client: Any) -> None:
        await ack()
        await gateway.handle_action(body, client)

    @app.action(DECLINE_ACTION)
    async def on_decline(ack: Any, body: dict, client: Any) -> None:
        await ack()
        await gateway.handle_action(body, client)

    @app.action(LINK_ACTION)
    async def on_link(ack: Any) -> None:
        await ack()

    return app


def make_gateway(settings: SlackSettings | None = None, gateway_settings: GatewaySettings | None = None) -> SlackGateway:
    slack_settings = settings or SlackSettings()
    agent_settings = gateway_settings or GatewaySettings()
    client = AgentClient(agent_settings.ari_agent_url, agent_settings.ari_api_key, timeout=agent_settings.request_timeout_seconds)
    store = SessionStore(path=slack_settings.slack_session_file or None)
    return SlackGateway(Bridge(client, agent_settings, store), slack_settings)


async def run() -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    settings = SlackSettings()
    settings.check()
    gateway = make_gateway(settings)
    gateway.bridge.settings.llm_config()
    gateway.bridge.settings.check_approval_policy()
    app = build_app(gateway, settings)
    auth = await app.client.auth_test()
    gateway.bot_user_id = str(auth.get("user_id") or "")
    logger.info("connected to Slack as %s (%s); agent %s", auth.get("user"), gateway.bot_user_id, gateway.bridge.settings.ari_agent_url)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    await handler.connect_async()
    try:
        await stop.wait()
    finally:
        await handler.close_async()
    logger.info("disconnected from Slack")
