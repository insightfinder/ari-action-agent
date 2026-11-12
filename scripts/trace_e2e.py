"""Tracing end to end: one traced task against a deployed agent, checked in Jaeger.

Plays mcp-service's part, since a real caller is what makes the trace reach
InsightFinder: the trace server forwards only a trace whose root has no
parent, so a request carrying a made-up traceparent is stored in Jaeger and
never sent on. The script therefore records the two spans mcp-service would
(`chat-prompt-response` and its child `agent:<name>`) on the trace server
itself, sends one task whose `trace` block carries the child's traceparent
(docs/DEVELOPER_GUIDE.md §5), and ends both spans when the task finishes.

With JAEGER_URL set it then reads the whole trace back and fails unless:
the agent's spans hang under `agent:<name>`, every span carries the trace
identity, no span sets `chat.id`, only the caller's root carries
`chat.prompt`/`chat.response`, and no span value looks like a credential.

Not part of `tests/`: it costs real LLM tokens and needs a deployed agent
with TRACE_SERVER_URL set. Run by hand against the test cluster only.

    kubectl -n insightfinder port-forward svc/ari-oncall-api 18001:80 &
    kubectl -n traceserver port-forward svc/traceserver-llm-labs 14618:4618 &
    kubectl -n monitoring port-forward svc/jaeger-query 16686:16686 &
    export ARI_BASE_URL=http://127.0.0.1:18001
    export ARI_API_KEY=...                # one of the agent's AGENT_API_KEYS
    export LLM_PROVIDER=anthropic LLM_API_KEY=... LLM_MODEL=...
    export OTLP_ENDPOINT=127.0.0.1:14618  # the trace server, OTLP/gRPC
    export TRACE_PROJECT=... TRACE_USER=... TRACE_LICENSE_KEY=...
    export JAEGER_URL=http://127.0.0.1:16686   # optional: check the trace

    python scripts/trace_e2e.py --kind oncall
    python scripts/trace_e2e.py --kind oncall --lane interactive
    python scripts/trace_e2e.py --kind k8s --namespace traceserver
    python scripts/trace_e2e.py --kind k8s "Is anything unhealthy in namespace web?"

TRACE_USER and TRACE_LICENSE_KEY must be the org's trace admin, the user
handshake gives mcp-service for that org and model (the first admin who
saved the MCP model setting), or the trace lands in a system of that user's
own that AI WatchTower does not list. TRACE_END_USER (default TRACE_USER)
is the person shown as asking. The oncall kind clones REPO_URL (default
octocat/Hello-World) and never pushes: `skip_pr` is set.

Any of these can instead go in `scripts/.env` (git-ignored, `KEY=value`
lines); a variable already set in the environment wins over the file.
Prints ids, span names and attribute keys, never a key or license value.
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import httpx
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

POLL_SECONDS = 5
TASK_TIMEOUT_S = 1500
WAITING = ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")
# The caller's service name on test, so the trace reads like a real one.
CALLER_SERVICE = "MCP-Agent-ARI"
AGENT_NAMES = {"oncall": "ARI On-Call Agent", "k8s": "ARI Kubernetes Agent"}
DEFAULT_PROMPTS = {
    "oncall": "Add a short CONTRIBUTING.md explaining how to open an issue and a pull request.",
    "k8s": "Check the health of the deployments in namespace {namespace}.",
}
IDENTITY = ("x-username", "x-trace-project", "x-licensekey")
# Shapes of credentials that must never reach a span (provider keys, GitHub
# tokens, bearer tokens, URL userinfo).
SECRET_LIKE = re.compile(
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|(?i:\bbearer\s+)[A-Za-z0-9._~+/=\-]{16,}|\b[a-z][a-z0-9+.\-]*://[^/\s@\[]+:[^/\s@]+@"
)

_DEFAULT_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5"}


def _load_env_file(path: Path) -> None:
    """`KEY=value` lines from `path` into os.environ, never overriding a
    variable that is already set. Values are never printed."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
            value = "" if value.startswith("#") else value
        if key and value and key not in os.environ:
            os.environ[key] = value


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"set {name}")
    return value


