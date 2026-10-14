import asyncio
import re
import uuid
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from pydantic import ValidationError
from rich.console import Console
from rich.text import Text

from chat_gateway.a2a_client import AgentError
from chat_gateway.bridge import STALE_PROPOSAL_NOTE, Bridge, decision_word, parse_directives
from chat_gateway.config import DEFAULT_MODELS, ConfigError, GatewaySettings
from chat_gateway.rendering import Rendered, state_label
from chat_gateway.session import Conversation
from chat_gateway.tasks import CANCELED, FAILED, INPUT_REQUIRED, NON_TERMINAL_STATES, artifacts, pending_action, status_text

from . import views
from .config_file import validation_message

RECOVERABLE = (AgentError, ConfigError, TimeoutError, RuntimeError, ValueError)
REQUESTER = "cli"
CONNECT_TIMEOUT_SECONDS = 15.0

COMMANDS = {
    "/help": "Show this list",
    "/repo": "Repository for your next requests, e.g. /repo https://github.com/org/repo",
    "/branch": "Branch to work from, e.g. /branch dev",
    "/base": "Branch the pull request targets, e.g. /base staging",
    "/status": "Show where the current task is, including a waiting proposal",
    "/wait": "Follow the current task again until it finishes",
    "/cancel": "Stop the current task",
    "/approve": "Approve the waiting proposal and open the pull request",
    "/decline": "Decline the waiting proposal",
    "/new": "Start a new conversation",
    "/config": "Show the current settings",
    "/setup": "Change the connection and keys",
    "/quit": "Leave (Ctrl-D works too)",
}
QUIT_COMMANDS = {"/quit", "/exit", "/q"}
SHELL_COMMAND_RE = re.compile(r"^ari\s+(tasks|approve|send|card|config|setup|chat)\b", re.IGNORECASE)
RUNNING_HINT = "/wait follows it again, /cancel stops it."

DECISIONS = [
    "Approve and open the pull request",
    "Decline",
    "Show the full diff link",
    "Ask a follow-up",
    "Decide later",
]
APPROVE, DECLINE, SHOW_DIFF, FOLLOW_UP, LATER = range(len(DECISIONS))

PROVIDERS = ["anthropic", "openai", "other"]
CLEAR = "-"

ARTIFACT_HINTS = {"root_cause": "root cause found", "pull_request": "pull request opened"}

OnUpdate = Callable[[Rendered], Awaitable[None]]
Factory = Callable[[OnUpdate], Awaitable[Any]]


def hint_for(rendered: Rendered) -> str:
    if rendered.state == "unavailable":
        return "the agent is restarting, still waiting"
    produced = [ARTIFACT_HINTS[name] for name in artifacts(rendered.task or {}) if name in ARTIFACT_HINTS]
    return ", ".join(produced)


def split_stale_note(text: str) -> tuple[str, bool]:
    if text.endswith(STALE_PROPOSAL_NOTE):
        return text[: -len(STALE_PROPOSAL_NOTE)].rstrip(), True
    return text, False


def action_id_of(rendered: Rendered | None) -> str:
    if rendered is None:
        return ""
    return str((pending_action(rendered.task or {}) or {}).get("id") or "")


