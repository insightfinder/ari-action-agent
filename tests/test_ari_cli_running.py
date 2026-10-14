import io
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

pytest.importorskip("typer")

from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_cli.interactive import ChatSession
from ari_cli.terminal import Progress
from chat_gateway.a2a_client import AgentClient
from chat_gateway.bridge import Bridge
from chat_gateway.config import GatewaySettings
from tests.ari_cli_fakes import PENDING, FakeAgent, _task

RUNNING_HINT = "/wait follows it again, /cancel stops it."


class ScriptedIO:
    def __init__(self, lines, confirm=False):
        self.lines = list(lines)
        self.answer = confirm
        self.confirmations = []

    def ask(self, label):
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)

    def choose(self, question, options, default=None):
        return None

    def confirm(self, question, default=False):
        self.confirmations.append(question)
        return self.answer

    @asynccontextmanager
    async def progress(self, label):
        yield Progress()


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in GatewaySettings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("common.http_retry.backoff_seconds", lambda attempt: 0)


def session_for(fake, lines, confirm=False, running=True):
    settings = GatewaySettings(
        llm_api_key="k", github_pat="p", default_repo_url="https://github.com/org/repo", poll_seconds=0
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), timeout=5)
    scripted = ScriptedIO(lines, confirm=confirm)
    buffer = io.StringIO()
    session = ChatSession(
        settings,
        scripted,
        Console(file=buffer, width=200),
        make_bridge=lambda s: Bridge(AgentClient("http://agent.test", "k", http=http), s),
    )
    if running:
        session.bridge.store.start(session.thread_key, "cli")
        session.bridge.store.record_task(
            session.thread_key, {"id": "task-1", "contextId": "ctx-1", "status": {"state": "TASK_STATE_WORKING"}}
        )
    return session, scripted, buffer


def output(buffer) -> str:
    return " ".join(buffer.getvalue().split())


def test_wait_follows_a_task_again_and_shows_its_proposal(clean_env):
    fake = FakeAgent()
    fake.script("task-1", _task("task-1", "TASK_STATE_WORKING"), _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    session, _, buffer = session_for(fake, ["/wait"])
    session.run()
    assert "Proposed change" in output(buffer)
    assert fake.sends == []


def test_a_message_while_the_task_runs_is_held_back_with_a_hint(clean_env):
    fake = FakeAgent()
    fake.script("task-1", _task("task-1", "TASK_STATE_WORKING"))
    session, _, buffer = session_for(fake, ["also add a test"])
    session.run()
    assert f"Task task-1 is still running. {RUNNING_HINT}" in output(buffer)
    assert fake.sends == []


def test_a_message_after_the_task_paused_shows_the_proposal_first(clean_env):
    fake = FakeAgent()
    fake.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    session, _, buffer = session_for(fake, ["hello"])
    session.run()
    assert "Proposed change" in output(buffer)
    assert fake.sends == []


def test_cancel_asks_first_and_stops_the_running_task(clean_env):
    fake = FakeAgent()
    fake.script("task-1", _task("task-1", "TASK_STATE_WORKING"))
    session, scripted, buffer = session_for(fake, ["/cancel"], confirm=True)
    session.run()
    assert scripted.confirmations and "Stop task task-1?" in scripted.confirmations[0]
    assert fake.cancels == ["task-1"]
    assert "Stopped task task-1." in output(buffer)


def test_cancel_can_be_called_off(clean_env):
    fake = FakeAgent()
    fake.script("task-1", _task("task-1", "TASK_STATE_WORKING"))
    session, _, buffer = session_for(fake, ["/cancel"], confirm=False)
    session.run()
    assert fake.cancels == []
    assert "Left it running." in output(buffer)


def test_cancel_on_a_waiting_proposal_declines_it(clean_env):
    fake = FakeAgent()
    fake.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    session, scripted, buffer = session_for(fake, ["/cancel"])
    session.run()
    assert fake.cancels == ["task-1"]
    assert scripted.confirmations == []
    assert "Declined. The branch ari/fix-it stays on the repository" in output(buffer)


def test_unique_prefixes_run_the_command_and_ambiguous_ones_are_explained(clean_env):
    fake = FakeAgent()
    session, _, buffer = session_for(fake, ["/st", "/s", "/xyz", "/he"], running=False)
    session.run()
    text = output(buffer)
    assert "No task in this conversation" in text or "no task in this conversation yet" in text
    assert "/s could mean /setup, /status. Type more of the command." in text
    assert "Unknown command /xyz. Type /help to see the commands." in text
    assert "/wait" in text and "/cancel" in text


def test_shell_commands_typed_into_the_chat_are_explained_not_sent(clean_env):
    fake = FakeAgent()
    fake.script("task-1", _task("task-1", "TASK_STATE_WORKING"))
    session, _, buffer = session_for(fake, ["ari tasks follow task-1", "ARI tasks cancel task-1"])
    session.run()
    text = output(buffer)
    assert text.count("That is a command for your shell, not a message for the agent.") == 2
    assert fake.sends == [] and fake.cancels == []
