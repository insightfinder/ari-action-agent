import asyncio
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Iterable

import typer
from rich.console import Console
from rich.text import Text

KEEP_HINT = " (Enter keeps the current one)"


def safe_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        isatty = getattr(stream, "isatty", None)
        if reconfigure is None or isatty is None or isatty():
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def drain_input() -> None:
    try:
        if not sys.stdin.isatty():
            return
        if sys.platform == "win32":
            import msvcrt

            while msvcrt.kbhit():
                msvcrt.getwch()
        else:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError, ImportError):
        pass


class Progress:
    def __init__(self) -> None:
        self.hint = ""

    def set(self, hint: str) -> None:
        self.hint = hint


class PlainProgress(Progress):
    def __init__(self, console: Console) -> None:
        super().__init__()
        self.console = console

    def set(self, hint: str) -> None:
        if hint and hint != self.hint:
            self.console.print(Text(f"  … {hint}"))
        super().set(hint)


class PlainIO:
    interactive = False

    def __init__(self, console: Console) -> None:
        self.console = console

    def ask(self, label: str) -> str:
        return input(f"{label} ")

    def text(self, label: str, default: str = "") -> str:
        try:
            return typer.prompt(label, default=default, show_default=bool(default))
        except typer.Abort:
            raise EOFError from None

    def secret(self, label: str, keep: bool = False) -> str:
        prompt = label + (KEEP_HINT if keep else "")
        if not sys.stdin.isatty():
            typer.echo(f"{prompt}: ", nl=False)
            line = sys.stdin.readline()
            typer.echo("")
            if not line:
                raise EOFError
            return line.rstrip("\r\n")
        try:
            return typer.prompt(prompt, default="", show_default=False, hide_input=True)
        except typer.Abort:
            raise EOFError from None

    def choose(self, question: str, options: list[str], default: int | None = None) -> int | None:
        self.console.print(Text(question))
        for number, option in enumerate(options, 1):
            marker = " (current)" if default is not None and number - 1 == default else ""
            self.console.print(Text(f"  {number}) {option}{marker}"))
        label = f"Choose 1-{len(options)}"
        while True:
            try:
                if default is None:
                    raw = typer.prompt(label)
                else:
                    raw = typer.prompt(label, default=str(default + 1))
            except typer.Abort:
                return None
            if raw.strip().isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            self.console.print(Text(f"Enter a number from 1 to {len(options)}."))

    def confirm(self, question: str, default: bool = False) -> bool:
        try:
            return typer.confirm(question, default=default)
        except typer.Abort:
            return False

    @asynccontextmanager
    async def progress(self, label: str) -> AsyncIterator[Progress]:
        self.console.print(Text(f"  … {label}"))
        yield PlainProgress(self.console)


class PromptIO:
    interactive = True

    def __init__(self, console: Console, history_file: Path, commands: Iterable[str]) -> None:
        import questionary
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory, InMemoryHistory

        names = sorted(commands)

        class SlashCompleter(Completer):
            def get_completions(self, document, complete_event):
                typed = document.text_before_cursor
                if not typed.startswith("/") or " " in typed:
                    return
                for name in names:
                    if name.startswith(typed):
                        yield Completion(name, start_position=-len(typed))

        try:
            history_file.parent.mkdir(parents=True, exist_ok=True)
            with history_file.open("a", encoding="utf-8"):
                pass
            history = FileHistory(str(history_file))
        except OSError:
            history = InMemoryHistory()

        self.console = console
        self._questionary = questionary
        self._session = PromptSession(history=history, completer=SlashCompleter(), complete_while_typing=True)

    def ask(self, label: str) -> str:
        return self._session.prompt([("ansigreen bold", f"{label} ")])

    def text(self, label: str, default: str = "") -> str:
        value = self._questionary.text(label, default=default).ask()
        if value is None:
            raise EOFError
        return value

    def secret(self, label: str, keep: bool = False) -> str:
        value = self._questionary.password(label + (KEEP_HINT if keep else "")).ask()
        if value is None:
            raise EOFError
        return value

    def choose(self, question: str, options: list[str], default: int | None = None) -> int | None:
        drain_input()
        selected = options[default] if default is not None else None
        answer = self._questionary.select(question, choices=options, default=selected).ask()
        return None if answer is None else options.index(answer)

    def confirm(self, question: str, default: bool = False) -> bool:
        drain_input()
        return bool(self._questionary.confirm(question, default=default).ask())

    @asynccontextmanager
    async def progress(self, label: str) -> AsyncIterator[Progress]:
        state = Progress()
        started = time.monotonic()

        def line() -> str:
            elapsed = int(time.monotonic() - started)
            hint = f" · {state.hint}" if state.hint else ""
            return f"{label}… {elapsed // 60}:{elapsed % 60:02d}{hint}"

        with self.console.status(Text(line())) as status:

            async def tick() -> None:
                while True:
                    status.update(Text(line()))
                    await asyncio.sleep(0.5)

            ticker = asyncio.create_task(tick())
            try:
                yield state
            finally:
                ticker.cancel()


def make_io(console: Console, history_file: Path, commands: Iterable[str]) -> PlainIO | PromptIO:
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            return PromptIO(console, history_file, commands)
        except ImportError:
            pass
    return PlainIO(console)
