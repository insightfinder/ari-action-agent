import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chat_gateway.a2a_client import AgentClient, AgentError, build_message


def _client(handler) -> AgentClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
    return AgentClient("http://agent.test/", "secret-key", http=http)


def _task(task_id="t1", state="TASK_STATE_SUBMITTED", **extra) -> dict:
    return {"id": task_id, "contextId": "c1", "status": {"state": state}, **extra}


def test_build_message_carries_prompt_config_and_continuation_ids():
    message = build_message("fix it", {"repo": {"url": "u"}}, context_id="c1", task_id="t1")
    uuid.UUID(message["message_id"])
    assert message["role"] == "ROLE_USER"
    assert message["parts"] == [{"text": "fix it"}, {"data": {"repo": {"url": "u"}}}]
    assert message["context_id"] == "c1"
    assert message["task_id"] == "t1"


def test_build_message_omits_ids_when_not_continuing():
    message = build_message("hi", {})
    assert "context_id" not in message
    assert "task_id" not in message


@pytest.mark.asyncio
async def test_send_posts_a_non_blocking_message_with_auth_headers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"task": _task()})

    task = await _client(handler).send("fix it", {"llm": {"provider": "openai"}})

    assert task["id"] == "t1"
    assert captured["url"] == "http://agent.test/message:send"
    assert captured["headers"]["x-api-key"] == "secret-key"
    assert captured["headers"]["a2a-version"] == "1.0"
    assert captured["body"]["configuration"] == {"return_immediately": True}
    assert captured["body"]["message"]["parts"][1] == {"data": {"llm": {"provider": "openai"}}}


@pytest.mark.asyncio
async def test_send_blocking_sends_no_configuration():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"task": _task(state="TASK_STATE_COMPLETED")})

    await _client(handler).send("hi", {}, return_immediately=False)
    assert "configuration" not in captured["body"]


@pytest.mark.asyncio
async def test_send_registers_a_webhook_when_given():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"task": _task()})

    await _client(handler).send("hi", {}, webhook_url="https://gw.test/hook/abc")
    assert captured["body"]["configuration"]["task_push_notification_config"] == {"url": "https://gw.test/hook/abc"}


@pytest.mark.asyncio
async def test_send_is_never_retried_because_it_creates_a_task(monkeypatch):
    monkeypatch.setattr("common.http_retry.backoff_seconds", lambda attempt: 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="try later")

    with pytest.raises(AgentError) as info:
        await _client(handler).send("hi", {})
    assert info.value.status_code == 503
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_get_task_and_cancel_hit_the_rest_paths():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=_task(state="TASK_STATE_CANCELED"))

    client = _client(handler)
    assert (await client.get_task("t1"))["id"] == "t1"
    assert (await client.cancel("t1"))["status"]["state"] == "TASK_STATE_CANCELED"
    assert seen == [("GET", "/tasks/t1"), ("POST", "/tasks/t1:cancel")]


@pytest.mark.asyncio
async def test_confirm_resumes_the_same_task_with_the_structured_field():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"task": _task()})

    paused = _task(state="TASK_STATE_INPUT_REQUIRED")
    await _client(handler).confirm(paused, "act-1", {"repo": {"url": "u"}, "llm": {"provider": "x"}}, text="ok")

    message = captured["body"]["message"]
    assert message["task_id"] == "t1"
    assert message["context_id"] == "c1"
    assert message["parts"][0] == {"text": "ok"}
    assert message["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}
    assert message["parts"][1]["data"]["repo"] == {"url": "u"}
    assert message["parts"][1]["data"]["llm"] == {"provider": "x"}


@pytest.mark.asyncio
async def test_a_4xx_raises_an_agent_error_carrying_the_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Task t9 not found")

    with pytest.raises(AgentError) as info:
        await _client(handler).get_task("t9")
    assert info.value.status_code == 404
    assert "Task t9 not found" in str(info.value)
    assert info.value.what == "tasks/get"


@pytest.mark.asyncio
async def test_a_non_json_body_is_an_agent_error_not_a_traceback():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    with pytest.raises(AgentError) as info:
        await _client(handler).get_task("t1")
    assert "<html>" in str(info.value)


@pytest.mark.asyncio
async def test_a_503_on_a_read_is_retried_before_giving_up(monkeypatch):
    monkeypatch.setattr("common.http_retry.backoff_seconds", lambda attempt: 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="try later")
        return httpx.Response(200, json=_task())

    task = await _client(handler).get_task("t1")
    assert task["id"] == "t1"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_an_empty_body_is_an_empty_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    assert await _client(handler).cancel("t1") == {}


@pytest.mark.asyncio
async def test_wait_polls_until_the_task_pauses_and_reports_each_change():
    states = iter(["TASK_STATE_WORKING", "TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED"])
    slept = []
    updates = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_task(state=next(states)))

    async def on_update(task):
        updates.append(task["status"]["state"])

    async def sleep(seconds):
        slept.append(seconds)

    task = await _client(handler).wait("t1", poll_seconds=0.5, on_update=on_update, sleep=sleep)

    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert updates == ["TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED"]
    assert slept == [0.5, 0.5]


