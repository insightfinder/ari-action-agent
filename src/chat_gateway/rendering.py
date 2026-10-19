from dataclasses import dataclass, field
from typing import Any

from .tasks import (
    CANCELED,
    COMPLETED,
    FAILED,
    INPUT_REQUIRED,
    SUBMITTED,
    WORKING,
    artifacts,
    pending_action,
    short_id,
    status_text,
    task_state,
)

STATE_LABELS = {
    SUBMITTED: "queued",
    WORKING: "working",
    INPUT_REQUIRED: "waiting for approval",
    COMPLETED: "done",
    FAILED: "failed",
    CANCELED: "canceled",
}

MAX_FILES_LISTED = 8
MAX_HUNK_CHARS = 1500


@dataclass(frozen=True)
class Button:
    action: str
    label: str
    task_id: str
    pending_action_id: str


@dataclass(frozen=True)
class Rendered:
    text: str
    state: str
    buttons: tuple[Button, ...] = ()
    links: tuple[str, ...] = ()
    task: dict | None = field(default=None, compare=False, hash=False, repr=False)


def state_label(task: dict[str, Any]) -> str:
    state = task_state(task)
    return STATE_LABELS.get(state, state.removeprefix("TASK_STATE_").lower() or "unknown")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def render_progress(task: dict[str, Any]) -> Rendered:
    produced = [name for name in artifacts(task) if name != "pending_action"]
    text = f"{state_label(task)} (task {short_id(_text(task.get('id')))})"
    if produced:
        text += "; produced: " + ", ".join(produced)
    return Rendered(text=text, state=task_state(task), task=task)


def render_proposal(action: dict[str, Any]) -> str:
    lines = [f"Proposed: {_text(action.get('action_name')) or _text(action.get('action')) or 'action'}"]
    repo = _text(action.get("repo")) or _text(action.get("repo_url"))
    if repo:
        lines.append(f"Repo: {repo} ({_text(action.get('branch')) or '?'} -> {_text(action.get('base_ref')) or '?'})")
    if action.get("title"):
        lines.append(f"Title: {_text(action['title'])}")
    files = action.get("changed_files")
    files = [_text(f) for f in files] if isinstance(files, list) else []
    if files:
        shown = ", ".join(files[:MAX_FILES_LISTED])
        if len(files) > MAX_FILES_LISTED:
            shown += f" (+{len(files) - MAX_FILES_LISTED} more)"
        lines.append(f"Files: {shown}")
    verification = action.get("verification")
    if isinstance(verification, dict) and verification:
        detail = _text(verification.get("detail"))
        lines.append(f"Verification: {_text(verification.get('status')) or 'unknown'}" + (f" ({detail})" if detail else ""))
    if action.get("compare_url"):
        lines.append(f"Full diff: {_text(action['compare_url'])}")
    hunks = _text(action.get("key_hunks"))
    if hunks:
        if len(hunks) > MAX_HUNK_CHARS:
            hunks = hunks[:MAX_HUNK_CHARS] + "\n... (truncated)"
        lines.append("```\n" + hunks + "\n```")
    return "\n".join(lines)


def render_pending(task: dict[str, Any]) -> Rendered:
    action = pending_action(task) or {}
    task_id = _text(task.get("id"))
    action_id = _text(action.get("id"))
    text_parts = [status_text(task) or "The agent is waiting for your approval."]
    if action:
        text_parts.append(render_proposal(action))
    buttons: tuple[Button, ...] = ()
    if action_id:
        buttons = (
            Button("approve", "Approve", task_id, action_id),
            Button("decline", "Decline", task_id, action_id),
        )
    compare_url = _text(action.get("compare_url"))
    links = (compare_url,) if compare_url else ()
    return Rendered(text="\n\n".join(text_parts), state=task_state(task), buttons=buttons, links=links, task=task)


def render_final(task: dict[str, Any]) -> Rendered:
    produced = artifacts(task)
    text = status_text(task) or f"{state_label(task)} (task {short_id(_text(task.get('id')))})"
    links: list[str] = []
    pr = produced.get("pull_request") or {}
    url = _text(pr.get("url")) if isinstance(pr, dict) else ""
    if url:
        text += f"\n\nPull request: {url}"
        links.append(url)
    return Rendered(text=text, state=task_state(task), links=tuple(links), task=task)


def render(task: dict[str, Any]) -> Rendered:
    state = task_state(task)
    if state in (SUBMITTED, WORKING):
        return render_progress(task)
    if state == INPUT_REQUIRED:
        return render_pending(task)
    return render_final(task)


def render_refusal(text: str) -> Rendered:
    return Rendered(text=text, state="refused")


def render_outage(error: object) -> Rendered:
    return Rendered(text=f"agent unavailable ({error}); still waiting, the run continues on the agent", state="unavailable")

def chunks(text: str, size: int) -> list[str]:
    remaining = text
    parts: list[str] = []
    while len(remaining) > size:
        cut = remaining.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        part = remaining[:cut]
        remaining = remaining[cut:].lstrip("\n")
        if part.count("```") % 2:
            part += "\n```"
            remaining = "```\n" + remaining
        parts.append(part)
    if remaining or not parts:
        parts.append(remaining)
    return parts



def proposal_text(rendered: Rendered) -> str:
    task = rendered.task or {}
    spoken = status_text(task)
    action = pending_action(task) or {}
    if not spoken or not action:
        return rendered.text
    details = []
    repo = action.get("repo") or action.get("repo_url")
    if repo:
        details.append(f"**Repository:** {repo}")
    if action.get("branch") or action.get("base_ref"):
        details.append(f"**Branch:** {action.get('branch') or '?'} → {action.get('base_ref') or '?'}")
    files = [str(f) for f in action.get("changed_files") or [] if f] if isinstance(action.get("changed_files"), list) else []
    if files:
        shown = ", ".join(files[:MAX_FILES_LISTED]) + (f" (+{len(files) - MAX_FILES_LISTED} more)" if len(files) > MAX_FILES_LISTED else "")
        details.append(f"**Files:** {shown}")
    verification = action.get("verification")
    if isinstance(verification, dict) and verification.get("status"):
        details.append(f"**Tests:** {verification['status']}")
    return spoken + ("\n\n" + "\n".join(details) if details else "")



def says_nothing_new(rendered: Rendered) -> bool:
    return rendered.state != INPUT_REQUIRED and not status_text(rendered.task or {}) and not rendered.links