def _llm() -> dict:
    provider = os.environ.get("LLM_PROVIDER", "openai")
    key = os.environ.get("LLM_API_KEY") or os.environ.get(f"{provider.upper()}_API_KEY")
    if not key:
        raise SystemExit(f"no API key for provider {provider!r}: set LLM_API_KEY")
    return {"provider": provider, "api_key": key, "model": os.environ.get("LLM_MODEL") or _DEFAULT_MODELS.get(provider)}


def _config(kind: str, args, trace_block: dict) -> dict:
    if kind == "oncall":
        options = {"skip_pr": True, "skip_tests": True}
    else:
        options = {"namespace": args.namespace}
    if args.lane:
        options["lane"] = args.lane
    config = {"llm": _llm(), "options": options, "trace": trace_block}
    if kind == "oncall":
        config["repo"] = {"provider": "github", "url": os.environ.get("REPO_URL", "https://github.com/octocat/Hello-World")}
    return config


def _run_task(prompt: str, config: dict) -> tuple[str, str, str]:
    """Sends the task and waits for it; returns (task id, final state, final text)."""
    base = _require("ARI_BASE_URL").rstrip("/")
    headers = {"x-api-key": _require("ARI_API_KEY"), "A2A-Version": "1.0", "Content-Type": "application/json"}
    message = {"message_id": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": prompt}, {"data": config}]}
    with httpx.Client(timeout=60, trust_env=False) as client:
        resp = client.post(f"{base}/message:send", headers=headers,
                           json={"message": message, "configuration": {"return_immediately": True}})
        resp.raise_for_status()
        task_id = resp.json()["task"]["id"]
        print(f"task {task_id}", flush=True)
        started = time.monotonic()
        while True:
            task = client.get(f"{base}/tasks/{task_id}", headers=headers).json()
            state = task["status"]["state"]
            if state not in WAITING or time.monotonic() - started > TASK_TIMEOUT_S:
                break
            time.sleep(POLL_SECONDS)
    parts = (task["status"].get("message") or {}).get("parts", [])
    text = " ".join(p.get("text", "") for p in parts)
    print(f"state {state} after {time.monotonic() - started:.0f}s", flush=True)
    return task_id, state, text


def _jaeger_trace(jaeger: str, trace_id: str, wait_s: int = 120) -> dict:
    """The trace once the agent's spans have reached Jaeger (they are batched)."""
    deadline = time.monotonic() + wait_s
    with httpx.Client(timeout=30, trust_env=False) as client:
        while True:
            resp = client.get(f"{jaeger.rstrip('/')}/api/traces/{trace_id}")
            data = resp.json().get("data") if resp.status_code == 200 else None
            if data and any(s["operationName"] == "a2a-task-result" for s in data[0]["spans"]):
                return data[0]
            if time.monotonic() > deadline:
                raise SystemExit(f"trace {trace_id}: the agent's spans did not reach Jaeger within {wait_s}s")
            time.sleep(POLL_SECONDS)


def _check(trace_data: dict, agent_span_id: str, root_span_id: str) -> list[str]:
    spans = trace_data["spans"]
    processes = trace_data["processes"]
    by_id = {s["spanID"]: s for s in spans}
    problems = []

    def parent(span):
        refs = [r for r in span.get("references", []) if r["refType"] == "CHILD_OF"]
        return refs[0]["spanID"] if refs else None

    def under_agent(span):
        seen = set()
        while span is not None and span["spanID"] not in seen:
            seen.add(span["spanID"])
            if parent(span) == agent_span_id:
                return True
            span = by_id.get(parent(span))
        return False

    agent_spans = [s for s in spans if processes[s["processID"]]["serviceName"] != CALLER_SERVICE]
    if not agent_spans:
        problems.append("no span from the agent")
    for span in spans:
        name = span["operationName"]
        tags = {t["key"]: t["value"] for t in span["tags"]}
        if not all(tags.get(key) for key in IDENTITY):
            problems.append(f"{name}: missing trace identity")
        if "chat.id" in tags:
            problems.append(f"{name}: sets chat.id")
        if span["spanID"] != root_span_id and {"chat.prompt", "chat.response"} & set(tags):
            problems.append(f"{name}: carries chat.prompt/chat.response")
        for key, value in tags.items():
            if key != "x-licensekey" and isinstance(value, str) and SECRET_LIKE.search(value):
                problems.append(f"{name}: {key} looks like a credential")
    for span in agent_spans:
        if not under_agent(span):
            problems.append(f"{span['operationName']}: not under the caller's agent span")
    return problems


def _print_tree(trace_data: dict) -> None:
    spans = trace_data["spans"]
    children: dict = {}
    for span in spans:
        refs = [r for r in span.get("references", []) if r["refType"] == "CHILD_OF"]
        children.setdefault(refs[0]["spanID"] if refs else None, []).append(span)

    def walk(parent_id, depth):
        for span in sorted(children.get(parent_id, []), key=lambda s: s["startTime"]):
            print(f"{'  ' * depth}{span['operationName']}  {span['duration'] // 1000}ms")
            walk(span["spanID"], depth + 1)

    ids = {s["spanID"] for s in spans}
    for parent_id in [p for p in children if p not in ids]:
        walk(parent_id, 1)


def main() -> int:
    _load_env_file(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("prompt", nargs="?", help="the request (a default per kind otherwise)")
    parser.add_argument("--kind", choices=sorted(AGENT_NAMES), default="oncall")
    parser.add_argument("--namespace", default="default", help="k8s only: the namespace to look at")
    parser.add_argument("--lane", choices=["interactive"], help="send on the interactive lane")
    args = parser.parse_args()

    user, project, license_key = _require("TRACE_USER"), _require("TRACE_PROJECT"), _require("TRACE_LICENSE_KEY")
    end_user = os.environ.get("TRACE_END_USER") or user
    session = f"trace-e2e-{uuid.uuid4().hex[:8]}"
    agent_name = AGENT_NAMES[args.kind]
    prompt = args.prompt or DEFAULT_PROMPTS[args.kind].format(namespace=args.namespace)

    provider = TracerProvider(resource=Resource.create({"service.name": CALLER_SERVICE}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=_require("OTLP_ENDPOINT"), insecure=True)))
    tracer = provider.get_tracer("trace-e2e")
    identity = {"x-username": user, "x-trace-project": project, "x-licensekey": license_key,
                "x-session-id": session, "chat.username": end_user}

    root = tracer.start_span("chat-prompt-response", attributes={**identity, "chat.prompt": prompt})
    agent = tracer.start_span(f"agent:{agent_name}", context=trace.set_span_in_context(root),
                              attributes={**identity, "agent.name": agent_name})
    agent_ctx = agent.get_span_context()
    trace_id = f"{agent_ctx.trace_id:032x}"
    print(f"trace {trace_id}  session {session}", flush=True)
    trace_block = {"traceparent": f"00-{trace_id}-{agent_ctx.span_id:016x}-01", "trace_project": project,
                   "trace_admin_user": user, "trace_admin_user_license": license_key,
                   "session_id": session, "username": end_user}
    state, text = "error", ""
    try:
        _, state, text = _run_task(prompt, _config(args.kind, args, trace_block))
    finally:
        # Ended either way, as mcp-service does, so the trace still has its root.
        agent.set_attribute("agent.state", state)
        agent.end()
        root.set_attribute("chat.response", text[:4000])
        root.end()
        provider.shutdown()

    jaeger = os.environ.get("JAEGER_URL")
    if not jaeger:
        print("JAEGER_URL unset: trace not checked")
        return 0
    trace_data = _jaeger_trace(jaeger, trace_id)
    _print_tree(trace_data)
    problems = _check(trace_data, f"{agent_ctx.span_id:016x}", f"{root.get_span_context().span_id:016x}")
    print(f"{len(trace_data['spans'])} spans; " + ("no problems" if not problems else f"{len(problems)} problem(s):"))
    for problem in problems:
        print(f"  - {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
