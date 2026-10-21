import asyncio
import html
import logging
import re
import signal
import time
from collections import OrderedDict
from typing import Any, Callable, Protocol

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .a2a_client import AgentClient, AgentError
from .bridge import Bridge
from .config import ConfigError, GatewaySettings
from .rendering import Button, Rendered, chunks, proposal_text, render_refusal, says_nothing_new
from .session import SessionStore
from .tasks import INPUT_REQUIRED, NON_TERMINAL_STATES as RUNNING_STATES, short_id

logger = logging.getLogger("chat_gateway.teams")

DECISION_KEY = "ari"
MAX_MESSAGE_CHARS = 12000
MAX_CARD_CHARS = 6000
MAX_REMEMBERED_CARDS = 500
MENTION = re.compile(r"<at>(.*?)</at>", re.DOTALL | re.IGNORECASE)
TAG = re.compile(r"<[^>]+>")
FENCE = re.compile(r"(```.*?```)", re.DOTALL)
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

HELP_TEXT = (
    "Mention me with a request, for example: fix the NPE in CustomerLookup, "
    "or ask a question. In a channel, mention me in every reply; in a chat with me, just write. "
    "Add `repo=<url>`, `ref=<branch>` or `base=<branch>` to choose the repository and branches. "
    "Say `status` to see where the current task is."
)

STATE_ICONS = {
    "TASK_STATE_COMPLETED": "✅",
    "TASK_STATE_FAILED": "❌",
    "TASK_STATE_CANCELED": "🚫",
    INPUT_REQUIRED: "✋",
    "refused": "⚠️",
    "error": "⚠️",
}
WORKING = "⏳"


class TeamsSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    teams_app_id: str = ""
    teams_app_password: str = ""
    teams_tenant_id: str = ""
    teams_allowed_teams: str = ""
    teams_allowed_users: str = ""
    teams_progress_seconds: float = 5.0
    teams_session_file: str = ""
    teams_host: str = "0.0.0.0"
    teams_port: int = 3978
    teams_messages_path: str = "/api/messages"

    @field_validator("teams_app_id", "teams_app_password", "teams_tenant_id", mode="before")
    @classmethod
    def strip_value(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    def check(self) -> None:
        if not GUID.match(self.teams_app_id):
            raise ConfigError("TEAMS_APP_ID must be the bot's id (a GUID) from the Teams Developer Portal")
        if not self.teams_app_password:
            raise ConfigError("TEAMS_APP_PASSWORD is empty; add a client secret for the bot")
        if not GUID.match(self.teams_tenant_id):
            raise ConfigError("TEAMS_TENANT_ID must be the Microsoft 365 tenant id (a GUID)")
        if not self.teams_messages_path.startswith("/") or self.teams_messages_path == "/healthz":
            raise ConfigError("TEAMS_MESSAGES_PATH must be a path starting with / (and not /healthz)")

    def allowed_teams(self) -> set[str]:
        return {t.strip() for t in self.teams_allowed_teams.split(",") if t.strip()}

    def allowed_users(self) -> set[str]:
        return {u.strip().lower() for u in self.teams_allowed_users.split(",") if u.strip()}


class Poster(Protocol):
    async def send(self, text: str = "", card: dict[str, Any] | None = None) -> str: ...

    async def update(self, activity_id: str, text: str = "", card: dict[str, Any] | None = None) -> None: ...


def to_plain(text: str, bot_name: str = "") -> str:
    def mention(match: re.Match) -> str:
        name = html.unescape(TAG.sub("", match.group(1))).strip()
        return "" if bot_name and name.lower() == bot_name.lower() else "@" + name

    value = MENTION.sub(mention, text or "")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = html.unescape(TAG.sub("", value)).replace("\xa0", " ")
    return re.sub(r"[ \t]{2,}", " ", value).strip()


def to_teams(text: str) -> str:
    pieces = []
    for index, piece in enumerate(FENCE.split(text or "")):
        if index % 2:
            pieces.append(piece)
            continue
        pieces.append(re.sub(r"(?<!\n)\n(?!\n)", "\n\n", piece))
    return "".join(pieces)


def headline(rendered: Rendered) -> str:
    task = rendered.task or {}
    task_id = str(task.get("id") or "")
    icon = STATE_ICONS.get(rendered.state, WORKING)
    label = {
        "TASK_STATE_COMPLETED": "Done",
        "TASK_STATE_FAILED": "Failed",
        "TASK_STATE_CANCELED": "Canceled",
        INPUT_REQUIRED: "Waiting for approval",
    }.get(rendered.state, "")
    if not label:
        return f"{icon} {rendered.text}"
    return f"{icon} {label}" + (f" (task {short_id(task_id)})" if task_id else "")


def status_line(rendered: Rendered) -> str:
    if rendered.state in RUNNING_STATES:
        return f"{WORKING} {rendered.text}"
    return headline(rendered)


def decision_data(thread_key: str, button: Button, requester: str = "") -> dict[str, str]:
    return {DECISION_KEY: button.action, "k": thread_key, "t": button.task_id, "p": button.pending_action_id, "r": requester}


def read_decision(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or value.get(DECISION_KEY) not in ("approve", "decline"):
        return {}
    return {key: str(value.get(key) or "") for key in (DECISION_KEY, "k", "t", "p", "r")}


def text_block(text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "TextBlock", "text": text, "wrap": True, **extra}


def proposal_card(
    rendered: Rendered, thread_key: str, requester: str = "", approvers_note: str | None = None
) -> dict[str, Any]:
    body: list[dict[str, Any]] = [text_block(headline(rendered), weight="Bolder")]
    body.append(text_block(proposal_text(rendered)[:MAX_CARD_CHARS]))
    if approvers_note:
        body.append(text_block(approvers_note, isSubtle=True, size="Small"))
    actions: list[dict[str, Any]] = []
    for button in rendered.buttons:
        action: dict[str, Any] = {"type": "Action.Submit", "title": button.label, "data": decision_data(thread_key, button, requester)}
        action["style"] = "positive" if button.action == "approve" else "destructive"
        actions.append(action)
    for index, url in enumerate(rendered.links[:3]):
        actions.append({"type": "Action.OpenUrl", "title": "Full diff" if index == 0 else "Link", "url": url})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": actions,
    }


def closed_card(card: dict[str, Any] | None, note: str) -> dict[str, Any]:
    base = dict(card or {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4", "body": []})
    base["body"] = [*base.get("body", []), text_block(note, isSubtle=True)]
    base["actions"] = [a for a in base.get("actions", []) if a.get("type") == "Action.OpenUrl"]
    return base


class ProgressMessage:
    def __init__(self, poster: Poster, activity_id: str, *, interval: float, clock: Callable[[], float] = time.monotonic):
        self.poster = poster
        self.activity_id = activity_id
        self.interval = interval
        self.clock = clock
        self.last_text = ""
        self.last_at = float("-inf")

    async def set(self, text: str, *, force: bool = False) -> None:
        if not self.activity_id or text == self.last_text:
            return
        now = self.clock()
        if not force and now - self.last_at < self.interval:
            return
        try:
            await self.poster.update(self.activity_id, text)
        except Exception as exc:
            logger.warning("progress update failed: %s", exc)
            return
        self.last_text = text
        self.last_at = now

    async def update(self, rendered: Rendered) -> None:
        await self.set(f"{WORKING} {rendered.text}")


class TeamsGateway:
    def __init__(self, bridge: Bridge, settings: TeamsSettings, *, clock: Callable[[], float] = time.monotonic):
        self.bridge = bridge
        self.settings = settings
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._cards: OrderedDict[str, dict[str, Any]] = OrderedDict()

    @staticmethod
    def user_of(activity: dict[str, Any]) -> tuple[str, str]:
        sender = activity.get("from") or {}
        return str(sender.get("aadObjectId") or sender.get("id") or "").lower(), str(sender.get("name") or "")

    @staticmethod
    def tenant_of(activity: dict[str, Any]) -> str:
        conversation = activity.get("conversation") or {}
        channel_data = activity.get("channelData") or {}
        return str(conversation.get("tenantId") or (channel_data.get("tenant") or {}).get("id") or "").lower()

    @staticmethod
    def team_of(activity: dict[str, Any]) -> set[str]:
        team = (activity.get("channelData") or {}).get("team") or {}
        return {str(team[key]) for key in ("id", "aadGroupId") if team.get(key)}

    def allowed(self, activity: dict[str, Any], user: str) -> bool:
        teams = self.settings.allowed_teams()
        users = self.settings.allowed_users()
        return (not teams or bool(teams & self.team_of(activity))) and (not users or user in users)

    async def handle(self, activity: dict[str, Any], poster: Poster) -> None:
        try:
            await self._handle(activity, poster)
        except Exception:
            logger.exception("teams gateway error")

    async def _handle(self, activity: dict[str, Any], poster: Poster) -> None:
        if activity.get("type") != "message":
            return
        tenant = self.tenant_of(activity)
        if tenant != self.settings.teams_tenant_id.lower():
            logger.warning("ignoring a message from tenant %r", tenant)
            return
        thread_key = str((activity.get("conversation") or {}).get("id") or "")
        user, name = self.user_of(activity)
        if not thread_key or not user:
            return
        decision = read_decision(activity.get("value"))
        if decision:
            await self.decide(activity, poster, decision, user, name)
            return
        bot_name = str((activity.get("recipient") or {}).get("name") or "")
        await self.converse(activity, poster, thread_key, user, to_plain(str(activity.get("text") or ""), bot_name))

    async def converse(self, activity: dict[str, Any], poster: Poster, thread_key: str, user: str, text: str) -> None:
        if not self.allowed(activity, user):
            await poster.send("⚠️ I am not enabled for you here.")
            return
        command = text.lower().strip(" .!?")
        if not text or command in ("help", "?"):
            await poster.send(HELP_TEXT)
            return
        if command == "status":
            rendered = await self.safely(self.bridge.status(thread_key))
            if rendered.state == INPUT_REQUIRED:
                await self.post_rendered(poster, thread_key, rendered)
            else:
                await poster.send(status_line(rendered))
            return
        lock = self._locks.setdefault(thread_key, asyncio.Lock())
        if lock.locked():
            await poster.send(f"{WORKING} I am still on the previous message in this conversation.")
            return
        async with lock:
            activity_id = await poster.send(f"{WORKING} Working on it...")
            progress = ProgressMessage(poster, activity_id, interval=self.settings.teams_progress_seconds, clock=self.clock)
            rendered = await self.safely(self.bridge.start(thread_key, text, user, on_update=progress.update))
            await self.finish(poster, thread_key, progress, rendered)

    async def post_rendered(self, poster: Poster, thread_key: str, rendered: Rendered) -> None:
        if rendered.buttons:
            conversation = self.bridge.store.get(thread_key)
            requester = conversation.requester if conversation else ""
            settings = self.bridge.settings
            note = None
            if not settings.requester_can_approve:
                if settings.approver_set() - {requester}:
                    note = "Needs approval from a configured approver other than the person who asked."
                else:
                    note = "No one other than the requester is configured to approve this; an admin has to add an approver."
            card = proposal_card(rendered, thread_key, requester, note)
            activity_id = await poster.send(card=card)
            self._remember_card(activity_id, card)
            return
        for chunk in chunks(to_teams(rendered.text), MAX_MESSAGE_CHARS):
            if chunk:
                await poster.send(chunk)

    async def finish(self, poster: Poster, thread_key: str, progress: ProgressMessage, rendered: Rendered) -> None:
        await progress.set(headline(rendered), force=True)
        if rendered.state not in ("refused", "error") and not says_nothing_new(rendered):
            await self.post_rendered(poster, thread_key, rendered)

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

    async def decide(self, activity: dict[str, Any], poster: Poster, decision: dict[str, str], user: str, name: str) -> None:
        thread_key = decision["k"] or str((activity.get("conversation") or {}).get("id") or "")
        card_id = str(activity.get("replyToId") or "")
        if not self.allowed(activity, user):
            await poster.send("⚠️ I am not enabled for you here.")
            return
        lock = self._locks.setdefault(thread_key, asyncio.Lock())
        if lock.locked():
            await poster.send(f"{WORKING} This conversation is busy; try again in a moment.")
            return
        async with lock:
            approving = decision[DECISION_KEY] == "approve"
            verb = "Approved" if approving else "Declined"
            who = name or "someone"
            activity_id = await poster.send(
                f"{WORKING} {who} {verb.lower()}. " + ("Opening the pull request..." if approving else "Canceling the task...")
            )
            progress = ProgressMessage(poster, activity_id, interval=self.settings.teams_progress_seconds, clock=self.clock)
            if approving:
                call = self.bridge.approve(thread_key, user, decision["p"], decision["t"], on_update=progress.update, requester=decision["r"])
            else:
                call = self.bridge.decline(thread_key, user, decision["t"], decision["p"], requester=decision["r"])
            rendered = await self.safely(call)
            if rendered.state not in ("refused", "error"):
                await self.close_card(poster, card_id, f"{verb} by {who}")
            await self.finish(poster, thread_key, progress, rendered)

    async def close_card(self, poster: Poster, card_id: str, note: str) -> None:
        if not card_id:
            return
        try:
            await poster.update(card_id, card=closed_card(self._cards.pop(card_id, None), note))
        except Exception as exc:
            logger.warning("could not remove the buttons: %s", exc)

    def _remember_card(self, activity_id: str, card: dict[str, Any]) -> None:
        if not activity_id:
            return
        self._cards[activity_id] = card
        while len(self._cards) > MAX_REMEMBERED_CARDS:
            self._cards.popitem(last=False)


class ProactivePoster:
    def __init__(self, adapter: Any, app_id: str, reference: Any):
        self.adapter = adapter
        self.app_id = app_id
        self.reference = reference

    @staticmethod
    def _activity(text: str, card: dict[str, Any] | None) -> Any:
        from microsoft_agents.activity import Activity
        from microsoft_agents.hosting.core import CardFactory

        activity = Activity(type="message", text=text or None, text_format="markdown")
        if card:
            activity.attachments = [CardFactory.adaptive_card(card)]
        return activity

    async def _continue(self, callback: Any) -> None:
        await self.adapter.continue_conversation(self.app_id, self.reference.get_continuation_activity(), callback)

    async def send(self, text: str = "", card: dict[str, Any] | None = None) -> str:
        activity = self._activity(text, card)
        sent: dict[str, str] = {}

        async def callback(context: Any) -> None:
            response = await context.send_activity(activity)
            sent["id"] = str(getattr(response, "id", "") or "")

        await self._continue(callback)
        return sent.get("id", "")

    async def update(self, activity_id: str, text: str = "", card: dict[str, Any] | None = None) -> None:
        activity = self._activity(text, card)
        activity.id = activity_id

        async def callback(context: Any) -> None:
            await context.update_activity(activity)

        await self._continue(callback)


class TeamsAgent:
    def __init__(self, gateway: TeamsGateway, adapter: Any, app_id: str):
        self.gateway = gateway
        self.adapter = adapter
        self.app_id = app_id
        self.tasks: set[asyncio.Task] = set()

    async def on_turn(self, context: Any) -> None:
        activity = context.activity
        if activity.type != "message":
            return
        data = activity.model_dump(mode="json", by_alias=True, exclude_none=True)
        poster = ProactivePoster(self.adapter, self.app_id, activity.get_conversation_reference())
        task = asyncio.create_task(self.gateway.handle(data, poster))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


def make_gateway(settings: TeamsSettings | None = None, gateway_settings: GatewaySettings | None = None) -> TeamsGateway:
    teams_settings = settings or TeamsSettings()
    agent_settings = gateway_settings or GatewaySettings()
    client = AgentClient(agent_settings.ari_agent_url, agent_settings.ari_api_key, timeout=agent_settings.request_timeout_seconds)
    store = SessionStore(path=teams_settings.teams_session_file or None)
    return TeamsGateway(Bridge(client, agent_settings, store), teams_settings)


async def run() -> None:
    from aiohttp import web
    from microsoft_agents.authentication.msal import MsalConnectionManager
    from microsoft_agents.hosting.aiohttp import CloudAdapter, jwt_authorization_decorator
    from microsoft_agents.hosting.core import AgentAuthConfiguration

    settings = TeamsSettings()
    settings.check()
    gateway = make_gateway(settings)
    gateway.bridge.settings.llm_config()
    gateway.bridge.settings.check_approval_policy()
    auth = AgentAuthConfiguration(
        client_id=settings.teams_app_id, tenant_id=settings.teams_tenant_id, client_secret=settings.teams_app_password
    )
    connections = MsalConnectionManager(connections_configurations={"SERVICE_CONNECTION": auth})
    adapter = CloudAdapter(connection_manager=connections)
    agent = TeamsAgent(gateway, adapter, settings.teams_app_id)

    @jwt_authorization_decorator
    async def messages(request: Any) -> Any:
        return await adapter.process(request, agent)

    async def health(request: Any) -> Any:
        return web.Response(text="ok")

    app = web.Application()
    app["agent_configuration"] = connections.get_default_connection_configuration()
    app.router.add_post(settings.teams_messages_path, messages)
    app.router.add_get("/healthz", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, settings.teams_host, settings.teams_port).start()
    logger.info(
        "listening on %s:%s%s for Teams bot %s; agent %s",
        settings.teams_host,
        settings.teams_port,
        settings.teams_messages_path,
        settings.teams_app_id,
        gateway.bridge.settings.ari_agent_url,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        for task in list(agent.tasks):
            task.cancel()
        await runner.cleanup()
    logger.info("stopped")
