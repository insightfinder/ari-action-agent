"""ARI Kubernetes Agent — live scenarios against a deployed agent.

Drives the deployed `k8s` agent kind over its real A2A surface, against
the fixtures in deploy/k8s-agent-e2e/fixtures.yaml (namespace
`ari-k8s-e2e`), and asserts on task state and artifacts — never on the
final message's wording, which is an LLM writing for a human.

Not part of `tests/`: every scenario costs real LLM tokens, and the
`--allow-writes` scenarios change the fixture Deployments. Run by hand
against the test cluster only, never in CI, and only after the agent and
fixtures from deploy/k8s-agent-e2e/ are in place.

    kubectl -n ari-k8s-agent port-forward svc/ari-k8s-api 18002:80 &
    export ARI_BASE_URL=http://127.0.0.1:18002
    export ARI_API_KEY=...            # one of the agent's AGENT_API_KEYS
    export LLM_PROVIDER=anthropic LLM_API_KEY=... LLM_MODEL=...

Any of these can instead go in `scripts/.env` (git-ignored, `KEY=value`
lines); a variable already set in the environment wins over the file.

    python scripts/k8s_agent_live_test.py --list
    python scripts/k8s_agent_live_test.py                 # read-only + proposals (cancelled)
    python scripts/k8s_agent_live_test.py --allow-writes  # also approves and applies changes

`resume_after_restart` also restarts the agent's API Deployment with kubectl
and port-forwards to it itself; it runs only when ARI_API_DEPLOYMENT and
ARI_API_SERVICE ("<namespace>/<name>") are set, and should run last, since
the restart ends any port-forward opened by hand.

Every scenario also checks that the e2e canary planted in the fixtures
(env, logs) never appears anywhere in the task it produced.
"""

import argparse
import contextlib
import json
import os
import subprocess
import time
import uuid

from pathlib import Path

import httpx

POLL_SECONDS = 5
WAITING = ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")
NAMESPACE = "ari-k8s-e2e"
CANARY = "CANARY-e2e-7f3a9c1e5b2d"  # must match deploy/k8s-agent-e2e/fixtures.yaml

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
            value = value.split(" #", 1)[0].strip()  # trailing comment
            value = "" if value.startswith("#") else value
        if key and value and key not in os.environ:
            os.environ[key] = value


def _base_url() -> str:
    return os.environ.get("ARI_BASE_URL", "http://127.0.0.1:18002").rstrip("/")


def _headers() -> dict:
    key = os.environ.get("ARI_API_KEY")
    if not key:
        raise SystemExit("set ARI_API_KEY to one of the agent's AGENT_API_KEYS")
    return {"x-api-key": key, "A2A-Version": "1.0", "Content-Type": "application/json"}


def _config(**options) -> dict:
    provider = os.environ.get("LLM_PROVIDER", "openai")
    key = os.environ.get("LLM_API_KEY") or os.environ.get(f"{provider.upper()}_API_KEY")
    if not key:
        raise SystemExit(f"no API key for provider {provider!r}: set LLM_API_KEY")
    model = os.environ.get("LLM_MODEL") or _DEFAULT_MODELS.get(provider)
    return {"llm": {"provider": provider, "api_key": key, "model": model}, "options": {"namespace": NAMESPACE, **options}}


