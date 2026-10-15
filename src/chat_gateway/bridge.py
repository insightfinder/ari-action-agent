import re
from dataclasses import replace
from typing import Any, Awaitable, Callable

from .a2a_client import AgentClient
from .config import GatewaySettings
from .rendering import Rendered, render, render_outage, render_progress, render_refusal, state_label
from .session import Conversation, SessionStore
from .tasks import INPUT_REQUIRED, NON_TERMINAL_STATES, context_id_of, is_running, pending_action, task_state

DIRECTIVE_RE = re.compile(r"(?<!\S)(repo|ref|base)[=:](\S+)")

APPROVAL_WORDS = frozenset(
    {"yes", "y", "yes please", "approve", "approved", "approve it", "ok", "okay", "lgtm", "go ahead", "do it", "ship it", "confirm", "confirmed"}
)
DECLINE_WORDS = frozenset(
    {"no", "n", "nope", "decline", "declined", "reject", "rejected", "cancel", "cancel it", "stop", "abort", "dont", "do not"}
)

STALE_PROPOSAL_NOTE = "The earlier proposal on this task can no longer be approved; ask for the change again to get a new one."

OnUpdate = Callable[[Rendered], Awaitable[None]]


def base_hint(base: str) -> str:
    return f"If this request leads to a pull request, open it against the {base} branch."


