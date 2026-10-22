import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")
pytest.importorskip("typer")
pytest.importorskip("prompt_toolkit")
pytest.importorskip("questionary")

pytestmark = pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pseudo-terminal")

SRC = Path(__file__).resolve().parents[1] / "src"

PROPOSAL = {
    "id": "act-1",
    "action_name": "Open pull request",
    "title": "Add SECURITY.md",
    "repo": "org/repo",
    "branch": "ari/add-security-md",
    "base_ref": "dev",
    "changed_files": ["SECURITY.md"],
    "verification": {"status": "not run", "detail": ""},
    "compare_url": "https://github.com/org/repo/compare/dev...ari/add-security-md",
    "key_hunks": "diff --git a/SECURITY.md b/SECURITY.md\n+# Security Policy",
}


def _task(task_id, state, text="", artifacts=None):
    task = {"id": task_id, "contextId": "ctx", "status": {"state": state}}
    if text:
        task["status"]["message"] = {"role": "ROLE_AGENT", "parts": [{"text": text}]}
    if artifacts:
        task["artifacts"] = artifacts
    return task


class FakeAgent(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.tasks = {}
        self.confirmed = []
        self.counter = 0


class Handler(BaseHTTPRequestHandler):
    server: FakeAgent

    def log_message(self, *args):
        pass

    def reply(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/.well-known/agent-card.json":
            return self.reply({"name": "Fake Agent", "skills": [{"id": "code_analysis_fix"}]})
        task_id = self.path.rsplit("/", 1)[-1]
        if task_id in self.server.tasks:
            return self.reply(self.server.tasks[task_id])
        return self.reply({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path.endswith(":cancel"):
            task_id = self.path.split("/")[-1].removesuffix(":cancel")
            self.server.tasks[task_id] = _task(task_id, "TASK_STATE_CANCELED")
            return self.reply(self.server.tasks[task_id])
        message = body["message"]
        data = message["parts"][1]["data"]
        task_id = message.get("task_id")
        if task_id and "confirm_pending_action" in data:
            self.server.confirmed.append(data["confirm_pending_action"]["id"])
            pr = [{"name": "pull_request", "parts": [{"data": {"url": "https://github.com/org/repo/pull/24"}}]}]
            self.server.tasks[task_id] = _task(task_id, "TASK_STATE_COMPLETED", "Opened.", pr)
        else:
            self.server.counter += 1
            task_id = f"task-{self.server.counter}"
            if "change" in message["parts"][0]["text"]:
                proposal = [{"name": "pending_action", "parts": [{"data": PROPOSAL}]}]
                self.server.tasks[task_id] = _task(task_id, "TASK_STATE_INPUT_REQUIRED", "Ready to open a PR.", proposal)
            else:
                self.server.tasks[task_id] = _task(task_id, "TASK_STATE_COMPLETED", "pong")
        return self.reply({"task": _task(task_id, "TASK_STATE_SUBMITTED")})


@pytest.fixture
def fake_agent():
    server = FakeAgent()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def spawn(env_extra, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ARI_", "LLM_", "GITHUB_", "DEFAULT_REPO"))}
    env.update(
        {
            "PYTHONPATH": str(SRC),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ARI_CONFIG_FILE": str(tmp_path / "config.toml"),
            "POLL_SECONDS": "0.1",
            "ARI_HISTORY_FILE": str(tmp_path / "history"),
            "TERM": "xterm-256color",
        }
    )
    env.update(env_extra)
    return pexpect.spawn(sys.executable, ["-m", "ari_cli.main"], env=env, encoding="utf-8", timeout=20, dimensions=(40, 110))


def test_the_chat_in_a_real_terminal(fake_agent, tmp_path):
    url = f"http://127.0.0.1:{fake_agent.server_address[1]}"
    child = spawn(
        {
            "ARI_AGENT_URL": url,
            "ARI_API_KEY": "k",
            "LLM_API_KEY": "sk-test",
            "GITHUB_PAT": "ghp_test",
            "DEFAULT_REPO_URL": "https://github.com/org/repo",
        },
        tmp_path,
    )
    child.expect("Fake Agent")
    child.expect("Type a request")
    child.expect("you ›")
    child.send("hello\r")
    child.expect("ARI ›")
    child.expect("pong")
    child.expect("you ›")
    child.send("please change it\r")
    child.expect("Proposed change")
    child.expect("What do you want to do")
    child.send("\r")
    child.expect("Pull request opened")
    child.expect("you ›")
    child.send("/he")
    child.send("\t")
    child.send("\r")
    if child.expect(["/branch", pexpect.TIMEOUT], timeout=3) == 1:
        child.send("\r")
        child.expect("/branch")
    child.expect("you ›")
    child.sendcontrol("d")
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 0
    assert fake_agent.confirmed == ["act-1"]


def test_the_setup_wizard_in_a_real_terminal(fake_agent, tmp_path):
    url = f"http://127.0.0.1:{fake_agent.server_address[1]}"
    child = spawn({}, tmp_path)
    seen = []
    child.logfile_read = type("Log", (), {"write": lambda self, s: seen.append(s), "flush": lambda self: None})()
    child.expect("Agent URL")
    child.sendcontrol("u")
    child.send(url + "\r")
    child.expect("Agent API key")
    child.send("agent-key\r")
    child.expect("LLM provider")
    child.send("\r")
    child.expect("Model")
    child.send("\r")
    child.expect("LLM API key")
    child.send("sk-new\r")
    child.expect("GitHub token")
    child.send("ghp_new\r")
    child.expect("Default repository URL")
    child.send("https://github.com/org/repo\r")
    child.expect("Branch to work from")
    child.send("\r")
    child.expect("Connected to Fake Agent")
    child.expect("Saved to")
    child.expect("you ›")
    child.sendcontrol("d")
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 0
    saved = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert f'ari_agent_url = "{url}"' in saved
    assert 'llm_provider = "openai"' in saved
    assert 'llm_model = "gpt-4o-mini"' in saved
    assert 'llm_api_key = "sk-new"' in saved
    assert (tmp_path / "config.toml").stat().st_mode & 0o077 == 0
    assert tmp_path.stat().st_mode & 0o077 == 0 or (tmp_path / "config.toml").parent == tmp_path
    screen = "".join(seen)
    for secret in ("agent-key", "sk-new", "ghp_new"):
        assert secret not in screen
