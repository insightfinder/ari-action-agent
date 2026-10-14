import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .tasks import INPUT_REQUIRED, context_id_of, pending_action, task_state


@dataclass
class Conversation:
    thread_key: str
    requester: str
    context_id: str = ""
    task_id: str = ""
    task_state: str = ""
    pending_action_id: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0


class SessionStore:
    def __init__(self, *, ttl_seconds: float = 86400.0, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._conversations: dict[str, Conversation] = {}

    def __len__(self) -> int:
        self.expire()
        return len(self._conversations)

    def expire(self) -> list[str]:
        now = self._clock()
        stale = [k for k, c in self._conversations.items() if now - c.updated_at > self._ttl]
        for key in stale:
            del self._conversations[key]
        return stale

    def get(self, thread_key: str) -> Conversation | None:
        self.expire()
        return self._conversations.get(thread_key)

    def start(self, thread_key: str, requester: str) -> Conversation:
        conversation = self.get(thread_key)
        if conversation is None:
            conversation = Conversation(thread_key=thread_key, requester=requester, updated_at=self._clock())
            self._conversations[thread_key] = conversation
        return conversation

    def record_task(self, thread_key: str, task: dict[str, Any], *, config: dict[str, Any] | None = None) -> Conversation:
        conversation = self._conversations[thread_key]
        conversation.task_id = task.get("id") or conversation.task_id
        conversation.context_id = context_id_of(task) or conversation.context_id
        conversation.task_state = task_state(task) or conversation.task_state
        action = pending_action(task)
        conversation.pending_action_id = action.get("id", "") if action and conversation.task_state == INPUT_REQUIRED else ""
        if config is not None:
            conversation.config = config
        conversation.updated_at = self._clock()
        return conversation

    def forget(self, thread_key: str) -> None:
        self._conversations.pop(thread_key, None)