class ChatSession:
    def __init__(
        self,
        settings: GatewaySettings,
        io: Any,
        console: Console,
        *,
        make_bridge: Callable[[GatewaySettings], Bridge],
        setup: Callable[[], GatewaySettings | None] | None = None,
        requester: str = REQUESTER,
    ) -> None:
        self.settings = settings
        self.io = io
        self.console = console
        self.make_bridge = make_bridge
        self.setup = setup
        self.requester = requester
        self.bridge = make_bridge(settings)
        self.thread_key = self.new_thread_key()
        self.base = ""
        self.shown_action_id = ""
        self.reported_pull_requests: set[str] = set()
        self.quit_requested = False
        self.handlers: dict[str, Callable[[str], None]] = {
            "/help": self.cmd_help,
            "/repo": self.cmd_repo,
            "/branch": self.cmd_branch,
            "/base": self.cmd_base,
            "/status": self.cmd_status,
            "/wait": self.cmd_wait,
            "/cancel": self.cmd_cancel,
            "/approve": lambda _: self.approve(),
            "/decline": lambda _: self.decline(),
            "/new": self.cmd_new,
            "/config": self.cmd_config,
            "/setup": self.cmd_setup,
        }

    @staticmethod
    def new_thread_key() -> str:
        return f"cli:{uuid.uuid4()}"

    def say(self, renderable: Any) -> None:
        self.console.print(Text(renderable) if isinstance(renderable, str) else renderable)

    def notice(self, message: str) -> None:
        self.say(views.notice_line(message))

    def conversation(self) -> Conversation | None:
        return self.bridge.store.get(self.thread_key)

    def run(self) -> None:
        self.show_banner()
        while not self.quit_requested:
            try:
                line = self.io.ask("you ›")
            except KeyboardInterrupt:
                self.notice("Press Ctrl-D or type /quit to leave.")
                continue
            except EOFError:
                self.say("")
                break
            line = line.strip()
            if not line:
                continue
            if not self.handle(line):
                break
            self.say("")
        self.remind_open_task()

    def run_async(self, factory: Factory, label: str) -> Any:
        async def go() -> Any:
            async with self.io.progress(label) as progress:

                async def on_update(rendered: Rendered) -> None:
                    progress.set(hint_for(rendered))

                return await factory(on_update)

        return asyncio.run(go())

    def call(self, factory: Factory, label: str = "Working") -> Rendered | None:
        try:
            return self.run_async(factory, label)
        except KeyboardInterrupt:
            conversation = self.conversation()
            task_id = conversation.task_id if conversation else ""
            message = "Stopped waiting."
            if task_id:
                message += f" Task {task_id} keeps running on the agent; {RUNNING_HINT}"
            self.notice(message)
        except RECOVERABLE as exc:
            self.say(views.error_line(exc))
        return None

    def show_banner(self) -> None:
        name, problem = "", ""
        try:
            card = self.run_async(
                lambda _: self.bridge.client.agent_card(max_attempts=1, timeout=CONNECT_TIMEOUT_SECONDS),
                "Connecting",
            )
            name = str(card.get("name") or "")
        except KeyboardInterrupt:
            problem = "the connection check was interrupted"
        except RECOVERABLE as exc:
            problem = str(exc)
        self.say(
            views.banner(
                name,
                self.settings.ari_agent_url,
                self.settings.default_repo_url,
                self.settings.default_repo_ref,
                self.base,
                problem,
            )
        )

    def handle(self, line: str) -> bool:
        if not line.startswith("/"):
            self.submit(line)
            return not self.quit_requested
        name, _, arg = line.partition(" ")
        name = self.resolve_command(name.lower())
        if not name:
            return True
        if name in QUIT_COMMANDS:
            return False
        handler = self.handlers[name]
        handler(arg.strip())
        return not self.quit_requested

    def resolve_command(self, name: str) -> str:
        if name in QUIT_COMMANDS or name in self.handlers:
            return name
        matches = sorted(c for c in [*self.handlers, *QUIT_COMMANDS] if c.startswith(name) and c != "/q")
        if len(matches) == 1:
            return matches[0]
        if matches:
            self.notice(f"{name} could mean {', '.join(matches)}. Type more of the command.")
        else:
            self.notice(f"Unknown command {name}. Type /help to see the commands.")
        return ""

    def submit(self, text: str) -> None:
        if SHELL_COMMAND_RE.match(text.strip()):
            self.notice(
                "That is a command for your shell, not a message for the agent. "
                f"Inside the chat use /status, {RUNNING_HINT} Leave with /quit to run it in your shell."
            )
            return
        prompt, directives = parse_directives(text)
        decision = decision_word(prompt)
        conversation = self.conversation()
        if decision and conversation is not None and conversation.task_id and (
            conversation.pending_action_id or conversation.task_state in NON_TERMINAL_STATES
        ):
            current = self.refresh()
            if current is None:
                return
            if current.state == INPUT_REQUIRED and action_id_of(current):
                if decision == "approve":
                    self.approve_current(current)
                else:
                    self.decline_current(current)
                return
            if current.state in NON_TERMINAL_STATES:
                self.explain_not_waiting(current)
                return
        if conversation is not None and conversation.task_id and conversation.task_state in NON_TERMINAL_STATES:
            current = self.refresh()
            if current is None:
                return
            if current.state in NON_TERMINAL_STATES:
                self.notice(f"Task {conversation.task_id} is still running. {RUNNING_HINT}")
                return
            if current.state == INPUT_REQUIRED:
                self.decide(current)
                return
        if self.base and "base" not in directives:
            text = f"{text} base={self.base}"
        rendered = self.call(lambda on_update: self.bridge.start(self.thread_key, text, self.requester, on_update=on_update))
        self.show(rendered)

    def refresh(self) -> Rendered | None:
        conversation = self.conversation()
        if conversation is None or not conversation.task_id:
            self.notice("Nothing is waiting for a decision in this conversation.")
            return None

        async def run(_: OnUpdate) -> Rendered:
            return await self.bridge.status(self.thread_key)

        return self.call(run, label="Checking")

    def explain_not_waiting(self, rendered: Rendered) -> None:
        task = rendered.task or {}
        task_id = str(task.get("id") or "")
        if rendered.state in NON_TERMINAL_STATES:
            self.notice(f"Task {task_id} is still running; there is nothing to decide yet. {RUNNING_HINT}")
            return
        self.notice(f"Nothing is waiting for a decision; the last task is {state_label(task)}.")

    def show(self, rendered: Rendered | None) -> None:
        if rendered is None:
            return
        text, stale = split_stale_note(rendered.text)
        task = rendered.task or {}
        if rendered.state == "refused":
            self.notice(text)
        elif rendered.state == INPUT_REQUIRED:
            self.decide(rendered)
        elif rendered.state == FAILED:
            failed_text, _ = split_stale_note(status_text(task) or text)
            self.say(views.failure(failed_text))
        elif rendered.state == CANCELED:
            self.notice("Canceled. Any branch the agent already pushed stays on the repository.")
        elif rendered.state in NON_TERMINAL_STATES:
            self.notice(f"Still running (task {task.get('id') or ''}). {RUNNING_HINT}")
        else:
            pull_request = artifacts(task).get("pull_request") or {}
            url = str(pull_request.get("url") or "") if isinstance(pull_request, dict) else ""
            if url and url not in self.reported_pull_requests:
                self.reported_pull_requests.add(url)
                self.say(views.success_line(f"Pull request opened: {url}"))
            else:
                self.say(views.answer(text))
        if stale:
            self.notice(STALE_PROPOSAL_NOTE)

    def decide(self, rendered: Rendered) -> None:
        task = rendered.task or {}
        action = pending_action(task) or {}
        action_id = str(action.get("id") or "")
        message, _ = split_stale_note(status_text(task) or rendered.text)
        if not action_id:
            self.say(views.answer(message))
            self.notice("The agent is waiting for your reply. Type it below.")
            return
        if message:
            self.say(views.answer(message))
        self.say(views.proposal(action))
        self.shown_action_id = action_id
        task_id = str(task.get("id") or "")
        while True:
            choice = self.io.choose("What do you want to do?", DECISIONS)
            if choice == APPROVE:
                self.approve(action_id)
                return
            if choice == DECLINE:
                self.decline()
                return
            if choice == SHOW_DIFF:
                url = str(action.get("compare_url") or "")
                self.say(url if url else views.notice_line("This proposal carries no diff link."))
                continue
            if choice == FOLLOW_UP:
                try:
                    text = self.io.ask("follow-up ›").strip()
                except (EOFError, KeyboardInterrupt):
                    text = ""
                if not text:
                    continue
                if text.startswith("/"):
                    if not self.handle(text):
                        self.quit_requested = True
                    return
                self.submit(text)
                return
            self.notice(f"The proposal is waiting. Type /approve or /decline when you are ready (task {task_id}).")
            return

    def approve(self, expected_action_id: str = "") -> None:
        current = self.refresh()
        if current is not None:
            self.approve_current(current, expected_action_id)

    def approve_current(self, current: Rendered, expected_action_id: str = "") -> None:
        if current.state != INPUT_REQUIRED:
            self.explain_not_waiting(current)
            return
        action_id = action_id_of(current)
        if not action_id:
            self.notice("The agent is waiting for a reply, not an approval. Type your answer.")
            return
        if expected_action_id and expected_action_id != action_id:
            self.notice("The proposal changed since it was shown. Here is the current one.")
            self.decide(current)
            return
        if action_id != self.shown_action_id:
            self.decide(current)
            return
        rendered = self.call(
            lambda on_update: self.bridge.approve(self.thread_key, self.requester, action_id, on_update=on_update),
            label="Opening the pull request",
        )
        self.show(rendered)

    def decline(self) -> None:
        current = self.refresh()
        if current is not None:
            self.decline_current(current)

    def decline_current(self, current: Rendered) -> None:
        if current.state != INPUT_REQUIRED or not action_id_of(current):
            self.explain_not_waiting(current)
            return
        action = pending_action(current.task or {}) or {}

        async def run(_: OnUpdate) -> Rendered:
            return await self.bridge.decline(self.thread_key, self.requester)

        rendered = self.call(run, label="Declining")
        if rendered is None:
            return
        if rendered.state != CANCELED:
            self.notice(
                f"The agent has not confirmed the decline yet (the task is {state_label(rendered.task or {})}). "
                "/status shows the current state."
            )
            return
        self.shown_action_id = ""
        branch = str(action.get("branch") or "")
        message = "Declined."
        if branch:
            message += f" The branch {branch} stays on the repository; delete it if you do not need it."
        self.notice(message)

    def remind_open_task(self) -> None:
        conversation = self.conversation()
        if conversation is None or not conversation.task_id:
            return
        task_id = conversation.task_id
        if conversation.task_state == INPUT_REQUIRED and conversation.pending_action_id:
            self.notice(f"A proposal is still waiting on task {task_id}. Approve it later with: ari approve {task_id}")
        elif conversation.task_state in NON_TERMINAL_STATES:
            self.notice(f"Task {task_id} is still running on the agent. Follow it with: ari tasks follow {task_id}")

    def cmd_help(self, _: str) -> None:
        self.say(views.help_table(COMMANDS))
        self.notice("While a proposal is waiting, typing yes or no also approves or declines it.")

    def cmd_repo(self, arg: str) -> None:
        if not arg:
            current = self.settings.default_repo_url
            self.say(f"Repository: {views.repo_label(current)}" if current else views.notice_line("No repository set."))
            return
        try:
            self.settings.check_repo_url(arg)
        except ConfigError as exc:
            self.say(views.error_line(exc))
            return
        self.settings.allowed_repo_hosts = ",".join(sorted(self.settings.allowed_hosts()))
        self.settings.default_repo_url = arg
        self.notice(f"Repository set to {views.repo_label(arg)} for your next requests.")

    def branch_summary(self) -> str:
        return views.branch_line(self.settings.default_repo_ref, self.base)

    def cmd_branch(self, arg: str) -> None:
        if arg:
            self.settings.default_repo_ref = arg
            self.notice(f"{self.branch_summary()}, from your next request on.")
            return
        self.say(self.branch_summary())

    def cmd_base(self, arg: str) -> None:
        if arg:
            self.base = arg
            self.notice(f"{self.branch_summary()}, from your next request on.")
            return
        self.say(self.branch_summary())

    def cmd_status(self, _: str) -> None:
        async def run(_: OnUpdate) -> Rendered:
            return await self.bridge.status(self.thread_key)

        self.show(self.call(run, label="Checking"))

    def cmd_wait(self, _: str) -> None:
        conversation = self.conversation()
        if conversation is None or not conversation.task_id:
            self.notice("No task in this conversation yet.")
            return

        async def run(on_update: OnUpdate) -> Rendered:
            return await self.bridge.follow(self.thread_key, on_update=on_update)

        self.show(self.call(run))

    def cmd_cancel(self, _: str) -> None:
        current = self.refresh()
        if current is None:
            return
        task_id = str((current.task or {}).get("id") or "")
        if current.state == INPUT_REQUIRED and action_id_of(current):
            self.decline_current(current)
            return
        if current.state not in NON_TERMINAL_STATES:
            self.notice(f"Nothing is running; the last task is {state_label(current.task or {})}.")
            return
        if not self.io.confirm(f"Stop task {task_id}? Anything it already pushed stays on the repository.", default=False):
            self.notice("Left it running.")
            return

        async def run(_: OnUpdate) -> Rendered:
            return await self.bridge.decline(self.thread_key, self.requester)

        rendered = self.call(run, label="Stopping")
        if rendered is not None and rendered.state == CANCELED:
            self.notice(f"Stopped task {task_id}.")
        elif rendered is not None:
            self.notice(f"The agent has not confirmed the stop yet (the task is {state_label(rendered.task or {})}).")

    def cmd_new(self, _: str) -> None:
        self.remind_open_task()
        self.thread_key = self.new_thread_key()
        self.shown_action_id = ""
        self.notice("Started a new conversation.")

    def cmd_config(self, _: str) -> None:
        from .config_file import config_path

        self.say(views.settings_table(self.settings.masked(), str(config_path())))

    def cmd_setup(self, _: str) -> None:
        if self.setup is None:
            self.notice("Setup is not available here.")
            return
        settings = self.setup()
        if settings is None:
            return
        self.remind_open_task()
        self.settings = settings
        self.bridge = self.make_bridge(settings)
        self.thread_key = self.new_thread_key()
        self.shown_action_id = ""
        self.show_banner()


