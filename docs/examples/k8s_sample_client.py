"""ARI Kubernetes Agent — sample A2A client.

    python k8s_sample_client.py "check the health of the pods in namespace shop"
    python k8s_sample_client.py "scale checkout to 3 replicas"   # stops at the proposal

A change always pauses in TASK_STATE_INPUT_REQUIRED with a `pending_action`
artifact; it is applied only by a second `message/send` on the same task
that carries `confirm_pending_action: {id}` (see docs/DEVELOPER_GUIDE.md
§10). Like the other kinds' samples, this script deliberately has no mode
that approves a change — approval belongs to a person. Declining is
`POST /tasks/{id}:cancel`.

Secrets come from the environment, never from this file:
    ARI_BASE_URL   default http://127.0.0.1:18002
    ARI_API_KEY    one of the agent's AGENT_API_KEYS
    LLM_PROVIDER   openai | anthropic (default openai)
    LLM_API_KEY    the request's LLM key
    LLM_MODEL      model name
"""

import argparse
import json
import os
import time
import uuid

import httpx

BASE = os.environ.get("ARI_BASE_URL", "http://127.0.0.1:18002").rstrip("/")
HEADERS = {"x-api-key": os.environ.get("ARI_API_KEY", ""), "A2A-Version": "1.0"}


def config(**extra) -> dict:
    return {
        "llm": {
            "provider": os.environ.get("LLM_PROVIDER", "openai"),
            "api_key": os.environ["LLM_API_KEY"],
            "model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        },
        "options": {},
        **extra,
    }


def send(prompt: str, data: dict, task: dict | None = None) -> dict:
    message = {"message_id": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": prompt}, {"data": data}]}
    if task:
        message["task_id"] = task["id"]
        message["context_id"] = task.get("contextId") or task.get("context_id")
    body = {"message": message, "configuration": {"return_immediately": True}}
    resp = httpx.post(f"{BASE}/message:send", headers=HEADERS, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()["task"]


def wait(task_id: str) -> dict:
    while True:
        task = httpx.get(f"{BASE}/tasks/{task_id}", headers=HEADERS, timeout=30).json()
        if task["status"]["state"] not in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"):
            return task
        time.sleep(5)


def artifacts(task: dict) -> dict:
    out = {}
    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if "data" in part:
                out[artifact.get("name")] = part["data"]
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    args = parser.parse_args()

    task = wait(send(args.prompt, config())["id"])
    print(task["status"]["state"])
    print(json.dumps(artifacts(task), indent=2)[:4000])


if __name__ == "__main__":
    main()