def parse_directives(text: str) -> tuple[str, dict[str, str]]:
    directives: dict[str, str] = {}

    def take(match: re.Match) -> str:
        value = match.group(2).rstrip(",;.!?").strip("`'\"<>")
        directives[match.group(1)] = value.split("|", 1)[0]
        return ""

    cleaned = DIRECTIVE_RE.sub(take, text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip(), directives


def decision_word(text: str) -> str:
    words = re.sub(r"[^a-z ]", " ", re.sub(r"['’]", "", text.lower()))
    words = " ".join(words.split())
    if words in APPROVAL_WORDS:
        return "approve"
    if words in DECLINE_WORDS:
        return "decline"
    return ""


class Bridge:
    def __init__(self, client: AgentClient, settings: GatewaySettings, store: SessionStore | None = None):
        self.client = client
        self.settings = settings
        self.store = store if store is not None else SessionStore()

    def build_config(self, thread_key: str, directives: dict[str, str], *, require_pat: bool = False) -> dict[str, Any]:
        repo_url = directives.get("repo") or self.settings.repo_for(thread_key)
        return {
            "repo": self.settings.repo_config(repo_url, directives.get("ref", ""), require_pat=require_pat),
            "llm": self.settings.llm_config(),
            "options": self.settings.options(),
        }

    def can_approve(self, conversation: Conversation, actor: str) -> bool:
        return actor == conversation.requester or actor in self.settings.approver_set()

    async def _refresh(self, thread_key: str, conversation: Conversation) -> dict[str, Any] | None:
        if not conversation.task_id:
            return None
        task = await self.client.get_task(conversation.task_id)
        self.store.record_task(thread_key, task)
        return task

    async def _follow(self, thread_key: str, task_id: str, on_update: OnUpdate | None) -> Rendered:
        async def progress(task: dict[str, Any]) -> None:
            self.store.record_task(thread_key, task)
            if on_update is not None and is_running(task):
                await on_update(render_progress(task))

        async def outage(error: Exception) -> None:
            if on_update is not None:
                await on_update(render_outage(error))

        task = await self.client.wait(
            task_id,
            poll_seconds=self.settings.poll_seconds,
            budget_seconds=self.settings.wait_seconds,
            outage_seconds=self.settings.agent_outage_seconds,
            on_update=progress,
            on_outage=outage,
        )
        self.store.record_task(thread_key, task)
        return render(task)

    async def start(
        self, thread_key: str, text: str, requester: str, on_update: OnUpdate | None = None
    ) -> Rendered:
        conversation = self.store.start(thread_key, requester)
        if conversation.task_state in NON_TERMINAL_STATES:
            await self._refresh(thread_key, conversation)
            if conversation.task_state in NON_TERMINAL_STATES:
                return render_refusal(
                    f"still working on task {conversation.task_id[:8]}; wait for it to finish or decline it first"
                )
        continuing = conversation.task_state == INPUT_REQUIRED
        if continuing:
            decision = decision_word(parse_directives(text)[0]) if conversation.pending_action_id else ""
            if decision == "approve":
                return await self.approve(thread_key, requester, on_update=on_update)
            if decision == "decline":
                return await self.decline(thread_key, requester)
            if not self.can_approve(conversation, requester):
                return render_refusal(
                    f"a proposal requested by {conversation.requester} is waiting for approval; "
                    f"only {conversation.requester} or a configured approver can continue this task"
                )
        prompt, directives = parse_directives(text)
        if not prompt:
            return render_refusal("nothing to do: the message is empty")
        if directives.get("base"):
            prompt += "\n" + base_hint(directives["base"])
        config = self.build_config(thread_key, directives)
        if not continuing:
            conversation.requester = requester
        task_id = conversation.task_id if continuing else ""
        submitted = await self.client.send(prompt, config, context_id=conversation.context_id, task_id=task_id)
        self.store.record_task(thread_key, submitted, config=config)
        rendered = await self._follow(thread_key, submitted["id"], on_update)
        if continuing and rendered.state != INPUT_REQUIRED:
            rendered = replace(
                rendered,
                text=f"{rendered.text}\n\n{STALE_PROPOSAL_NOTE}",
            )
        return rendered

    def _recover(self, thread_key: str, actor: str, task_id: str) -> Conversation | Rendered:
        conversation = self.store.get(thread_key)
        if conversation is not None and conversation.task_id:
            return conversation
        if not task_id:
            return render_refusal("nothing is waiting for approval in this conversation")
        if actor not in self.settings.approver_set():
            return render_refusal(
                f"this conversation is no longer in memory; only a configured approver can act on task {task_id[:8]}"
            )
        conversation = self.store.start(thread_key, actor)
        conversation.requester = actor
        conversation.task_id = task_id
        return conversation

    async def approve(
        self,
        thread_key: str,
        actor: str,
        pending_action_id: str = "",
        task_id: str = "",
        on_update: OnUpdate | None = None,
    ) -> Rendered:
        recovered = self._recover(thread_key, actor, task_id)
        if isinstance(recovered, Rendered):
            return recovered
        conversation = recovered
        if not self.can_approve(conversation, actor):
            return render_refusal(f"only {conversation.requester} or a configured approver can approve this")
        task = await self._refresh(thread_key, conversation)
        if task is None or task_state(task) != INPUT_REQUIRED or not conversation.pending_action_id:
            label = state_label(task) if task else "unknown"
            return render_refusal(f"the task is no longer waiting for approval (it is {label}); ask for the change again")
        if pending_action_id and pending_action_id != conversation.pending_action_id:
            return render_refusal("that proposal is not the one currently waiting; it may be stale")
        if not conversation.config:
            action = pending_action(task) or {}
            conversation.config = self.build_config(thread_key, {"repo": str(action.get("repo_url") or "")}, require_pat=True)
        if not context_id_of(task):
            task = {**task, "contextId": conversation.context_id}
        resumed = await self.client.confirm(
            task, conversation.pending_action_id, conversation.config, text=f"approved by {actor}"
        )
        self.store.record_task(thread_key, resumed)
        return await self._follow(thread_key, resumed.get("id") or conversation.task_id, on_update)

    async def decline(self, thread_key: str, actor: str, task_id: str = "") -> Rendered:
        recovered = self._recover(thread_key, actor, task_id)
        if isinstance(recovered, Rendered):
            return recovered
        conversation = recovered
        if not self.can_approve(conversation, actor):
            return render_refusal(f"only {conversation.requester} or a configured approver can decline this")
        result = await self.client.cancel(conversation.task_id)
        task = result if result.get("id") else await self.client.get_task(conversation.task_id)
        self.store.record_task(thread_key, task)
        return render(task)

    async def follow(self, thread_key: str, on_update: OnUpdate | None = None) -> Rendered:
        conversation = self.store.get(thread_key)
        if conversation is None or not conversation.task_id:
            return render_refusal("no task in this conversation yet")
        return await self._follow(thread_key, conversation.task_id, on_update)

    async def status(self, thread_key: str) -> Rendered:
        conversation = self.store.get(thread_key)
        if conversation is None or not conversation.task_id:
            return render_refusal("no task in this conversation yet")
        task = await self.client.get_task(conversation.task_id)
        self.store.record_task(thread_key, task)
        return render(task)