def run_setup(
    io: Any,
    console: Console,
    current: dict[str, Any],
    check: Callable[[GatewaySettings], str],
    effective: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    console.print(views.setup_intro())
    effective = effective or {}
    values = dict(current)

    def default(name: str, fallback: str = "") -> str:
        return str(current.get(name) or effective.get(name) or fallback)

    def keep(name: str) -> bool:
        return bool(current.get(name))

    def secret(label: str, name: str) -> str:
        answer = io.secret(label, keep=keep(name)).strip()
        if answer == CLEAR:
            return ""
        return answer or str(current.get(name) or "")

    def optional(label: str, name: str) -> str:
        answer = io.text(label, default=default(name)).strip()
        return "" if answer == CLEAR else answer

    try:
        values["ari_agent_url"] = io.text("Agent URL", default=default("ari_agent_url", "http://localhost:8000")).strip()
        values["ari_api_key"] = secret("Agent API key", "ari_api_key")
        current_provider = default("llm_provider", str(GatewaySettings.model_fields["llm_provider"].default))
        index = PROVIDERS.index(current_provider) if current_provider in PROVIDERS else PROVIDERS.index("other")
        picked = io.choose("LLM provider", PROVIDERS, default=index)
        if picked is None:
            raise EOFError
        provider = PROVIDERS[picked]
        if provider == "other":
            provider = io.text("Provider name", default="" if current_provider in PROVIDERS else current_provider).strip()
        values["llm_provider"] = provider
        if provider != current_provider:
            values.pop("llm_base_url", None)
        model = default("llm_model") if provider == current_provider else ""
        values["llm_model"] = io.text("Model", default=model or DEFAULT_MODELS.get(provider, "")).strip()
        values["llm_api_key"] = secret("LLM API key", "llm_api_key")
        values["github_pat"] = secret("GitHub token (needed to push branches and open pull requests)", "github_pat")
        values["default_repo_url"] = optional("Default repository URL (optional)", "default_repo_url")
        values["default_repo_ref"] = optional("Branch to work from (optional)", "default_repo_ref")
    except (EOFError, KeyboardInterrupt):
        console.print(views.notice_line("Setup canceled; nothing was saved."))
        return None

    values = {k: v for k, v in values.items() if v not in ("", None)}
    try:
        if not values.get("llm_provider"):
            raise ConfigError("an LLM provider name is required")
        if urlsplit(str(values.get("ari_agent_url") or "")).scheme not in ("http", "https"):
            raise ConfigError("the agent URL must start with http:// or https://")
        settings = GatewaySettings(**values)
        settings.llm_config()
        if settings.default_repo_url:
            settings.check_repo_url(settings.default_repo_url)
    except ValidationError as exc:
        console.print(views.error_line(f"invalid setting: {validation_message(exc)}"))
        console.print(views.notice_line("Nothing was saved."))
        return None
    except ConfigError as exc:
        console.print(views.error_line(exc))
        console.print(views.notice_line("Nothing was saved."))
        return None

    try:
        name = check(settings)
        console.print(views.success_line(f"Connected to {name or settings.ari_agent_url}."))
    except (KeyboardInterrupt, *RECOVERABLE) as exc:
        reason = "the check was interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        console.print(views.error_line(f"could not reach the agent: {reason}"))
        if not io.confirm("Save these settings anyway?", default=False):
            console.print(views.notice_line("Nothing was saved."))
            return None
    return values
