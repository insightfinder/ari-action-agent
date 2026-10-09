"""ARI On-Call Agent — live scenario test against a deployed agent.

The unit suite runs every capability against mocked tools, which is why
it stayed green through three defects that only appear when something
real misbehaves: a model that edits nothing while claiming it did, a
clock that accumulates across retries, a network that is slower than a
local filesystem (see docs/MEMORY.md, 2026-10-09). This script drives the
deployed agent over its real A2A surface instead, and asserts on what
came back.

It is deliberately not part of `tests/` — every scenario costs real LLM
tokens, real clone/build time, and in two cases writes a branch to a real
repository. Run it by hand against test/stg, never in CI.

    export ARI_BASE_URL=http://localhost:8000
    export ARI_API_KEY=...
    export OPENAI_API_KEY=...          # or LLM_PROVIDER/LLM_API_KEY/LLM_MODEL
    export GITHUB_PAT=...              # needs push + PR rights on REPO_URL
    export REPO_URL=https://github.com/insightfinder/...

    python scripts/oncall_live_test.py            # everything
    python scripts/oncall_live_test.py budget     # one scenario
    python scripts/oncall_live_test.py --list

Scenarios that open a pull request (`confirm`) are skipped unless
`--allow-writes` is passed, so the default run is safe to repeat.
"""

import argparse
import json
import os
import sys
import time
import uuid

import httpx

POLL_SECONDS = 5
TERMINAL_PREFIXES = ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")

_DEFAULT_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5"}


# --- plumbing --------------------------------------------------------------


def _base_url() -> str:
    return os.environ.get("ARI_BASE_URL", "http://localhost:8000").rstrip("/")


def _headers() -> dict:
    return {
        "x-api-key": os.environ.get("ARI_API_KEY", "dev-local-e2e-key"),
        "A2A-Version": "1.0",
        "Content-Type": "application/json",
    }


def _llm() -> dict:
    provider = os.environ.get("LLM_PROVIDER", "openai")
    key = os.environ.get("LLM_API_KEY") or os.environ.get(f"{provider.upper()}_API_KEY")
    if not key:
        raise SystemExit(f"no API key for provider {provider!r}: set LLM_API_KEY or {provider.upper()}_API_KEY")
    model = os.environ.get("LLM_MODEL") or _DEFAULT_MODELS.get(provider)
    if not model:
        raise SystemExit(f"no default model known for provider {provider!r}: set LLM_MODEL")
    return {"provider": provider, "api_key": key, "model": model}


def _config(**options) -> dict:
    repo = {"url": os.environ.get("REPO_URL", "https://github.com/octocat/Hello-World")}
    pat = os.environ.get("GITHUB_PAT")
    if pat:
        repo["pat"] = pat
    ref = os.environ.get("REPO_REF")
    if ref:
        repo["ref"] = ref
    return {"repo": repo, "llm": _llm(), "options": options}


def _send(prompt: str, config: dict, *, task_id: str = "", context_id: str = "") -> dict:
    """One `message/send`, non-blocking. Returns the Task as submitted."""
    message = {
        "message_id": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": prompt}, {"data": config}],
    }
    # A resume has to carry both, or the agent treats it as a new request
    # and the confirm id resolves against nothing.
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


def _wait(task_id: str, budget_seconds: float) -> tuple[dict, float]:
    """Polls to a terminal state. Returns (task, elapsed). INPUT_REQUIRED is
    terminal here — the task sits there until something confirms it."""
    started = time.monotonic()
    while True:
        resp = httpx.get(f"{_base_url()}/tasks/{task_id}", headers=_headers(), timeout=30)
        resp.raise_for_status()
        task = resp.json()
        state = task["status"]["state"]
        elapsed = time.monotonic() - started
        if state not in TERMINAL_PREFIXES:
            return task, elapsed
        if elapsed > budget_seconds:
            raise TimeoutError(f"still {state} after {elapsed:.0f}s (client gave up; the run may continue)")
        time.sleep(POLL_SECONDS)


def _state(task: dict) -> str:
    return task["status"]["state"]


def _text(task: dict) -> str:
    parts = (task.get("status", {}).get("message") or {}).get("parts") or []
    return " ".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _artifact(task: dict, name: str) -> dict | None:
    for artifact in task.get("artifacts") or []:
        if artifact.get("name") == name:
            for part in artifact.get("parts") or []:
                if isinstance(part, dict) and "data" in part:
                    return part["data"]
    return None


