import asyncio
import uuid
from typing import Any, Awaitable, Callable

from chat_gateway.a2a_client import AgentError
from chat_gateway.bridge import Bridge, decision_word, parse_directives
from chat_gateway.config import ConfigError
from chat_gateway.rendering import STATE_LABELS, Rendered
from chat_gateway.session import Conversation
from chat_gateway.tasks import INPUT_REQUIRED, NON_TERMINAL_STATES, pending_action

Progress = Callable[[str], Awaitable[None]]
Confirm = Callable[[str], Awaitable[str]]

KEY_PREFIX = "mcp:"

RUNNING_HINT = (
    "The agent is still working. Call ari_status with this conversation_id to keep following it; "
    "the work continues on the agent in the meantime."
)
PROPOSAL_HINT = (
    "A proposal is waiting for a person's decision. Show the user the proposal above, including the full diff link. "
    "Call ari_approve only after the user has explicitly told you to approve this proposal; never approve on your own. "
    "Call ari_decline if the user does not want it, or ari_ask with the same conversation_id to ask for changes."
)
DONE_HINT = "Finished. Use ari_ask with the same conversation_id for a follow-up, or without one to start something new."


class UserFacingError(Exception):
    pass


def new_conversation_id() -> str:
    return uuid.uuid4().hex


def describe(conversation_id: str, rendered: Rendered, conversation: Conversation | None) -> str:
    if rendered.state == "refused":
        return f"Not done: {rendered.text}\n\nconversation_id: {conversation_id}"
    lines = [f"conversation_id: {conversation_id}"]
    if conversation is not None and conversation.task_id:
        lines.append(f"task_id: {conversation.task_id}")
    lines.append(f"state: {STATE_LABELS.get(rendered.state, rendered.state.removeprefix('TASK_STATE_').lower())}")
    if rendered.state == INPUT_REQUIRED and conversation is not None and conversation.pending_action_id:
        lines.append(f"proposal_id: {conversation.pending_action_id}")
    lines.append("")
    lines.append(rendered.text)
    lines.append("")
    if rendered.state in NON_TERMINAL_STATES or rendered.state == "unavailable":
        lines.append(RUNNING_HINT)
    elif rendered.state == INPUT_REQUIRED:
        lines.append(PROPOSAL_HINT)
    else:
        lines.append(DONE_HINT)
    return "\n".join(lines)


def proposal_summary(task: dict[str, Any]) -> str:
    action = pending_action(task) or {}
    parts = [str(action.get("title") or action.get("action_name") or "the proposed change")]
    repo = action.get("repo") or action.get("repo_url")
    if repo:
        parts.append(f"Repository: {repo}")
    if action.get("branch") or action.get("base_ref"):
        parts.append(f"Branch: {action.get('branch') or '?'} -> {action.get('base_ref') or '?'}")
    files = action.get("changed_files")
    if isinstance(files, list) and files:
        parts.append("Files: " + ", ".join(str(f) for f in files[:8]) + (" ..." if len(files) > 8 else ""))
    if action.get("compare_url"):
        parts.append(f"Full diff: {action['compare_url']}")
    return "\n".join(parts)


