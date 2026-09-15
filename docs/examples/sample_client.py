"""ARI On-Call Agent — sample A2A client.

Demonstrates the three ways to get a result from this agent, all through
the SAME operation (`message/send` — see docs/DEVELOPER_GUIDE.md §3):

    python sample_client.py chat           # blocking — waits inline
    python sample_client.py task-poll       # non-blocking — polls tasks/get
    python sample_client.py task-webhook    # non-blocking — registers a callback

Uses plain `httpx` — no A2A-specific client library required, since the
protocol is just JSON over HTTP. The official `a2a-sdk` also ships a real
client (`a2a.client.ClientFactory`) if you'd rather not hand-build request
bodies; the equivalent call there is roughly:

    from a2a.client import ClientFactory
    client = await ClientFactory.create_from_url(base_url)
    async for event in client.send_message(send_message_request):
        ...  # an async generator of Task/Message/status-update events

That client speaks protobuf messages directly (`a2a.types.SendMessageRequest`
etc.) rather than raw JSON dicts, and needs an `AuthInterceptor` +
`CredentialService` wired up for the `x-api-key` header — more moving parts
for a first example, so this script sticks to plain HTTP.

Secrets are read from environment variables, never hardcoded:
    OPENAI_API_KEY   real key for config.llm
    GITHUB_PAT       only needed for task-poll/task-webhook (opens a real PR)
    ARI_BASE_URL     default http://localhost:8000
    ARI_API_KEY      default dev-local-e2e-key
    REPO_URL         default https://github.com/octocat/Hello-World
"""

import argparse
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import httpx


def _headers() -> dict:
    return {
        "x-api-key": os.environ.get("ARI_API_KEY", "dev-local-e2e-key"),
        "A2A-Version": "1.0",
        "Content-Type": "application/json",
    }


def _config(with_pat: bool = False) -> dict:
    repo = {"url": os.environ.get("REPO_URL", "https://github.com/octocat/Hello-World")}
    if with_pat:
        repo["pat"] = os.environ["GITHUB_PAT"]
    return {
        "repo": repo,
        "llm": {"provider": "openai", "api_key": os.environ["OPENAI_API_KEY"], "model": "gpt-4o-mini"},
        "options": {"skip_tests": True},
    }


def _message(prompt: str, config: dict) -> dict:
    return {
        "message_id": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": prompt}, {"data": config}],
    }


def fetch_agent_card(base_url: str) -> dict:
    resp = httpx.get(f"{base_url}/.well-known/agent-card.json", timeout=10)
    resp.raise_for_status()
    return resp.json()


def chat(base_url: str) -> None:
    """Blocking call — waits inline for the result, like a synchronous ask."""
    prompt = "What are the most common causes of a NullPointerException in Java?"
    resp = httpx.post(
        f"{base_url}/message:send",
        headers=_headers(),
        json={"message": _message(prompt, _config())},
        timeout=60,
    )
    resp.raise_for_status()
    task = resp.json()["task"]
    print(f"state: {task['status']['state']}")
    print(task["status"]["message"]["parts"][0]["text"])


def task_poll(base_url: str) -> None:
    """Non-blocking call — returns immediately, caller polls for status."""
    prompt = "Analyze this repository and suggest one small improvement, then open a PR. Skip tests."
    resp = httpx.post(
        f"{base_url}/message:send",
        headers=_headers(),
        json={
            "message": _message(prompt, _config(with_pat=True)),
            "configuration": {"return_immediately": True},
        },
        timeout=30,
    )
    resp.raise_for_status()
    task_id = resp.json()["task"]["id"]
    print(f"submitted task {task_id}, polling...")

    while True:
        resp = httpx.get(f"{base_url}/tasks/{task_id}", headers=_headers(), timeout=10)
        resp.raise_for_status()
        state = resp.json()["status"]["state"]
        print(f"  {state}")
        if state not in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"):
            print(json.dumps(resp.json(), indent=2))
            return
        time.sleep(10)


def task_webhook(base_url: str) -> None:
    """Non-blocking call with a registered push-notification webhook — the
    agent's own SDK delivers status updates to it as the task progresses;
    this script never polls."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            delivery = json.loads(body)
            received.append(delivery)
            if "artifactUpdate" in delivery:
                print(f"  webhook delivery: artifact '{delivery['artifactUpdate']['artifact']['name']}'")
            else:
                state = delivery.get("task", delivery.get("statusUpdate", {})).get("status", {}).get("state")
                print(f"  webhook delivery: {state}")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("0.0.0.0", 9300), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    print("listening for webhook deliveries on :9300")

    prompt = "Analyze this repository and suggest one small improvement, then open a PR. Skip tests."
    resp = httpx.post(
        f"{base_url}/message:send",
        headers=_headers(),
        json={
            "message": _message(prompt, _config(with_pat=True)),
            "configuration": {
                "return_immediately": True,
                # localhost works when this script and the agent run on the
                # same host without a container in between. If the agent
                # itself runs in a container (e.g. this project's own
                # docker-compose.yaml), it needs a way to reach *this*
                # process from inside that container instead —
                # host.docker.internal (Docker Desktop) or this host's LAN
                # IP, not localhost.
                "task_push_notification_config": {"url": "http://localhost:9300/callback"},
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    task_id = resp.json()["task"]["id"]
    print("submitted:", task_id)

    def is_terminal_for_this_task(delivery: dict) -> bool:
        update = delivery.get("statusUpdate")
        return bool(update) and update.get("taskId") == task_id and update["status"]["state"] in (
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
        )

    deadline = time.time() + 600
    while time.time() < deadline:
        if any(is_terminal_for_this_task(d) for d in received):
            break
        time.sleep(2)

    server.shutdown()
    for delivery in received:
        print(json.dumps(delivery, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["chat", "task-poll", "task-webhook", "agent-card"])
    args = parser.parse_args()

    base_url = os.environ.get("ARI_BASE_URL", "http://localhost:8000")

    if args.mode == "agent-card":
        print(json.dumps(fetch_agent_card(base_url), indent=2))
    elif args.mode == "chat":
        chat(base_url)
    elif args.mode == "task-poll":
        task_poll(base_url)
    elif args.mode == "task-webhook":
        task_webhook(base_url)


if __name__ == "__main__":
    main()