def _branch_exists(repo_url: str, branch: str) -> bool | None:
    """None when it cannot be checked (no PAT, or a non-GitHub remote) —
    reported as unknown rather than silently counted as a pass."""
    pat = os.environ.get("GITHUB_PAT")
    slug = repo_url.rstrip("/").removesuffix(".git").split("github.com/")[-1]
    if not pat or "/" not in slug:
        return None
    resp = httpx.get(
        f"https://api.github.com/repos/{slug}/branches/{branch}",
        headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    return resp.status_code == 200


# --- scenarios -------------------------------------------------------------
#
# Each returns (ok, note). `ok=None` means "could not be decided from this
# run" — reported separately from a failure, because a scenario that
# depends on the model behaving a particular way is evidence when it
# reproduces and nothing when it doesn't.


def scenario_card(_args) -> tuple[bool | None, str]:
    """The agent card is served at the path the integration actually uses."""
    resp = httpx.get(f"{_base_url()}/.well-known/agent-card.json", timeout=30)
    resp.raise_for_status()
    card = resp.json()
    skills = [s.get("id") for s in card.get("skills") or []]
    ok = "code_analysis_fix" in skills
    return ok, f"name={card.get('name')!r} skills={skills}"


def scenario_budget(_args) -> tuple[bool | None, str]:
    """A request given 120s of budget must stop at ~120s, not run for the
    plan's design maximum. This is the one scenario that does not depend
    on what the model decides to do: the clock is the assertion.

    Asks for verification explicitly, since `verify` is opt-in and is the
    step whose retry loop produced the 45m56s run.
    """
    budget = 120
    prompt = (
        "Investigate this repository, make one small improvement, then run the test suite "
        "and make sure it builds before you finish."
    )
    task = _send(prompt, _config(pipeline_budget_seconds=budget))
    task, elapsed = _wait(task["id"], budget_seconds=budget * 4)
    state, text = _state(task), _text(task)
    # Generous upper bound: the budget bounds the step loop, and cleanup +
    # resolve_artifacts + respond still run after it.
    within = elapsed < budget * 2.5
    ok = state == "TASK_STATE_FAILED" and within
    return ok, f"{state} after {elapsed:.0f}s (budget {budget}s) :: {text[:200]}"


def scenario_empty(_args) -> tuple[bool | None, str]:
    """Asking for something already true must end quickly, before `verify`
    is ever handed a change that does not exist. Depends on the model
    actually declining to edit, so a run where it edits anyway proves
    nothing either way and is reported as such.

    Decided structurally, not from the final message. `respond` is an LLM
    writing for a human: on the run this was built against it rendered the
    guard as "didn't actually modify any files" rather than echoing the
    raised error, so a string match on its prose reported INCONCLUSIVE for
    a run that had in fact worked perfectly. The agent's own integration
    guide says this outright -- "task.status.message's text is for a human
    to read, not for your code to parse" -- and the signal it points at is
    the artifact list: `root_cause` resolves from `generate_change.
    root_cause`, so a `generate_change` that raised leaves it absent.
    Confirmed against the real workflow result, whose `data` held
    `clone_repo` alone: no generate_change entry, and no verify at all.

    Reaching here means `budget` already drove a request through
    clone_repo and into verify, so a clone failure is not a plausible
    alternative explanation for FAILED-with-no-root_cause by this point.
    """
    prompt = (
        "Make sure this repository has a README file. If it already has one, change nothing at all — "
        "do not reword it, do not add to it, do not create any other file. Then verify the build."
    )
    task = _send(prompt, _config())
    task, elapsed = _wait(task["id"], budget_seconds=900)
    state, text = _state(task), _text(task)
    produced_a_change = _artifact(task, "root_cause") is not None
    if state == "TASK_STATE_FAILED" and not produced_a_change:
        # A single verify attempt alone costs its own timeout_seconds (480s
        # for this agent) before the on_failure loop even starts, so
        # finishing well inside that is itself evidence verify never ran.
        ok = elapsed < 420
        return ok, f"guard fired: FAILED, no root_cause artifact, {elapsed:.0f}s :: {text[:160]}"
    return None, (
        f"model edited something or answered instead ({state}, "
        f"root_cause={'yes' if produced_a_change else 'no'}, {elapsed:.0f}s) :: {text[:160]}"
    )


def _pause_a_task(args, prompt: str) -> tuple[dict, dict | None, float]:
    """Drives one request to the approval pause. Returns (task, pending
    action, elapsed).

    Both `propose` and `reject` need a paused task of their own: a confirm
    — valid or bogus — moves that task to a terminal state, and the A2A
    protocol has no way back from one. Sharing a single paused task between
    them made whichever ran second fail with a bare HTTP 400 that said
    nothing about the real cause.
    """
    task = _send(prompt, _config())
    task, elapsed = _wait(task["id"], budget_seconds=1800)
    return task, _artifact(task, "pending_action"), elapsed


def scenario_propose(args) -> tuple[bool | None, str]:
    """The pause. A normal fix request must end at INPUT_REQUIRED with a
    `pending_action` carrying everything an approval email needs, and the
    branch must already be on the remote so `compare_url` resolves.
    """
    prompt = os.environ.get(
        "ARI_TEST_PROMPT",
        "Add a short CONTRIBUTING.md describing how to run the tests. Keep it under 20 lines.",
    )
    task, action, elapsed = _pause_a_task(args, prompt)
    state = _state(task)
    if state != "TASK_STATE_INPUT_REQUIRED":
        return False, f"expected INPUT_REQUIRED, got {state} after {elapsed:.0f}s :: {_text(task)[:200]}"
    if not action:
        return False, "INPUT_REQUIRED but no pending_action artifact"

    required = ["id", "action", "repo", "branch", "base_ref", "title", "body", "changed_files", "compare_url"]
    missing = [f for f in required if not action.get(f)]
    pushed = _branch_exists(action.get("repo_url") or _config()["repo"]["url"], action.get("branch", ""))

    # Hand the next scenario what it needs, so propose->confirm can run in
    # one invocation without a human copying ids around.
    args.carry = {
        "task_id": task["id"],
        "context_id": task.get("contextId") or task.get("context_id") or "",
        "pending_action_id": action["id"],
        "branch": action.get("branch"),
    }
    ok = not missing and pushed is not False
    note = (
        f"{elapsed:.0f}s branch={action.get('branch')} pushed={pushed} "
        f"files={len(action.get('changed_files') or [])} hunks={len(action.get('key_hunks') or '')}c"
    )
    if missing:
        note += f" MISSING={missing}"
    return ok, note


def scenario_reject(args) -> tuple[bool | None, str]:
    """A confirm carrying an id the task never proposed must fail, not fall
    through to ordinary planning and open something anyway.

    Pauses its own task rather than reusing `propose`'s — see
    `_pause_a_task`.
    """
    task, action, _ = _pause_a_task(args, "Add a one-line comment to any single source file.")
    if _state(task) != "TASK_STATE_INPUT_REQUIRED" or not action:
        return None, f"could not get a paused task to reject against ({_state(task)})"

    config = _config()
    config["confirm_pending_action"] = {"id": str(uuid.uuid4())}
    task = _send(
        "approving",
        config,
        task_id=task["id"],
        context_id=task.get("contextId") or task.get("context_id") or "",
    )
    task, elapsed = _wait(task["id"], budget_seconds=300)
    state = _state(task)
    return state == "TASK_STATE_FAILED", f"{state} after {elapsed:.0f}s :: {_text(task)[:200]}"


def scenario_confirm(args) -> tuple[bool | None, str]:
    """The round trip docs/MEMORY.md has recorded as unverified since
    2026-09-18: pause, approve, pull request. Writes to a real repo.
    """
    carry = getattr(args, "carry", None)
    if not carry:
        return None, "needs `propose` to have run first in the same invocation"
    if not args.allow_writes:
        return None, "skipped: opens a real pull request, pass --allow-writes"
    config = _config()
    config["confirm_pending_action"] = {"id": carry["pending_action_id"]}
    task = _send("approved, open the pull request", config, task_id=carry["task_id"], context_id=carry["context_id"])
    task, elapsed = _wait(task["id"], budget_seconds=600)
    state = _state(task)
    pr = _artifact(task, "pull_request") or {}
    url = pr.get("url") if isinstance(pr, dict) else None
    ok = state == "TASK_STATE_COMPLETED" and bool(url)
    return ok, f"{state} after {elapsed:.0f}s pr={url} :: {_text(task)[:160]}"


SCENARIOS = {
    "card": scenario_card,
    "budget": scenario_budget,
    "empty": scenario_empty,
    "propose": scenario_propose,
    "reject": scenario_reject,
    "confirm": scenario_confirm,
}

# propose must precede confirm (which consumes the task it pauses).
# reject pauses its own, so its position only affects cost.
ORDER = ["card", "budget", "empty", "propose", "confirm", "reject"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="*", help="scenario names; default all, in dependency order")
    parser.add_argument("--allow-writes", action="store_true", help="permit scenarios that open a pull request")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = parser.parse_args()
    args.carry = None

    if args.list:
        for name in ORDER:
            print(f"{name:10} {(SCENARIOS[name].__doc__ or '').strip().splitlines()[0]}")
        return 0

    chosen = args.scenarios or ORDER
    unknown = [s for s in chosen if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; known: {list(SCENARIOS)}")
    chosen = [name for name in ORDER if name in chosen]

    print(f"agent: {_base_url()}   repo: {_config()['repo']['url']}\n")
    results = []
    for name in chosen:
        print(f"--- {name} ...", flush=True)
        try:
            ok, note = SCENARIOS[name](args)
        except Exception as exc:  # a scenario blowing up is a result, not a crash
            ok, note = False, f"{type(exc).__name__}: {exc}"
        label = {True: "PASS", False: "FAIL", None: "INCONCLUSIVE"}[ok]
        print(f"    {label}: {note}\n", flush=True)
        results.append((name, ok, note))

    print("=" * 72)
    for name, ok, note in results:
        print(f"{ {True: 'PASS', False: 'FAIL', None: '????'}[ok] :5} {name:10} {note}")
    failed = [n for n, ok, _ in results if ok is False]
    print("=" * 72)
    print(f"{len(results)} scenarios, {len(failed)} failed" + (f": {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
