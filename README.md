# ARI On-Call Agent

A standalone agent that receives an incident/prompt + full configuration in
a single request, autonomously clones a repo, analyzes the problem, generates
and verifies a fix (via [opencode](https://github.com/sst/opencode) running
in a sandbox), pushes it to a branch, and asks a person to approve the pull
request before opening it — then reports back.

**A pull request is never opened without a human saying so.** The run pauses
in A2A's `TASK_STATE_INPUT_REQUIRED` carrying everything needed to judge the
change (title, target branch, files, an excerpt of the diff, and a compare
link to the pushed branch), and the pull request is opened only once the
caller confirms. See
[`docs/INPUT_REQUIRED_INTEGRATION.md`](docs/INPUT_REQUIRED_INTEGRATION.md)
for that contract.

It's a **genuine A2A (Agent2Agent) protocol server**, built on the official
[`a2a-sdk`](https://pypi.org/project/a2a-sdk/) (Linux Foundation project) —
JSON-RPC and REST/HTTP+JSON transports, a real `/.well-known/agent-card.json`,
real `Task`/`Message`/`Part` objects. See
[`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) for how to call it,
including a runnable sample client. (An earlier version of this agent had a
hand-rolled REST API that only borrowed A2A's vocabulary without being
wire-compatible — see `docs/MEMORY.md` for why that changed.)

It is **not** part of the `multi-agent-ai` service mesh. It has no shared
database, no shared config service, and no dependency on `shared/`. Every
request is fully self-contained (its own LLM key/model, its own repo
credentials) and it is meant to be built, deployed, and scaled independently.
All agents are deployed only on Kubernetes.

## Agent kinds

The same framework hosts several independently deployed *agent kinds*
(`AGENT_KIND`), each with its own Agent Card, registries under
`configs/<kind>/` and worker image:

| Kind | What it does | Worker image |
|---|---|---|
| `oncall` | Everything described above: code analysis, fix, PR after approval | `ari-oncall-agent-worker` (`Dockerfile.oncall`) |
| `k8s` | Pod-level health of the cluster it runs in; Deployment scaling and CPU/memory changes, each proposed first and applied only after a person approves — see [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) §10 | `ari-k8s-agent-worker` (`Dockerfile.k8s`) |

The Jenkins kind is deprecated. `docs/ARCHITECTURE.md` explains how kinds
are added.

## Agent kinds

The same framework hosts several independently deployed *agent kinds*
(`AGENT_KIND`), each with its own Agent Card, registries under
`configs/<kind>/` and worker image:

| Kind | What it does | Worker image |
|---|---|---|
| `oncall` | Everything described above: code analysis, fix, PR after approval | `ari-oncall-agent-worker` (`Dockerfile.oncall`) |
| `jenkins` | Reads and diagnoses a Jenkins job or build; triggers or stops a build, each proposed first and run only after a person approves — see [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) §9 | `ari-jenkins-agent-worker` (`Dockerfile.jenkins`) |

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) explains how kinds are added.

## Why it looks the way it does

- **Temporal-only execution.** There is no "inline" fallback engine. Every
  request runs as one Temporal workflow, one A2A `Task`. Temporal (hosted
  externally, see `.env`) is what gives durability/retries/observability
  without this service needing its own database.
- **Plan, then execute — dynamically.** The prompt varies request to
  request ("...and skip tests", "just open the PR", "check the repo in dev
  branch and raise a PR against staging", a bare question with no repo work
  at all). Nothing is hardcoded as a fixed pipeline. A planning step
  (LLM-driven, using the *request's own* LLM config) looks at the prompt +
  the registered capability/tool catalog and emits an ordered step plan,
  including which branch to work from and which to open a PR against when
  the prompt names them. A generic executor then runs whatever that plan says.
- **opencode does the actual coding work.** `analyze` / `generate_change` /
  `verify` are thin capability wrappers around opencode running against the
  cloned repo in an isolated sandbox workspace. This service does not
  reimplement code-editing or test-running logic.
- **One A2A operation, three ways to get the result.** Every request is the
  same `message/send` call. A caller that blocks gets the result inline once
  the `Task` reaches a terminal state (what used to be this agent's separate
  "chat" endpoint). A caller that sets `configuration.return_immediately:
  true` gets the `Task` back right away and either polls `tasks/get` or
  registers a `pushNotificationConfig` for a webhook — the SDK delivers that
  webhook itself, progressively, as the task's status changes; this agent
  never POSTs a callback itself. (What used to be this agent's separate
  "task" endpoint, now the same operation, just non-blocking.)
- **Repos are cached, not re-cloned every time.** A shared local mirror per
  repo URL is kept fresh with an incremental fetch on every use; each
  request still gets its own fully independent working copy cloned from
  that mirror, so concurrent requests against the same repo never interfere.
- **No persistence.** No chat history, no request store. The A2A SDK's
  in-memory `TaskStore` gives best-effort history within one task's
  lifetime, not a durable record — see `docs/design.md` for what that
  means in practice. Temporal's own workflow history is the audit trail.

See [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) for how to call this
agent, [`docs/design.md`](docs/design.md) for the full design, and
[`docs/MEMORY.md`](docs/MEMORY.md) for the decision/verification history
behind it (including what's been confirmed against a real opencode binary,
real repos, real API keys, and the real A2A SDK, vs. what's still unverified).

## Layout

```
ari-action-agent/
├── configs/<kind>/          # declarative capability/tool registries per agent kind (extend here, not in code)
├── deploy/k8s-agent-e2e/    # temporary test-cluster deployment + fixtures for the k8s kind
├── docs/
│   ├── ARCHITECTURE.md      # repo map, and how to add an agent kind
│   ├── DEVELOPER_GUIDE.md   # how to call the agents (A2A), with sample clients
│   ├── design.md            # design reference
│   └── MEMORY.md            # decision/verification history
├── scripts/                 # live scenario scripts and local helpers
├── src/
│   ├── common/              # agent-kind-agnostic engine: A2A server, Temporal workflow/activities, planner, respond
│   ├── ari_oncall_agent/    # the oncall kind's capabilities, tools and metadata
│   ├── k8s_agent/           # the k8s kind's capabilities, tools and metadata
│   └── api/                 # thin entrypoints: api.main (A2A server), api.worker (Temporal worker)
└── tests/
```

## Extending it

- **New skill** (the way to add capabilities without changing this code):
  drop an [Agent Skills](https://agentskills.io/specification) folder into
  `skills/`, or mount it as a subfolder of `SKILLS_DIR`. The planner can run it on its
  own, and the change agent can use it during a code change. See
  [`docs/SKILLS.md`](docs/SKILLS.md) and `ari skills validate`.
- **New capability**: add a module under `<kind package>/capabilities/`, add an entry to
  `configs/<kind>/capabilities.yaml` (module path, description, tools, retry/on_failure
  policy). The planner picks it up automatically because it reads the catalog
  at plan time — no engine code changes.
- **New tool**: add a module under `<kind package>/tools/`, add an entry to
  `configs/<kind>/tools.yaml`, reference it from whichever capability's `tools:` list
  needs it.
- **New pipeline family** (e.g. a non-code-fix trigger later): the planner
  and executor are already generic over the capability catalog — a new class
  of request just means new capabilities/tools registered, same engine.

## Running locally

Temporal itself is **not** run locally by default — `TEMPORAL_HOST` in `.env`
points at the externally hosted cluster/namespace this service connects to
in production. For local testing, `deploy/temporal-dev/` stands up a
throwaway Temporal + Web UI in Docker:

```bash
# 1. Local Temporal (Docker) + Web UI on :8233
cd deploy/temporal-dev && docker compose up -d && cd ../..

# 2. This agent
cp .env.example .env   # set TEMPORAL_HOST=localhost:7233, TEMPORAL_NAMESPACE=default,
                        # AGENT_API_KEYS, OPENCODE_BIN (path to an installed opencode binary)
pip install -e .        # the k8s kind's worker: pip install -e ".[k8s]"
scripts/run_worker.sh  # Temporal worker (executes capabilities)
scripts/run_api.sh     # FastAPI app (accepts /v1/chat and /v1/tasks)
```

See [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) for exactly how to
call this agent (blocking, polling, and push-notification-webhook — all the
same `message/send` operation) and a runnable sample client at
`docs/examples/sample_client.py` (reads `OPENAI_API_KEY` / `GITHUB_PAT` from
the environment, never hardcoded). `scripts/e2e_callback_receiver.py` is
this project's own generic webhook-logging tool for local testing.

## Testing

Two layers, and they catch different things. Only the first one needs Docker.

### Unit tests

`pytest tests/` covers every capability against mocked tools. Docker is not a
preference here — 11 modules need `a2a`/`fcntl` and cannot even be *collected*
on Windows, so run them in the worker image:

```bash
docker run --rm -v "$PWD:/work" -w /work insightfinderinc/ari-oncall-agent-worker:test   sh -lc 'pip install -q pytest pytest-asyncio "kubernetes>=37,<38"; PYTHONPATH=/work/src python -m pytest tests/ -q'
```

**A green suite is not enough on its own.** Mocks always return something, so
they cannot produce the three conditions that have actually broken this agent
in production: a model that edits nothing while reporting that it did, a clock
that accumulates across retries, and a brand-new file that `git diff HEAD`
cannot see. All three shipped past a passing suite; see `docs/MEMORY.md`,
2026-10-09.

### Live scenarios against a deployed agent

`scripts/oncall_live_test.py` closes that gap. It is a plain HTTP client — no
Docker, no local agent, nothing to stand up. Its only dependency is `httpx`:

```bash
pip install httpx

export ARI_BASE_URL=...  ARI_API_KEY=...
export LLM_PROVIDER=anthropic LLM_API_KEY=... LLM_MODEL=claude-haiku-4-5
export GITHUB_PAT=...    REPO_URL=https://github.com/<org>/<repo>  REPO_REF=<branch>

python scripts/oncall_live_test.py --list           # the six scenarios
python scripts/oncall_live_test.py                  # all of them, no writes
python scripts/oncall_live_test.py budget           # just one
python scripts/oncall_live_test.py --allow-writes   # include the one that opens a PR
```

`ARI_BASE_URL` is wherever the agent actually answers. An environment that
publishes an ingress can be hit directly (`https://<host>`); one that does not
is reached the same way its own callers reach it, through the in-cluster
Service:

```bash
kubectl -n <ns> port-forward svc/ari-oncall-api 18001:80 &
export ARI_BASE_URL=http://127.0.0.1:18001
```

That second form is worth preferring even when an ingress exists: it is the
path MCP Service itself uses (`ARI_ONCALL_AGENT_URL=http://ari-oncall-api`), so
it tests what production traffic traverses rather than an edge the product does
not use. `ARI_API_KEY` is the agent's own `AGENT_API_KEYS` for that
environment.

| scenario | what it proves |
|---|---|
| `card` | the agent card is served where integrations look for it |
| `budget` | a request given N seconds stops at ~N, instead of running to the plan's design maximum |
| `empty` | a change that edits nothing fails fast, before `verify` is handed something that does not exist |
| `propose` | the pause: `INPUT_REQUIRED` + a `pending_action` whose branch is already pushed |
| `confirm` | approve the proposal and a pull request opens — design.md §5c's round trip |
| `reject` | a confirm carrying an id the task never proposed is refused, not quietly planned around |

It is deliberately **not** in `tests/`: each scenario costs real LLM tokens and
real clone/build time, and two of them write a branch and a pull request to
`REPO_URL`. Run it by hand against a test environment, never in CI, and point
`REPO_URL` at a repository you do not mind collecting branches in.

Two things it assumes, both learned the hard way (`docs/MEMORY.md`): assert on
`status.state` and on which artifacts resolved, never on the final message —
that text is an LLM writing for a human and is paraphrased differently every
run. And each confirm scenario pauses its own task, because a confirm (valid
or bogus) moves a task to a terminal state that A2A cannot return from.

The `k8s` kind has its own equivalent, `scripts/k8s_agent_live_test.py`,
run against the fixtures in `deploy/k8s-agent-e2e/` on a test cluster
(see that directory's README). Without `--allow-writes` it only reads and
cancels its proposals; with it, it approves changes to the fixture
Deployments. Its settings (`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`,
`ARI_BASE_URL`, `ARI_API_KEY`) come from the environment or from a
git-ignored `scripts/.env`. Its unit and workflow tests (`tests/test_k8s_*.py`) run
against a fake API server and need no cluster.

Neither layer sees everything. Both are per-request: they assert on what one
call returns. Anything that only shows up by accumulating inside a
long-running worker — leaked child processes, growing caches — is invisible to
both, and is found by looking at the process, not at a response.

See `docs/MEMORY.md` for what's been verified against real infrastructure
(and real gaps found and fixed along the way: Temporal persists request
secrets unencrypted in its event history — needs a Payload Codec before any
non-local deploy; a few real A2A SDK integration bugs only surfaced by
actually calling the endpoints, not by reading the SDK source).

<!-- CI test: verifying test-branch docker tag pushes correctly -->