@pytest.mark.asyncio
async def test_wait_reports_again_when_an_artifact_appears_mid_run():
    responses = iter(
        [
            _task(state="TASK_STATE_WORKING"),
            _task(state="TASK_STATE_WORKING", artifacts=[{"name": "root_cause", "parts": [{"data": {"summary": "x"}}]}]),
            _task(state="TASK_STATE_COMPLETED", artifacts=[{"name": "root_cause", "parts": [{"data": {"summary": "x"}}]}]),
        ]
    )
    updates = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async def on_update(task):
        updates.append((task["status"]["state"], len(task.get("artifacts") or [])))

    async def sleep(seconds):
        pass

    await _client(handler).wait("t1", on_update=on_update, sleep=sleep)
    assert updates == [("TASK_STATE_WORKING", 0), ("TASK_STATE_WORKING", 1), ("TASK_STATE_COMPLETED", 1)]


@pytest.mark.asyncio
async def test_wait_rides_out_an_agent_restart_and_reports_it_once(monkeypatch):
    monkeypatch.setattr("common.http_retry.backoff_seconds", lambda attempt: 0)
    script = [200, 503, 503, 503, 503, 503, 503, 503, 200]
    states = iter(["TASK_STATE_WORKING", "TASK_STATE_COMPLETED"])
    outages = []
    updates = []

    def handler(request: httpx.Request) -> httpx.Response:
        if script.pop(0) == 503:
            return httpx.Response(503, text="no available server")
        return httpx.Response(200, json=_task(state=next(states)))

    async def on_outage(error):
        outages.append(error)

    async def on_update(task):
        updates.append(task["status"]["state"])

    async def sleep(seconds):
        pass

    task = await _client(handler).wait("t1", on_update=on_update, on_outage=on_outage, sleep=sleep)

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert updates == ["TASK_STATE_WORKING", "TASK_STATE_COMPLETED"]
    assert len(outages) == 1
    assert isinstance(outages[0], AgentError) and outages[0].status_code == 503
    assert script == []


@pytest.mark.asyncio
async def test_wait_rides_out_connection_failures(monkeypatch):
    monkeypatch.setattr("common.http_retry.backoff_seconds", lambda attempt: 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) <= 3:
            raise httpx.ConnectError("All connection attempts failed", request=request)
        return httpx.Response(200, json=_task(state="TASK_STATE_COMPLETED"))

    async def sleep(seconds):
        pass

    task = await _client(handler).wait("t1", sleep=sleep)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_wait_gives_up_when_the_agent_stays_unreachable(monkeypatch):
    monkeypatch.setattr("common.http_retry.backoff_seconds", lambda attempt: 0)
    ticks = iter([0.0, 0.0, 100.0, 200.0, 301.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="no available server")

    async def sleep(seconds):
        pass

    with pytest.raises(TimeoutError) as info:
        await _client(handler).wait("t1", outage_seconds=300, sleep=sleep, clock=lambda: next(ticks))
    assert "agent unreachable for 301s" in str(info.value)
    assert "continues on the agent" in str(info.value)


@pytest.mark.asyncio
async def test_wait_does_not_hide_a_task_the_agent_no_longer_knows():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Task t1 not found")

    async def sleep(seconds):
        pass

    with pytest.raises(AgentError) as info:
        await _client(handler).wait("t1", sleep=sleep)
    assert info.value.status_code == 404


@pytest.mark.asyncio
async def test_wait_gives_up_after_its_budget_without_cancelling():
    ticks = iter([0.0, 10.0, 20.0, 31.0])
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=_task(state="TASK_STATE_WORKING"))

    async def sleep(seconds):
        pass

    with pytest.raises(TimeoutError) as info:
        await _client(handler).wait("t1", budget_seconds=30, sleep=sleep, clock=lambda: next(ticks))
    assert "continues on the agent" in str(info.value)
    assert seen and all(method == "GET" and path == "/tasks/t1" for method, path in seen)
