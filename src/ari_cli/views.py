from typing import Any
from urllib.parse import urlsplit

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

MAX_DIFF_LINES = 24
MAX_FILES = 8


def _s(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def repo_label(url: str) -> str:
    path = urlsplit(url).path.strip("/").removesuffix(".git")
    return path or url


def branch_line(ref: str, base: str) -> str:
    works_on = f"Works on branch {ref}" if ref else "Works on the repository's default branch"
    if base:
        return f"{works_on}; pull requests target {base}"
    return f"{works_on}; pull requests target {ref if ref else 'it'}"


def banner(agent_name: str, agent_url: str, repo_url: str, ref: str, base: str, problem: str = "") -> Panel:
    host = urlsplit(agent_url).netloc or agent_url
    body = Text()
    if problem:
        body.append(f"Cannot reach {host}: {problem}\n", style="red")
        body.append("Run /setup to change the connection.\n")
    else:
        body.append(f"Connected to {host}\n")
    if repo_url:
        body.append(f"Repository: {repo_label(repo_url)}\n")
        body.append(branch_line(ref, base) + "\n")
    else:
        body.append("No repository set yet. Use /repo <url> before asking for a change.\n")
    body.append("Type a request, or /help for commands.", style="dim")
    return Panel(body, title=Text(agent_name or "ARI"), title_align="left", border_style="cyan")


def answer(text: str) -> Group:
    return Group(Text("ARI ›", style="bold cyan"), Padding(Markdown(text or "(no answer)"), (0, 0, 0, 2)))


def error_line(message: object) -> Text:
    return Text.assemble(("Error: ", "bold red"), str(message))


def notice_line(message: str) -> Text:
    return Text(message, style="yellow")


def success_line(message: str) -> Text:
    return Text.assemble(("✔ ", "bold green"), message)


def failure(message: str) -> Group:
    parts: list[RenderableType] = [Text.assemble(("✖ ", "bold red"), "The agent could not finish this request.")]
    if message:
        parts.append(Padding(Markdown(message), (0, 0, 0, 2)))
    return Group(*parts)


def proposal(action: dict[str, Any]) -> Panel:
    title = _s(action.get("title")) or _s(action.get("action_name")) or "Proposed change"
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", no_wrap=True)
    meta.add_column(overflow="fold")
    repo = _s(action.get("repo")) or repo_label(_s(action.get("repo_url")))
    if repo:
        meta.add_row("Repository", Text(repo))
    branch = _s(action.get("branch"))
    if branch:
        meta.add_row("Branch", Text(f"{branch} → {_s(action.get('base_ref')) or '?'}"))
    files = action.get("changed_files")
    files = [_s(f) for f in files] if isinstance(files, list) else []
    if files:
        shown = ", ".join(files[:MAX_FILES])
        if len(files) > MAX_FILES:
            shown += f" (+{len(files) - MAX_FILES} more)"
        meta.add_row("Files", Text(shown))
    verification = action.get("verification")
    if isinstance(verification, dict) and verification:
        detail = _s(verification.get("detail"))
        meta.add_row("Tests", Text((_s(verification.get("status")) or "unknown") + (f" ({detail})" if detail else "")))
    compare_url = _s(action.get("compare_url"))
    if compare_url:
        meta.add_row("Full diff", Text(compare_url))
    parts: list[RenderableType] = [Text(title, style="bold"), meta]
    hunks = _s(action.get("key_hunks"))
    if hunks:
        lines = hunks.splitlines()
        shown_hunks = "\n".join(lines[:MAX_DIFF_LINES])
        if len(lines) > MAX_DIFF_LINES:
            shown_hunks += f"\n... ({len(lines) - MAX_DIFF_LINES} more lines)"
        parts.append(Text(""))
        parts.append(Syntax(shown_hunks, "diff", theme="ansi_dark", background_color="default", word_wrap=True))
    return Panel(Group(*parts), title="Proposed change", title_align="left", border_style="magenta")


def help_table(commands: dict[str, str]) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    for command, description in commands.items():
        table.add_row(Text(command), Text(description))
    return table


def settings_table(masked: dict[str, Any], path: str) -> Group:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(overflow="fold")
    for name, value in masked.items():
        table.add_row(Text(name), Text(_s(value)))
    return Group(Text(f"Settings file: {path}", style="dim"), table)


def setup_intro() -> Panel:
    return Panel(
        Text(
            "Let's connect ari to an ARI agent.\n"
            "Press Enter to keep the value shown in brackets, or type - to clear it.\n"
            "Secrets are not echoed.",
        ),
        title="Setup",
        title_align="left",
        border_style="cyan",
    )
