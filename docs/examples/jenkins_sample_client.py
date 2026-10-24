"""ARI Jenkins Agent — sample A2A client.

Sibling to `sample_client.py` (the on-call agent's own example) — same
protocol, same three response patterns (see docs/DEVELOPER_GUIDE.md §3),
different config `DataPart` shape (§9). Demonstrates:

    python jenkins_sample_client.py agent-card   # fetch this deployment's Agent Card
    python jenkins_sample_client.py chat         # blocking — a status question
    python jenkins_sample_client.py task-poll    # non-blocking — diagnose a failure, polls tasks/get
    python jenkins_sample_client.py propose      # blocking — request a change, print the pending_action artifact

Deliberately does NOT include a mode that confirms and executes a
proposed action — see docs/DEVELOPER_GUIDE.md §9 for how confirmation
actually works (a second message/send in the same context_id, carrying
the prior pending_action's exact fields via conversation history) and why
this example doesn't automate it: this codebase's general posture is to
never wire an automatic confirm-and-mutate path in example/test code.

Secrets are read from environment variables, never hardcoded:
    JENKINS_BASE_URL   e.g. https://jenkins.example.com
    JENKINS_USER       Jenkins username the token belongs to
    JENKINS_TOKEN      Jenkins API token
    ANTHROPIC_API_KEY  real key for config.llm (the default provider)
    ARI_BASE_URL       default http://localhost:8000
    ARI_API_KEY        default dev-local-e2e-key
    JOB_NAME           full job name (with folder path) to ask about, e.g. "deploy/checkout-service"

Optional:
    LLM_PROVIDER       default "anthropic"; "openai" also supported
    LLM_API_KEY        overrides ANTHROPIC_API_KEY/OPENAI_API_KEY
    LLM_MODEL          overrides the provider's default model
    JENKINS_AUTH_TYPE  default "api_token"; "bearer"/"basic" also supported
    JENKINS_VERIFY_TLS default "true"
    ARI_CHAT_TIMEOUT   seconds to wait in chat mode, default 600
"""

import argparse
import json
import os
import time
import uuid

import httpx


def _headers() -> dict:
    return {
        "x-api-key": os.environ.get("ARI_API_KEY", "dev-local-e2e-key"),
        "A2A-Version": "1.0",
        "Content-Type": "application/json",
    }


_DEFAULT_MODELS = {"anthropic": "claude-sonnet-5", "openai": "gpt-4o-mini"}


def _llm() -> dict:
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    key = os.environ.get("LLM_API_KEY") or os.environ.get(f"{provider.upper()}_API_KEY")
    if not key:
        raise SystemExit(
            f"no API key for provider {provider!r}: set LLM_API_KEY or {provider.upper()}_API_KEY"
        )
    model = os.environ.get("LLM_MODEL") or _DEFAULT_MODELS.get(provider)
    if not model:
        raise SystemExit(f"no default model known for provider {provider!r}: set LLM_MODEL")
    return {"provider": provider, "api_key": key, "model": model}


def _jenkins() -> dict:
    base_url = os.environ.get("JENKINS_BASE_URL")
    token = os.environ.get("JENKINS_TOKEN")
    if not base_url or not token:
        raise SystemExit("set JENKINS_BASE_URL and JENKINS_TOKEN")
    return {
        "base_url": base_url,
        "auth": {
            "type": os.environ.get("JENKINS_AUTH_TYPE", "api_token"),
            "user": os.environ.get("JENKINS_USER"),
            "token": token,
        },
        "verify_tls": os.environ.get("JENKINS_VERIFY_TLS", "true").lower() != "false",
    }


def _config() -> dict:
    return {"jenkins": _jenkins(), "llm": _llm()}


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


def _job_name() -> str:
    return os.environ.get("JOB_NAME", "deploy/checkout-service")


def chat(base_url: str) -> None:
    """Blocking call -- a plain status question, no confirmation needed."""
    prompt = f"how's the {_job_name()} job doing right now?"
    resp = httpx.post(
        f"{base_url}/message:send",
        headers=_headers(),
        json={"message": _message(prompt, _config())},
        timeout=float(os.environ.get("ARI_CHAT_TIMEOUT", "600")),
    )
    resp.raise_for_status()
    task = resp.json()["task"]
    print(f"state: {task['status']['state']}")
    print(task["status"]["message"]["parts"][0]["text"])


def task_poll(base_url: str) -> None:
    """Non-blocking call -- diagnose a failure, caller polls for status."""
    prompt = f"why has {_job_name()} been failing? diagnose the last failed build."
    resp = httpx.post(
        f"{base_url}/message:send",
        headers=_headers(),
        json={
            "message": _message(prompt, _config()),
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


def propose(base_url: str) -> None:
    """Blocking call -- request a change, print the pending_action artifact.
    Deliberately stops here; see this module's docstring for why there is
    no mode that goes on to confirm and execute it."""
    prompt = f"deploy {_job_name()} to staging"
    resp = httpx.post(
        f"{base_url}/message:send",
        headers=_headers(),
        json={"message": _message(prompt, _config())},
        timeout=float(os.environ.get("ARI_CHAT_TIMEOUT", "600")),
    )
    resp.raise_for_status()
    task = resp.json()["task"]
    print(f"state: {task['status']['state']}")
    print(task["status"]["message"]["parts"][0]["text"])
    for artifact in task.get("artifacts", []):
        if artifact["name"] == "pending_action":
            print("\npending_action artifact:")
            print(json.dumps(artifact["parts"][0]["data"], indent=2))
            print(
                "\nTo actually run this, send a follow-up message/send in the same "
                "context_id confirming it -- see docs/DEVELOPER_GUIDE.md §9. This "
                "script deliberately does not do that for you."
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["agent-card", "chat", "task-poll", "propose"])
    args = parser.parse_args()

    base_url = os.environ.get("ARI_BASE_URL", "http://localhost:8000")

    if args.mode == "agent-card":
        print(json.dumps(fetch_agent_card(base_url), indent=2))
    elif args.mode == "chat":
        chat(base_url)
    elif args.mode == "task-poll":
        task_poll(base_url)
    elif args.mode == "propose":
        propose(base_url)


if __name__ == "__main__":
    main()
