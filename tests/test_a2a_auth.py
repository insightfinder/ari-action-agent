import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from common.app_factory import create_app  # noqa: E402
from common.settings import get_settings  # noqa: E402

HEADERS = {"A2A-Version": "1.0"}
SEND = {"message": {"role": "ROLE_USER", "messageId": "m1", "parts": [{"text": "hi"}]}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEYS", "good-key")
    get_settings.cache_clear()
    # No `with`: the lifespan's task reconciler would try to reach Temporal.
    yield TestClient(create_app(), raise_server_exceptions=False)
    get_settings.cache_clear()


@pytest.mark.parametrize("key", [None, "wrong-key"])
@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", "/", {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": SEND}),
        # REST routes sit behind the SDK's rest_error_handler, which turned the old in-route 401 into a 500.
        ("POST", "/message:send", SEND),
        ("POST", "/message:stream", SEND),
        ("GET", "/tasks/some-task", None),
        ("GET", "/tenant-a/tasks/some-task", None),
    ],
)
def test_every_a2a_operation_without_a_valid_key_is_a_401(client, method, path, body, key):
    headers = dict(HEADERS, **({"x-api-key": key} if key else {}))
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing x-api-key"}


def test_a_valid_key_reaches_the_sdk(client):
    response = client.get("/tasks/some-task", headers=dict(HEADERS, **{"x-api-key": "good-key"}))
    assert response.status_code == 404  # the SDK's TaskNotFound, not the key check


@pytest.mark.parametrize("path", ["/.well-known/agent-card.json", "/healthz", "/docs", "/openapi.json"])
def test_the_agent_card_healthz_and_docs_stay_open(client, path):
    # The agent card path also matches the SDK's `/{tenant}` REST mount by prefix.
    assert client.get(path).status_code == 200


def test_an_unknown_path_is_still_a_404(client):
    assert client.get("/no-such-route").status_code == 404
