# ARI On-Call Agent

A standalone agent that receives an incident/prompt + full configuration in
a single request, autonomously clones a repo, analyzes the problem, generates
and verifies a fix (via [opencode](https://github.com/sst/opencode) running
in a sandbox), and opens a pull request — then reports back.

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
credentials) and it is meant to be built, deployed, and scaled independently
(EC2, a Kubernetes Job/Deployment, etc.).

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
- **opencode does the actual coding work.** `analyze` / `generate_fix` /
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
on-call-ari-agent/
├── configs/                # declarative capability/tool registries (extend here, not in code)
├── deploy/k8s/              # Kubernetes manifests (api + worker deployments)
├── docs/
│   ├── DEVELOPER_GUIDE.md   # how to call this agent (A2A), with a sample client
│   ├── design.md            # full design doc
│   └── MEMORY.md            # decision/verification history
├── scripts/                 # local run helpers
├── src/ari_agent/
│   ├── a2a_server/           # AgentExecutor bridging A2A's Task lifecycle to our Temporal workflow, Agent Card, auth
│   ├── api/                  # internal config models (validated out of a request's DataPart)
│   ├── orchestration/        # Temporal client, worker, workflow, activities
│   ├── planning/              # LLM-driven dynamic step planner
│   ├── capabilities/          # one module per pipeline step (clone, analyze, fix, verify, publish, respond)
│   ├── tools/                 # concrete actions capabilities call (git, repo cache, opencode, sandbox)
│   ├── llm/                   # per-request LLM client factory (never a global/shared key)
│   └── models/                 # Plan/Step schema shared by planner + executor
└── tests/
```

## Extending it

- **New capability**: add a module under `capabilities/`, add an entry to
  `configs/capabilities.yaml` (module path, description, tools, retry/on_failure
  policy). The planner picks it up automatically because it reads the catalog
  at plan time — no engine code changes.
- **New tool**: add a module under `tools/`, add an entry to
  `configs/tools.yaml`, reference it from whichever capability's `tools:` list
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
pip install -e .
scripts/run_worker.sh  # Temporal worker (executes capabilities)
scripts/run_api.sh     # FastAPI app (accepts /v1/chat and /v1/tasks)
```

See [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) for exactly how to
call this agent (blocking, polling, and push-notification-webhook — all the
same `message/send` operation) and a runnable sample client at
`docs/examples/sample_client.py` (reads `OPENAI_API_KEY` / `GITHUB_PAT` from
the environment, never hardcoded). `scripts/e2e_callback_receiver.py` is
this project's own generic webhook-logging tool for local testing.

See `docs/MEMORY.md` for what's been verified against real infrastructure
(and real gaps found and fixed along the way: Temporal persists request
secrets unencrypted in its event history — needs a Payload Codec before any
non-local deploy; a few real A2A SDK integration bugs only surfaced by
actually calling the endpoints, not by reading the SDK source).