class AriService:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def key(conversation_id: str) -> str:
        return KEY_PREFIX + conversation_id

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def _conversation(self, conversation_id: str, actor: str) -> Conversation:
        conversation_id = (conversation_id or "").strip()
        if not conversation_id:
            raise UserFacingError("conversation_id is required")
        conversation = self.bridge.store.get(self.key(conversation_id))
        if conversation is None:
            raise UserFacingError(
                f"conversation {conversation_id} is unknown or has expired; start a new one with ari_ask"
            )
        if not self.bridge.can_take_part(conversation, actor):
            raise UserFacingError(f"conversation {conversation_id} belongs to {conversation.requester}")
        return conversation

    async def _guarded(self, key: str, work: Callable[[], Awaitable[Rendered]]) -> Rendered:
        lock = self._lock(key)
        if lock.locked():
            raise UserFacingError("another call on this conversation is still running; try again in a moment")
        async with lock:
            try:
                return await work()
            except TimeoutError:
                pass
            except ConfigError as exc:
                raise UserFacingError(str(exc)) from None
            except AgentError as exc:
                if exc.status_code == 503:
                    raise UserFacingError(
                        "the agent is temporarily unavailable (HTTP 503), usually because it is restarting; "
                        "try the same call again in a minute"
                    ) from None
                raise UserFacingError(f"the agent refused the request: {exc}") from None
            except RuntimeError as exc:
                raise UserFacingError(f"the agent could not be reached: {exc}") from None
            try:
                return await self.bridge.status(key)
            except (AgentError, RuntimeError) as exc:
                raise UserFacingError(f"the agent could not be reached: {exc}") from None

    def _updates(self, progress: Progress | None):
        if progress is None:
            return None

        async def on_update(rendered: Rendered) -> None:
            await progress(rendered.text)

        return on_update

    async def ask(
        self,
        actor: str,
        request: str,
        conversation_id: str = "",
        repo: str = "",
        ref: str = "",
        base: str = "",
        progress: Progress | None = None,
    ) -> str:
        request = (request or "").strip()
        if not request:
            raise UserFacingError("request is empty")
        if conversation_id:
            conversation = self._conversation(conversation_id, actor)
            if conversation.task_state == INPUT_REQUIRED and decision_word(parse_directives(request)[0]):
                raise UserFacingError(
                    "a proposal is waiting; approving or declining is done with ari_approve or ari_decline, not with ari_ask"
                )
        else:
            conversation_id = new_conversation_id()
        text = request
        for name, value in (("repo", repo), ("ref", ref), ("base", base)):
            if value and value.strip():
                text += f" {name}={value.strip()}"
        key = self.key(conversation_id)
        rendered = await self._guarded(key, lambda: self.bridge.start(key, text, actor, on_update=self._updates(progress)))
        return describe(conversation_id, rendered, self.bridge.store.get(key))

    async def status(self, actor: str, conversation_id: str, progress: Progress | None = None) -> str:
        conversation = self._conversation(conversation_id, actor)
        key = self.key(conversation_id)
        if conversation.task_state in NON_TERMINAL_STATES:
            rendered = await self._guarded(key, lambda: self.bridge.follow(key, on_update=self._updates(progress)))
        else:
            rendered = await self._guarded(key, lambda: self.bridge.status(key))
        return describe(conversation_id, rendered, self.bridge.store.get(key))

    async def prepare_approval(self, actor: str, conversation_id: str, proposal_id: str) -> tuple[str, Rendered | None]:
        conversation = self._conversation(conversation_id, actor)
        key = self.key(conversation_id)
        if not self.bridge.can_approve(conversation, actor):
            return describe(conversation_id, self.bridge.approval_refusal(conversation, actor), conversation), None
        current = await self._guarded(key, lambda: self.bridge.status(key))
        conversation = self.bridge.store.get(key)
        if current.state != INPUT_REQUIRED or conversation is None or not conversation.pending_action_id:
            raise UserFacingError("nothing in this conversation is waiting for approval")
        if (proposal_id or "").strip() != conversation.pending_action_id:
            raise UserFacingError(
                "proposal_id does not match the proposal currently waiting; call ari_status, show the user the current proposal, and ask again"
            )
        return proposal_summary(current.task or {}), current

    async def approve(
        self,
        actor: str,
        conversation_id: str,
        proposal_id: str,
        confirm: Confirm,
        *,
        require_confirmation: bool = False,
        progress: Progress | None = None,
    ) -> str:
        summary, current = await self.prepare_approval(actor, conversation_id, proposal_id)
        if current is None:
            return summary
        answer = await confirm(summary)
        return await self.finish_approval(
            actor, conversation_id, proposal_id, answer, current, require_confirmation=require_confirmation, progress=progress
        )

    async def finish_approval(
        self,
        actor: str,
        conversation_id: str,
        proposal_id: str,
        answer: str,
        current: Rendered,
        *,
        require_confirmation: bool = False,
        progress: Progress | None = None,
    ) -> str:
        key = self.key(conversation_id)
        conversation = self.bridge.store.get(key)
        if answer == "unsupported" and require_confirmation:
            raise UserFacingError(
                "this app cannot ask you to confirm an approval, and this server requires that confirmation; "
                "approve the proposal from Slack, Teams or the ari CLI instead"
            )
        if answer in ("decline", "cancel"):
            return describe(
                conversation_id,
                Rendered(text="Not approved: the confirmation was declined. The proposal is still waiting.", state=INPUT_REQUIRED, task=current.task),
                conversation,
            )
        rendered = await self._guarded(
            key,
            lambda: self.bridge.approve(key, actor, pending_action_id=proposal_id.strip(), on_update=self._updates(progress)),
        )
        return describe(conversation_id, rendered, self.bridge.store.get(key))

    async def decline(self, actor: str, conversation_id: str) -> str:
        self._conversation(conversation_id, actor)
        key = self.key(conversation_id)
        rendered = await self._guarded(key, lambda: self.bridge.decline(key, actor))
        return describe(conversation_id, rendered, self.bridge.store.get(key))
