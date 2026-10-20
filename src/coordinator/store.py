import json
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable

import structlog

logger = structlog.get_logger()

FILE_NAME = "coordinator-routes.json"
MAX_AGE_SECONDS = 7 * 24 * 3600
PROMPT_CHARS = 500


@dataclass(frozen=True)
class TaskLink:
    agent: str
    task_id: str
    context_id: str


class RouteStore:
    def __init__(self, root: str = "", *, max_age_seconds: float = MAX_AGE_SECONDS, clock: Callable[[], float] = time.time):
        self._path = os.path.join(root, FILE_NAME) if root else ""
        self._max_age = max_age_seconds
        self._clock = clock
        self._tasks: dict[str, dict[str, Any]] = {}
        self._contexts: dict[str, dict[str, Any]] = {}
        if self._path:
            self._load()

    def task(self, task_id: str) -> TaskLink | None:
        entry = self._tasks.get(task_id)
        if not entry:
            return None
        return TaskLink(entry["agent"], entry["task_id"], entry.get("context_id", ""))

    def link_task(self, task_id: str, link: TaskLink) -> None:
        self._tasks[task_id] = {"agent": link.agent, "task_id": link.task_id, "context_id": link.context_id, "updated": self._clock()}
        self.save()

    def context(self, context_id: str) -> dict[str, Any]:
        entry = self._contexts.get(context_id) or {}
        return {
            "agent": entry.get("agent", ""),
            "downstream": dict(entry.get("downstream") or {}),
            "turns": list(entry.get("turns") or []),
        }

    def remember(self, context_id: str, agent: str, downstream_context_id: str, prompt: str, *, max_turns: int) -> None:
        entry = self._contexts.setdefault(context_id, {"agent": "", "downstream": {}, "turns": []})
        entry["agent"] = agent
        if downstream_context_id:
            entry["downstream"][agent] = downstream_context_id
        turns = list(entry.get("turns") or []) + [{"agent": agent, "request": prompt[:PROMPT_CHARS]}]
        entry["turns"] = turns[-max_turns:] if max_turns > 0 else []
        entry["updated"] = self._clock()
        self.save()

    def save(self) -> None:
        self._prune()
        if not self._path:
            return
        data = {"version": 1, "tasks": self._tasks, "contexts": self._contexts}
        directory = os.path.dirname(self._path)
        try:
            os.makedirs(directory, exist_ok=True)
            handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(data, out)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("coordinator_route_store_write_failed", path=self._path, error=str(exc))

    def _prune(self) -> None:
        cutoff = self._clock() - self._max_age
        for table in (self._tasks, self._contexts):
            for key in [k for k, v in table.items() if float(v.get("updated") or 0) < cutoff]:
                del table[key]

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("coordinator_route_store_unreadable", path=self._path, error=str(exc))
            return
        tasks = data.get("tasks") if isinstance(data, dict) else None
        contexts = data.get("contexts") if isinstance(data, dict) else None
        for key, value in (tasks or {}).items():
            if isinstance(value, dict) and value.get("agent") and value.get("task_id"):
                self._tasks[str(key)] = value
        for key, value in (contexts or {}).items():
            if isinstance(value, dict):
                self._contexts[str(key)] = value
        self._prune()