def _send(prompt: str, config: dict, *, task_id: str = "", context_id: str = "") -> dict:
    message = {"message_id": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": prompt}, {"data": config}]}
    if task_id:
        message["task_id"] = task_id
    if context_id:
        message["context_id"] = context_id
    resp = httpx.post(
        f"{_base_url()}/message:send",
        headers=_headers(),
        json={"message": message, "configuration": {"return_immediately": True}},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["task"]


def _wait(task_id: str, budget_seconds: float = 900) -> tuple[dict, float]:
    started = time.monotonic()
    while True:
        resp = httpx.get(f"{_base_url()}/tasks/{task_id}", headers=_headers(), timeout=30)
        resp.raise_for_status()
        task = resp.json()
        elapsed = time.monotonic() - started
        if task["status"]["state"] not in WAITING:
            return task, elapsed
        if elapsed > budget_seconds:
            raise TimeoutError(f"still {task['status']['state']} after {elapsed:.0f}s")
        time.sleep(POLL_SECONDS)


def _cancel(task_id: str) -> None:
    httpx.post(f"{_base_url()}/tasks/{task_id}:cancel", headers=_headers(), timeout=30)


def _artifact(task: dict, name: str) -> dict | None:
    for artifact in task.get("artifacts") or []:
        if artifact.get("name") == name:
            for part in artifact.get("parts") or []:
                if isinstance(part, dict) and "data" in part:
                    return part["data"]
    return None


def _run(prompt: str, **options) -> tuple[dict, float]:
    task = _send(prompt, _config(**options))
    return _wait(task["id"])


def _leaks(task: dict) -> bool:
    return CANARY in json.dumps(task)


def _propose(prompt: str) -> tuple[dict, dict | None]:
    task, _ = _run(prompt)
    return task, _artifact(task, "pending_action")


def _approve(task: dict, pending: dict) -> tuple[dict, float]:
    config = _config()
    config["confirm_pending_action"] = {"id": pending["id"]}
    resumed = _send("approved", config, task_id=task["id"], context_id=task.get("contextId") or task.get("context_id") or "")
    return _wait(resumed["id"], budget_seconds=1200)


def _change_round_trip(args, prompt: str, check) -> tuple[bool | None, str]:
    task, pending = _propose(prompt)
    if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED" or not pending:
        return False, f"expected INPUT_REQUIRED + pending_action, got {task['status']['state']} :: {_artifact(task, 'refusal')}"
    if not args.allow_writes:
        _cancel(task["id"])
        return None, f"proposal ok ({pending.get('action_name')}); approval skipped, pass --allow-writes"
    done, elapsed = _approve(task, pending)
    result = _artifact(done, "action_result") or {}
    ok = done["status"]["state"] == "TASK_STATE_COMPLETED" and check(result) and not _leaks(task) and not _leaks(done)
    return ok, f"{done['status']['state']} in {elapsed:.0f}s status={result.get('status')} :: {pending.get('action_name')}"


# ---------------------------------------------------------------- scenarios


def scenario_card(_args):
    """The agent card advertises the kubernetes_ops skill."""
    card = httpx.get(f"{_base_url()}/.well-known/agent-card.json", timeout=30).json()
    skills = [s.get("id") for s in card.get("skills") or []]
    return "kubernetes_ops" in skills, f"name={card.get('name')!r} skills={skills}"


def scenario_health(_args):
    """Namespace health finds the three planted failures, with causes."""
    task, elapsed = _run(f"check the health of the pods in namespace {NAMESPACE}")
    report = _artifact(task, "health_report") or {}
    pairs = [(f.get("name") or "", str(f.get("reason"))) for f in report.get("findings") or [] if f.get("kind") == "Pod"]
    found = {
        "crash": any(n.startswith("e2e-crash-") and "CrashLoopBackOff" in r for n, r in pairs),
        "oom": any(n.startswith("e2e-oom-") and "OOMKilled" in r for n, r in pairs),
        "image": any(n.startswith("e2e-badimage-") and r in ("ImagePullBackOff", "ErrImagePull") for n, r in pairs),
    }
    # The namespace also holds a Deployment named like it (fixture `ari-k8s-e2e`): still a namespace check.
    scope = (report.get("scope") or {}).get("type")
    ok = task["status"]["state"] == "TASK_STATE_COMPLETED" and all(found.values()) and scope == "namespace" and not _leaks(task)
    return ok, f"{task['status']['state']} in {elapsed:.0f}s scope={scope} found={found} leak={_leaks(task)}"


def scenario_health_prefix(_args):
    """A Deployment's health check leaves out a prefix namesake's pods and events (e2e-web vs e2e-web-gateway)."""
    task, elapsed = _run("check the health of the e2e-web deployment")
    report = _artifact(task, "health_report") or {}
    blob = json.dumps({k: report.get(k) for k in ("findings", "warning_events", "container_resources")})
    scope = report.get("scope") or {}
    ok = (
        task["status"]["state"] == "TASK_STATE_COMPLETED"
        and scope.get("deployment") == "e2e-web"
        and "e2e-web-gateway" not in blob
    )
    return ok, f"{task['status']['state']} in {elapsed:.0f}s scope={scope} gateway_mentioned={'e2e-web-gateway' in blob}"


def scenario_refuse_managed(_args):
    """A plain replica request on KEDA- and HPA-managed Deployments is refused."""
    notes, ok = [], True
    for name in ("e2e-keda", "e2e-hpa"):
        task, _ = _run(f"scale {name} to 3 replicas")
        refusal = _artifact(task, "refusal") or {}
        hit = task["status"]["state"] == "TASK_STATE_COMPLETED" and "autoscaler" in refusal and _artifact(task, "pending_action") is None
        ok = ok and hit
        notes.append(f"{name}:{'refused' if hit else task['status']['state']}")
    return ok, " ".join(notes)


def scenario_refuse_zero(_args):
    """Scaling to zero is never proposed."""
    task, _ = _run("scale e2e-plain to 0 replicas")
    return _artifact(task, "refusal") is not None and _artifact(task, "pending_action") is None, task["status"]["state"]


def scenario_refuse_limit_below_request(_args):
    """A memory limit below the current request is refused, not auto-adjusted."""
    task, _ = _run("set the memory limit of e2e-plain to 8Mi")
    refusal = _artifact(task, "refusal") or {}
    return "exceed" in str(refusal.get("reason")), str(refusal.get("reason"))[:160]


def scenario_clamp(_args):
    """A CPU value above node capacity is clamped, and the proposal says so (cancelled, never applied)."""
    task, pending = _propose("set the cpu request of e2e-plain to 64 and the cpu limit to 64")
    if pending:
        _cancel(task["id"])
    clamped = (pending or {}).get("clamped") or []
    ok = bool(clamped) and all(c.get("requested") == "64" for c in clamped)
    return ok, f"clamped={[(c.get('field'), c.get('applied')) for c in clamped]}"


def scenario_clamp_sidecar(_args):
    """A memory cap leaves room for the pod's other container (e2e-sidecar's 256Mi proxy; cancelled)."""
    task, pending = _propose("set the memory limit of container app in e2e-sidecar to 4000Gi")
    if pending:
        _cancel(task["id"])
    clamped = (pending or {}).get("clamped") or []
    basis = " ".join(str(c.get("basis")) for c in clamped)
    ok = bool(clamped) and "other containers' requests 256Mi" in basis
    return ok, f"clamped={[(c.get('field'), c.get('applied')) for c in clamped]}"


def scenario_high_replicas(_args):
    """More than 50 replicas is proposed as critical (cancelled, never applied)."""
    task, pending = _propose("scale e2e-plain to 51 replicas")
    if pending:
        _cancel(task["id"])
    tier = (pending or {}).get("risk_tier")
    return tier == "critical", f"risk_tier={tier} action_name={(pending or {}).get('action_name')}"


def scenario_drift(_args):
    """A Helm/Flux-labelled Deployment gets a drift notice and a persistence hint (cancelled)."""
    task, pending = _propose("scale e2e-helm-labelled to 2 replicas")
    if pending:
        _cancel(task["id"])
    pending = pending or {}
    ok = (pending.get("ownership") or {}).get("class") == "helm" and bool(pending.get("drift_notice")) and "persistence" in pending
    return ok, f"ownership={pending.get('ownership')} persistence.broken_at={(pending.get('persistence') or {}).get('broken_at')}"


def scenario_scale(args):
    """Propose -> approve -> scale e2e-plain to 3 -> verified; then back to 2."""
    ok, note = _change_round_trip(args, "scale e2e-plain to 3 replicas", lambda r: r.get("status") == "succeeded")
    if ok and args.allow_writes:
        _change_round_trip(args, "scale e2e-plain to 2 replicas", lambda r: True)
    return ok, note


def scenario_keda_max(args):
    """Explicit KEDA max change on e2e-keda is proposed, approved and applied."""
    return _change_round_trip(args, "set the KEDA max replicas of e2e-keda to 4", lambda r: r.get("status") == "succeeded")


def scenario_hpa_min(args):
    """Explicit HPA min change on e2e-hpa is proposed, approved and applied."""
    return _change_round_trip(args, "set the HPA min replicas of e2e-hpa to 2", lambda r: r.get("status") == "succeeded")


def scenario_slow_start(args):
    """A resource change on the slow-starting fixture waits for the rollout instead of timing out."""
    return _change_round_trip(
        args, "set the memory limit of e2e-slow to 96Mi", lambda r: r.get("status") == "succeeded" and (r.get("verification") or {}).get("waited_seconds", 0) > 60
    )


def scenario_fix_stuck(args):
    """Raising the memory limit of a Deployment stuck in ProgressDeadlineExceeded (e2e-memhungry) succeeds:
    the old deadline is not read as the fix's verdict."""
    return _change_round_trip(args, "set the memory limit of e2e-memhungry to 128Mi", lambda r: r.get("status") == "succeeded")


@contextlib.contextmanager
def _port_forward(namespace: str, service: str, port: int = 18003):
    proc = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward", f"svc/{service}", f"{port}:80"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{url}/healthz", timeout=2).status_code == 200:
                    break
            time.sleep(1)
        yield url
    finally:
        proc.terminate()


def scenario_resume_after_restart(args):
    """A proposal survives an API restart: propose, restart the API Deployment, approve (the task store volume)."""
    target, service = os.environ.get("ARI_API_DEPLOYMENT"), os.environ.get("ARI_API_SERVICE")
    if not args.allow_writes or not target or not service:
        return None, "needs --allow-writes, ARI_API_DEPLOYMENT and ARI_API_SERVICE (<namespace>/<name>)"
    task, pending = _propose("scale e2e-plain to 4 replicas")
    if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED" or not pending:
        return False, f"expected INPUT_REQUIRED + pending_action, got {task['status']['state']}"
    namespace, deployment = target.split("/", 1)
    subprocess.run(["kubectl", "-n", namespace, "rollout", "restart", f"deployment/{deployment}"], check=True, capture_output=True)
    subprocess.run(["kubectl", "-n", namespace, "rollout", "status", f"deployment/{deployment}", "--timeout=300s"], check=True, capture_output=True)
    svc_namespace, svc = service.split("/", 1)
    previous = os.environ.get("ARI_BASE_URL")
    with _port_forward(svc_namespace, svc) as url:
        os.environ["ARI_BASE_URL"] = url
        try:
            done, elapsed = _approve(task, pending)
            result = _artifact(done, "action_result") or {}
            ok = done["status"]["state"] == "TASK_STATE_COMPLETED" and result.get("status") == "succeeded"
            if ok:
                _change_round_trip(args, "scale e2e-plain to 2 replicas", lambda r: True)
        finally:
            if previous is None:
                os.environ.pop("ARI_BASE_URL", None)
            else:
                os.environ["ARI_BASE_URL"] = previous
    return ok, f"{done['status']['state']} in {elapsed:.0f}s status={result.get('status')} after restarting {target}"


def scenario_persist(args):
    """The persistence hint is a usable hand-off: plain Git coordinates, manifest-string values, and
    ready_to_persist only once the change succeeded (e2e-helm-labelled 1 -> 2 -> 1). The Git fields
    are only checked when the fixture's Flux labels name a HelmRelease that exists on the cluster."""
    task, pending = _propose("scale e2e-helm-labelled to 2 replicas")
    if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED" or not pending:
        return False, f"expected INPUT_REQUIRED + pending_action, got {task['status']['state']}"
    before = pending.get("persistence") or {}
    if not args.allow_writes:
        _cancel(task["id"])
        return before.get("ready_to_persist") is False, "proposal only; pass --allow-writes for the result"
    done, _ = _approve(task, pending)
    after = (_artifact(done, "action_result") or {}).get("persistence") or {}
    changes = after.get("changes") or []
    git = after.get("git")
    ok = (
        before.get("ready_to_persist") is False
        and after.get("ready_to_persist") is True
        and [(c.get("from"), c.get("to")) for c in changes] == [("1", "2")]
        and (git is None or bool(git.get("repo_slug") and git.get("branch") and git.get("commit")))
        and not _leaks(done)
    )
    _change_round_trip(args, "scale e2e-helm-labelled to 1 replica", lambda r: True)
    chart = ((after.get("helmrelease") or {}).get("chart_source") or {}).get("url")
    return ok, f"git={git and git.get('repo_slug')} chart_source={chart} changes={changes} broken_at={after.get('broken_at')}"


SCENARIOS = {
    "card": scenario_card,
    "health": scenario_health,
    "health_prefix": scenario_health_prefix,
    "refuse_managed": scenario_refuse_managed,
    "refuse_zero": scenario_refuse_zero,
    "refuse_limit": scenario_refuse_limit_below_request,
    "clamp": scenario_clamp,
    "clamp_sidecar": scenario_clamp_sidecar,
    "high_replicas": scenario_high_replicas,
    "drift": scenario_drift,
    "scale": scenario_scale,
    "keda_max": scenario_keda_max,
    "hpa_min": scenario_hpa_min,
    "slow_start": scenario_slow_start,
    "persist": scenario_persist,
    "fix_stuck": scenario_fix_stuck,
    "resume_after_restart": scenario_resume_after_restart,
}


def main() -> int:
    _load_env_file(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="*")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--allow-writes", action="store_true", help="approve proposals so they are applied to the fixtures")
    args = parser.parse_args()
    if args.list:
        for name, fn in SCENARIOS.items():
            print(f"{name:15} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0
    chosen = args.scenarios or list(SCENARIOS)
    unknown = [s for s in chosen if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; known: {list(SCENARIOS)}")
    print(f"agent: {_base_url()}   fixtures: {NAMESPACE}   writes: {args.allow_writes}\n")
    results = []
    for name in chosen:
        print(f"--- {name} ...", flush=True)
        try:
            ok, note = SCENARIOS[name](args)
        except Exception as exc:  # noqa: BLE001 — report and continue
            ok, note = False, f"{type(exc).__name__}: {exc}"
        results.append((name, ok, note))
        print(f"    {({True: 'PASS', False: 'FAIL', None: 'SKIP'})[ok]}: {note}\n", flush=True)
    failed = [n for n, ok, _ in results if ok is False]
    print("=" * 72)
    for name, ok, note in results:
        print(f"{({True: 'PASS', False: 'FAIL', None: 'SKIP'})[ok]:5} {name:15} {note[:120]}")
    print("=" * 72)
    print(f"{len(results)} scenarios, {len(failed)} failed" + (f": {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
