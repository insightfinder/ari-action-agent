from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"

NON_TERMINAL_STATES = frozenset({SUBMITTED, WORKING})


def task_state(task: dict[str, Any]) -> str:
    status = task.get("status") if isinstance(task, dict) else None
    state = status.get("state") if isinstance(status, dict) else None
    return state if isinstance(state, str) else ""


def context_id_of(task: dict[str, Any]) -> str:
    value = task.get("contextId") or task.get("context_id") or ""
    return value if isinstance(value, str) else ""


def status_text(task: dict[str, Any]) -> str:
    status = task.get("status") if isinstance(task, dict) else None
    message = status.get("message") if isinstance(status, dict) else None
    parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list):
        return ""
    texts = [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"]]
    return "\n".join(texts).strip()


def artifacts(task: dict[str, Any]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    entries = task.get("artifacts") if isinstance(task, dict) else None
    for artifact in entries if isinstance(entries, list) else []:
        if not isinstance(artifact, dict):
            continue
        name = artifact.get("name")
        parts = artifact.get("parts")
        if not isinstance(name, str) or not name or not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("data"), dict):
                found[name] = part["data"]
                break
    return found


def pending_action(task: dict[str, Any]) -> dict | None:
    return artifacts(task).get("pending_action")


def is_running(task: dict[str, Any]) -> bool:
    return task_state(task) in NON_TERMINAL_STATES


def is_paused(task: dict[str, Any]) -> bool:
    return task_state(task) == INPUT_REQUIRED


def is_finished(task: dict[str, Any]) -> bool:
    state = task_state(task)
    return bool(state) and state not in NON_TERMINAL_STATES and state != INPUT_REQUIRED


def short_id(task_id: str) -> str:
    return task_id[:8]
