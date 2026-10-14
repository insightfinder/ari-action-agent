import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.ari_cli_fakes import FakeAgent


@pytest.fixture
def agent(monkeypatch, tmp_path):
    cli = pytest.importorskip("ari_cli.main")
    from chat_gateway.a2a_client import AgentClient
    from chat_gateway.config import GatewaySettings

    fake = FakeAgent()
    for name in GatewaySettings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(cli.CONFIG_FILE_ENV, str(tmp_path / "config.toml"))
    monkeypatch.setenv("ARI_HISTORY_FILE", str(tmp_path / "history"))
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_PAT", "ghp_test")
    monkeypatch.setenv("DEFAULT_REPO_URL", "https://github.com/org/repo")
    monkeypatch.setenv("POLL_SECONDS", "0")
    monkeypatch.setattr("common.http_retry.backoff_seconds", lambda attempt: 0)

    def make_client(settings):
        http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), timeout=5)
        return AgentClient(settings.ari_agent_url, settings.ari_api_key, http=http)

    monkeypatch.setattr(cli, "make_client", make_client)
    return fake


@pytest.fixture
def chat_agent(agent, monkeypatch):
    monkeypatch.setenv("ARI_AGENT_URL", "http://agent.test")
    return agent


@pytest.fixture
def first_run(agent, monkeypatch):
    for name in ("LLM_API_KEY", "GITHUB_PAT", "DEFAULT_REPO_URL", "ARI_AGENT_URL"):
        monkeypatch.delenv(name, raising=False)
    return agent
