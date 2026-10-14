import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable

import httpx

from common.http_retry import RETRYABLE_STATUS, request_with_retry

from .tasks import NON_TERMINAL_STATES, context_id_of, task_state


class AgentError(RuntimeError):
    def __init__(self, what: str, status_code: int, body: str):
        super().__init__(f"{what} returned HTTP {status_code}: {body[:500]}")
        self.what = what
        self.status_code = status_code
        self.body = body


def build_message(
    prompt: str, config: dict[str, Any], *, context_id: str = "", task_id: str = ""
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": prompt}, {"data": config}],
    }
    if context_id:
        message["context_id"] = context_id
    if task_id:
        message["task_id"] = task_id
    return message


class AgentClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        http: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._http = http

    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "A2A-Version": "1.0", "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        what: str,
        *,
        json: Any = None,
        timeout: float | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        kwargs: dict[str, Any] = {"headers": self.headers(), "timeout": timeout or self.timeout}
        if json is not None:
            kwargs["json"] = json
        if self._http is not None:
            response = await request_with_retry(self._http, method, url, what=what, max_attempts=max_attempts, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await request_with_retry(client, method, url, what=what, max_attempts=max_attempts, **kwargs)
        if response.status_code >= 400:
            raise AgentError(what, response.status_code, response.text)
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            raise AgentError(what, response.status_code, response.text) from None
        return body if isinstance(body, dict) else {"value": body}

    async def agent_card(self) -> dict[str, Any]:
        return await self._request("GET", "/.well-known/agent-card.json", "agent card")

    async def send(
        self,
        prompt: str,
        config: dict[str, Any],
        *,
        context_id: str = "",
        task_id: str = "",
        return_immediately: bool = True,
        webhook_url: str = "",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"message": build_message(prompt, config, context_id=context_id, task_id=task_id)}
        configuration: dict[str, Any] = {}
        if return_immediately:
            configuration["return_immediately"] = True
        if webhook_url:
            configuration["task_push_notification_config"] = {"url": webhook_url}
        if configuration:
            body["configuration"] = configuration
        result = await self._request("POST", "/message:send", "message/send", json=body, timeout=timeout, max_attempts=1)
        task = result.get("task")
        return task if isinstance(task, dict) else result

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/tasks/{task_id}", "tasks/get")

    async def cancel(self, task_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}:cancel", "tasks/cancel")

    async def confirm(
        self,
        task: dict[str, Any],
        pending_action_id: str,
        config: dict[str, Any],
        *,
        text: str = "approved",
    ) -> dict[str, Any]:
        confirm_config = {**config, "confirm_pending_action": {"id": pending_action_id}}
        return await self.send(text, confirm_config, context_id=context_id_of(task), task_id=task["id"])

    async def _poll(self, task_id: str) -> tuple[dict[str, Any] | None, Exception | None]:
        try:
            return await self.get_task(task_id), None
        except AgentError as exc:
            if exc.status_code not in RETRYABLE_STATUS:
                raise
            return None, exc
        except RuntimeError as exc:
            return None, exc

    async def wait(
        self,
        task_id: str,
        *,
        poll_seconds: float = 5.0,
        budget_seconds: float = 3600.0,
        outage_seconds: float = 600.0,
        on_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_outage: Callable[[Exception], Awaitable[None]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        started = clock()
        last_signature: tuple[str, int] | None = None
        outage_started: float | None = None
        while True:
            task, error = await self._poll(task_id)
            if task is None:
                now = clock()
                if outage_started is None:
                    outage_started = now
                    if on_outage is not None:
                        await on_outage(error)
                down = now - outage_started
                if down > outage_seconds or now - started > budget_seconds:
                    raise TimeoutError(
                        f"agent unreachable for {down:.0f}s while following task {task_id} ({error}); "
                        "the run continues on the agent"
                    )
                await sleep(poll_seconds)
                continue
            outage_started = None
            signature = (task_state(task), len(task.get("artifacts") or []))
            if on_update is not None and signature != last_signature:
                await on_update(task)
                last_signature = signature
            if task_state(task) not in NON_TERMINAL_STATES:
                return task
            elapsed = clock() - started
            if elapsed > budget_seconds:
                raise TimeoutError(
                    f"task {task_id} still {task_state(task)} after {elapsed:.0f}s; the run continues on the agent"
                )
            await sleep(poll_seconds)
