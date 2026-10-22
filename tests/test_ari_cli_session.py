import io
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("typer")

from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_cli.interactive import ChatSession, hint_for
from ari_cli.terminal import Progress
from chat_gateway.config import GatewaySettings
from chat_gateway.rendering import Rendered, render_outage
from chat_gateway.session import SessionStore


class ScriptedIO:
    def __init__(self, lines):
        self.lines = list(lines)

    def ask(self, label):
        if not self.lines:
            raise EOFError
        item = self.lines.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def choose(self, question, options, default=0):
        return None

    @asynccontextmanager
    async def progress(self, label):
        yield Progress()


class Client:
    async def agent_card(self, **kwargs):
        return {"name": "Agent"}


class InterruptedBridge:
    def __init__(self):
        self.client = Client()
        self.store = SessionStore()

    async def start(self, thread_key, text, requester, **kwargs):
        self.store.start(thread_key, requester)
        self.store.record_task(thread_key, {"id": "task-9", "contextId": "c", "status": {"state": "TASK_STATE_WORKING"}})
        raise KeyboardInterrupt


def test_ctrl_c_while_waiting_stops_following_and_ctrl_c_at_the_prompt_does_not_quit():
    buffer = io.StringIO()
    settings = GatewaySettings(llm_api_key="k", default_repo_url="https://github.com/org/repo")
    scripted = ScriptedIO(["fix it", KeyboardInterrupt(), "/quit", "never read"])
    session = ChatSession(
        settings,
        scripted,
        Console(file=buffer, width=120),
        make_bridge=lambda s: InterruptedBridge(),
    )
    session.run()
    out = buffer.getvalue()
    assert scripted.lines == ["never read"]
    flat = " ".join(out.split())
    assert "Stopped waiting. Task task-9 keeps running on the agent; /wait follows it again, /cancel stops it." in flat
    assert "Press Ctrl-D or type /quit to leave." in flat
    assert "Task task-9 is still running on the agent. Follow it with: ari tasks follow task-9" in flat


def test_progress_hints_name_what_the_agent_has_produced():
    assert hint_for(render_outage("HTTP 503")) == "the agent is restarting, still waiting"
    task = {
        "id": "t",
        "status": {"state": "TASK_STATE_WORKING"},
        "artifacts": [{"name": "root_cause", "parts": [{"data": {"summary": "x"}}]}],
    }
    assert hint_for(Rendered(text="", state="TASK_STATE_WORKING", task=task)) == "root cause found"
    assert hint_for(Rendered(text="", state="TASK_STATE_WORKING")) == ""
