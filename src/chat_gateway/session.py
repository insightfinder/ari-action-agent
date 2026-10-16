import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .tasks import INPUT_REQUIRED, context_id_of, pending_action, task_state

logger = logging.getLogger("chat_gateway.session")

PERSISTED_FIELDS = ("thread_key", "requester", "context_id", "task_id", "task_state", "pending_action_id", "updated_at")


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
    def __init__(
        self,
        *,
        ttl_seconds: float = 86400.0,
        clock: Callable[[], float] = time.time,
        path: str | os.PathLike | None = None,
    ):
        self._ttl = ttl_seconds
        self._clock = clock
        self._path = Path(path) if path else None
        self._conversations: dict[str, Conversation] = {}
        if self._path is not None:
            self._load()

    def __len__(self) -> int:
        self.expire()
        return len(self._conversations)

    def expire(self) -> list[str]:
        now = self._clock()
        stale = [k for k, c in self._conversations.items() if now - c.updated_at > self._ttl]
        for key in stale:
            del self._conversations[key]
        if stale:
            self.save()
        return stale

    def get(self, thread_key: str) -> Conversation | None:
        self.expire()
        return self._conversations.get(thread_key)

    def start(self, thread_key: str, requester: str) -> Conversation:
        conversation = self.get(thread_key)
        if conversation is None:
            conversation = Conversation(thread_key=thread_key, requester=requester, updated_at=self._clock())
            self._conversations[thread_key] = conversation
            self.save()
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
        self.save()
        return conversation

    def forget(self, thread_key: str) -> None:
        if self._conversations.pop(thread_key, None) is not None:
            self.save()

    def save(self) -> None:
        if self._path is None:
            return
        data = {
            "version": 1,
            "conversations": [{name: getattr(c, name) for name in PERSISTED_FIELDS} for c in self._conversations.values()],
        }
        temporary = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(data, out)
            os.replace(temporary, self._path)
        except OSError as exc:
            logger.warning("could not save conversations to %s: %s", self._path, exc)

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("could not read conversations from %s, starting empty: %s", self._path, exc)
            return
        entries = data.get("conversations") if isinstance(data, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("thread_key"), str) or not entry["thread_key"]:
                continue
            try:
                conversation = Conversation(
                    thread_key=entry["thread_key"],
                    requester=str(entry.get("requester") or ""),
                    context_id=str(entry.get("context_id") or ""),
                    task_id=str(entry.get("task_id") or ""),
                    task_state=str(entry.get("task_state") or ""),
                    pending_action_id=str(entry.get("pending_action_id") or ""),
                    updated_at=float(entry.get("updated_at") or 0.0),
                )
            except (TypeError, ValueError):
                continue
            self._conversations[conversation.thread_key] = conversation
        self.expire()
