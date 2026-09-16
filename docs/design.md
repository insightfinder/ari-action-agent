# ARI On-Call Agent — Design

Current-state architecture reference. For *why* things ended up this way,
what's been verified against real infrastructure, and the history of bugs
found and fixed along the way, see [`docs/MEMORY.md`](MEMORY.md) — that's
the decision/verification log; this doc is just what's true today.

## 1. What this is

A standalone agent that takes an incident/prompt plus full repo/LLM
configuration in a single request, autonomously clones the repo, analyzes
the problem in an isolated sandbox using [opencode](https://github.com/sst/opencode),
generates and verifies a fix, and opens a pull request.

Design principles:

- **Standalone.** Its own repo, its own deploy (EC2/Kubernetes), no
  dependency on any other service, database, or shared config store. Auth
  is a static API key per request.
- **Stateless.** No chat history store, no request database. Every
  request carries everything it needs; nothing is persisted between
  requests except a local repo-mirror cache (§6) purely as a performance
  optimization, and an in-memory Task store (§2) that only needs to
  survive one process's lifetime.
- **Genuinely A2A-protocol-compliant** (the [Agent2Agent
  protocol](https://a2a-protocol.org), built on the official
  [`a2a-sdk`](https://pypi.org/project/a2a-sdk/), a Linux Foundation
  project) — JSON-RPC and REST/HTTP+JSON transports today; see §2. Not a
  custom API that merely borrows A2A's vocabulary.
- **Dynamic planning, not a fixed pipeline.** Prompts vary too much
  ("skip tests", "just open the PR", "check the repo in dev branch and
  raise a PR against staging", a bare question with no repo work at all)
  to hardcode a step sequence. A planner — using the request's own LLM —
  reads the prompt and a capability catalog and emits the step list for
  that request.
- **opencode does the coding work.** Analysis, editing, and test-running
  are wrappers around opencode running in a sandboxed workspace, not
  bespoke LLM-tool-calling loops.
- **Temporal-backed execution.** Every request runs as one Temporal
  workflow (hosted externally — this service is only a client/worker
  against it), which is what gives durability/retries/observability
  without this service needing its own database.

See `docs/DEVELOPER_GUIDE.md` for how to actually call this agent, with
real request/response examples and a runnable sample client.

## 2. A2A protocol surface

There is exactly one operation, `message/send` — no separate "chat" vs.
"task" endpoint. What differs is how the *caller* chooses to receive the
result, entirely via standard A2A mechanics:

| | Blocking | Non-blocking + polling | Non-blocking + webhook |
|---|---|---|---|
| Caller gets | Final `Task`, inline | `Task` (submitted) immediately | `Task` (submitted) immediately |
| Completion | Same call blocks until terminal state | Caller calls `tasks/get` | The SDK POSTs progressive updates to a registered `pushNotificationConfig` |

All three run the exact same `AriOnCallWorkflow` via the exact same
`AriAgentExecutor.execute()` (`a2a_server/executor.py`) — there is no
second code path. `context.task_id` (the A2A Task ID) **is** the Temporal
workflow ID, one identifier end to end; Temporal's own workflow history is
the audit trail.

**Transports**: JSON-RPC (`POST /`) and REST/HTTP+JSON
(`POST /message:send`, `GET /tasks/{id}`, etc.), both served from the same
FastAPI app via `add_a2a_routes_to_fastapi`. gRPC is not implemented — it
needs a genuinely separate server process (HTTP/2 + protobuf framing, not
ASGI) and its own port/dependencies; deferred until a real caller needs it
(§7).

**Auth**: `x-api-key` header, checked against `AGENT_API_KEYS`, wired in
via a custom `ServerCallContextBuilder` (`a2a_server/auth.py`). Every
request also needs an `A2A-Version: 1.0` header — the SDK treats its
absence as an older, unsupported protocol version.

**Config `DataPart`**: the prompt travels as a plain `TextPart`; this
agent's own config (repo URL/credentials, LLM provider/key/model, free-form
`options` like `skip_tests`, an optional system-prompt override) rides
alongside it as a `DataPart` on the same `Message` — see
`docs/DEVELOPER_GUIDE.md` §5 for the exact shape. There is no `callback`
field in this config; delivering a result asynchronously is the A2A
protocol's own concern (`pushNotificationConfig`), not this agent's.

**State**: `InMemoryTaskStore` and `InMemoryPushNotificationConfigStore` —
deliberate, not a shortcut. This agent stores no request state of its own
by design; these only give `tasks/get` and best-effort per-task
`history` something to read within one process's lifetime. A restart
means callers re-register, same as re-submitting a request against any
stateless service.

**The executor** (`a2a_server/executor.py:AriAgentExecutor.execute()`):

1. Extracts the prompt (joined `TextPart`s), config (`DataPart`), and prior
   conversation history (reconstructed from the current `Task`'s own
   accumulated history, when the message continues an existing
   task/context).
2. Enqueues the initial `Task` object (state `SUBMITTED`) — this is what
   lets a non-blocking caller's response return immediately.
3. Starts `AriOnCallWorkflow` via the Temporal client, with the A2A task ID
   as the workflow ID.
4. Awaits the workflow's result (this can take minutes; that's fine —
   it's one of many concurrent asyncio tasks in the API process, and it's
   exactly what lets a *blocking* caller's connection stay open until
   completion).
5. Maps the result onto `Task` artifacts (`root_cause`, `pull_request`
   when present) and a final status update (§4).

Not implemented, deliberately: fine-grained per-capability progress as
separate status updates (Temporal's own workflow history already has that
detail — this reports `SUBMITTED → WORKING → one terminal state`), and
`TASK_STATE_INPUT_REQUIRED` (a genuine paused "waiting on your reply"
flow) — `respond` (§4) always gives a definitive answer instead.

## 3. Core abstractions

| Abstraction | What it is | Where |
|---|---|---|
| **Request** | `RequestConfig` — repo, llm, system_prompt override, options. Self-contained, immutable. | `api/schemas.py` |
| **Plan** | Ordered list of `Step`s (capability + params), produced per-request by the planner. | `models/plan.py`, `planning/planner.py` |
| **Capability** | One pipeline step's behavior (`clone_repo`, `analyze`, `generate_fix`, `verify`, `publish_pr`, `respond`). Declared in `configs/capabilities.yaml` (description, tools, retry, on_failure); implemented as `run(ctx) -> dict` in `capabilities/`. | `capabilities/` |
| **Tool** | A concrete callable a capability may use (`git_clone`, `opencode`, `repo_cache`, `llm_complete`, ...). Declared in `configs/tools.yaml`; implemented in `tools/`. | `tools/` |

Adding a capability or tool = new module + new YAML entry. No changes to
`orchestration/workflow.py`, `orchestration/activities.py`, or the API
layer — this is the extensibility seam: new purposes later are new
capabilities/tools/catalog entries, not a rewrite.

## 4. Planning, execution, and the response

1. The `plan_task` activity calls `planning/planner.py`, which sends the
   prompt + `capabilities.registry.catalog()` (names, descriptions, tools)
   to the request's own LLM and gets back an ordered step list. It honors
   `config.options` (e.g. `skip_tests`) and dynamic branch names in the
   prompt (§6) as explicit directives. Malformed planner output falls back
   to the standard clone→analyze→fix→verify→publish pipeline rather than
   failing the request.
2. `plan_task` enriches each step with its capability's `retry` and
   `on_failure` policy from `configs/capabilities.yaml`, so
   `orchestration/workflow.py` itself never has to read the registry
   (keeps it free of filesystem/import side effects, required for
   Temporal's workflow sandbox).
3. `workflow.py` walks the steps via the generic `execute_step` activity.
   `verify` failing loops back to `generate_fix` up to `max_attempts`,
   then the run ends in `fail_needs_human` (`status: "failed"`) rather
   than spinning forever.
4. Every step happens against one ephemeral sandbox workspace
   (`tools/sandbox.py`, keyed by `request_id`), cleaned up when the
   workflow ends — one request's bad patch/hung test can't affect another.
5. A final `respond` capability **always** runs after the loop above,
   regardless of what the planner chose or whether the pipeline gave up.

**Response shape.** `AriOnCallWorkflow.run()` returns one plain dict:

```json
{
  "request_id": "...",
  "status": "completed",
  "message": "Fixed the WaitGroup Add/Wait race in discoverScan.go and added CLI flags for subnet/port range; verify passed; opened PR #1.",
  "root_cause": { "summary": "...", "evidence": [...] },
  "pr": { "url": "...", "branch": "ari/fix-..." },
  "data": { "...": "raw per-capability results, for programmatic callers" }
}
```

`status`: `completed | failed | no_action | needs_approval` (the last one
reserved — no policy/approval gate exists yet). `a2a_server/executor.py`
maps this onto the real A2A wire shape: `message` becomes the completed
`Task`'s final `Message`, `root_cause`/`pr` become named `Artifact`s, and
`status` maps onto `TaskState` (`completed`/`no_action` →
`TASK_STATE_COMPLETED`, `failed` → `TASK_STATE_FAILED`, `needs_approval` →
`TASK_STATE_INPUT_REQUIRED`, unused today).

`message` is never a template. It comes from `respond`
(`capabilities/respond.py`) — an `internal: true` capability (excluded
from the planner's catalog, so the planner never picks it itself) that
`workflow.py` calls directly and unconditionally after the planner-chosen
steps finish or give up. It uses the request's own LLM over the prompt,
conversation history, and whatever step results exist:

- **No steps ran** (a plain question, or a turn already answered by
  history) → `respond` answers the question directly.
- **Steps ran** → `respond` synthesizes root cause, what changed and why,
  verify status, and the PR link into one coherent summary.
- **Pipeline gave up** → `respond` explains what's blocking it.
- **Always states things definitively, never a dangling question.** A
  caller may block, poll, or register a webhook, and `respond` has no idea
  which — so its system prompt forbids ending with a question or an offer
  conditioned on a reply, since this agent doesn't implement
  `TASK_STATE_INPUT_REQUIRED` (§2). If `respond` itself fails after
  retries, a templated one-liner is used as a last resort — the request
  must never come back with no message at all.

## 5. opencode + GitHub MCP integration

opencode does all the actual analysis/editing/test-running *and* the
GitHub PR operations — the latter via opencode's own built-in MCP client
pointed at the public GitHub MCP server, not a custom Python MCP client.

- **Per-request `opencode.json`**, written into the cloned repo right
  after clone and torn down with the rest of the sandbox:
  `provider.<llm.provider>.options.apiKey` (that request's own key, never
  a shared one), and — since GitHub credentials are also per-request —
  `mcp.github` pointed at `https://api.githubcopilot.com/mcp/` with
  `Authorization: Bearer <repo.pat>` and `oauth: false` (forces PAT auth).
  Kept out of the repo's own git history via `.git/info/exclude`, not
  `.gitignore` (which opencode itself could edit).
- **Four scoped opencode agents**, each `mode: "primary"` (required for
  `--agent <name>` selection to actually take effect): `ari-analyze`
  (read-only, `edit: deny`), `ari-fix` (`edit: allow`, no build tooling),
  `ari-verify` (`edit: deny`, broad `bash: allow` since test/build
  commands are unpredictable per-repo), `ari-publish` (`edit: deny`, the
  *only* agent with `github_*` MCP tools enabled). `analyze`/
  `generate_fix`/`verify`/`publish_pr` each call `opencode run --agent
  <name> ...` with only the matching agent.
- **One ephemeral `opencode run` process per capability step**, not a
  long-lived `opencode serve` — a Temporal activity is a self-contained
  unit of work, and Temporal doesn't guarantee two activities for the same
  workflow land on the same worker. Continuity across steps comes from
  opencode's own `--continue` flag (resume the most recent session for
  that `--dir`), gated on whether an earlier step in this run already
  started one — no session ID is threaded through Temporal payloads.
  `--auto` (auto-approve anything not explicitly denied) is a backstop;
  agent permission blocks only ever use `allow`/`deny`, never `ask`, since
  nothing is present to answer a prompt.
- **Output parsing** (`tools/opencode_cli.py:_parse_events`) reads
  `--format json`'s NDJSON event stream: each line's `part.type` is
  `"text"` or `"tool"` (tool parts carry `tool`/`callID`/
  `state.{status,input,output,error}`); a fatal error arrives as a
  top-level `{"type": "error", ...}` event on stdout, not stderr.
  `publish_pr` reads the PR URL out of the matching GitHub MCP tool call's
  result, with a regex-over-response-text fallback. `generate_fix`
  captures the diff via plain `git diff HEAD` rather than trusting
  opencode's own account of what it changed.

**LLM client** (`llm/client.py`) uses the official SDKs — `openai>=3.14`
(`AsyncOpenAI`, the Responses API) and `anthropic>=1.6` (`AsyncAnthropic`,
the Messages API) — used only by the planner and `respond`; a fresh client
is constructed per call since the key is per-request, never global.
Unrelated to opencode's own LLM calls, which it manages itself via
`provider.apiKey` in the per-request `opencode.json`.

## 6. Repo caching & dynamic branches

Two related requirements: many requests target the same repo over time
(repeat incidents against the same service), so re-cloning from the
remote every time wastes time/bandwidth/disk; and a single prompt can name
*different* branches for "where to work from" and "what to open the PR
against" ("check the repo in the dev branch... raise a PR against
staging"), so neither can be a single static field.

**Caching** (`tools/repo_cache.py`): one `git clone --mirror` per unique
repo URL, kept in `REPO_CACHE_ROOT` (persists across requests —
deliberately separate from `SANDBOX_ROOT`, which is wiped per request).
Before every use, `ensure_cache()` runs a `git fetch` against it — latest
every time, not just on first use. `clone_repo` then does a fast **local**
clone from that mirror into the request's own sandbox — this is what
gives isolation: a `--mirror` cache has no working tree to edit in the
first place, and every request's own clone is a fully independent copy,
so concurrent requests against the same repo can never see or disturb
each other's in-progress patch. Locking is two-layered (an in-process
`asyncio.Lock` plus a real `fcntl.flock`) so concurrent requests against
the *same* repo serialize on updating that one mirror without blocking
requests against other repos.

A credential is only ever used transiently for the fetch/clone call —
never persisted into the mirror's own git config (scrubbed back to the
plain URL immediately after the initial clone). `clone_repo` re-points the
per-request clone's `origin` back at the real authenticated upstream
afterward, since `git remote -v` would otherwise show the cache path.

Documented limits: this cache lives on whichever worker host handles the
request — multiple worker replicas each keep an independent cache unless
`REPO_CACHE_ROOT` is a shared volume mounted at the same path on every
replica; there's no eviction/GC (fine for a bounded set of well-known
repos, revisit if that stops being true).

**Dynamic branches**: `clone_repo` accepts an optional `ref` step param
(which branch to check out — falls back to `repo.ref`/`"main"`), and
`publish_pr` accepts an optional `base_ref` step param (which branch the
PR targets — falls back to the same default, independently of whatever
`ref` was). The planner's system prompt reads the prompt for named
branches and sets these params.

`repo.url` can also be a local filesystem path, not just a network
remote — git supports this natively.

## 7. Deployment image

One `Dockerfile`, two entrypoints (`uvicorn ari_agent.main:app` for the
API, `python -m ari_agent.orchestration.worker` for the worker) — same
image, same dependencies, since capabilities/tools/planner are shared
code.

Built on `python:3.12-slim`, plus:

- **`git`, `curl`, `ca-certificates`** — `clone_repo` and friends shell
  out to `git` directly. `git config --system --add safe.directory '*'`
  is also set at build time, since cloning a directory owned by a
  different user than the container process (e.g. a bind-mounted host
  path) otherwise hits git's "detected dubious ownership" refusal — this
  must be real git config, not a per-command `-c` override.
- **Node.js 22 + `opencode-ai`** pinned to an exact version — floating to
  `latest` would silently invalidate the behavior this agent's opencode
  integration (§5) was built and verified against; bump the pin
  deliberately and re-verify.
- **`golang-go` + `openjdk-21-jdk-headless` + `maven`** — needed for
  `verify` to build/test *target* repos written in Go or Java, not for
  ARI itself. A deliberate, bounded bet: Go/Java/Node/Python are the
  languages this agent has actually been exercised against. Not full
  multi-language coverage — a target repo needing Rust, .NET, etc. will
  clone/analyze/fix fine (opencode doesn't need the target's own toolchain
  for that) but `verify` will fail with "command not found," correctly
  surfaced as a real failure rather than silently succeeding. Extend this
  list as real requests need more languages, or revisit with a
  per-request build sandbox (Docker-outside-of-Docker or an external build
  service) if the language spread outgrows "just add it to the image."

## 8. Known limitations

- **gRPC transport** not implemented — needs its own server process/port
  and `grpcio`/`grpcio-tools` dependencies. Deferred, not abandoned.
- **`TASK_STATE_INPUT_REQUIRED`** not implemented — no genuine paused
  "waiting for your reply" flow; a caller follows up by sending a new
  message in the same context instead.
- **Push-notification delivery retry semantics** are the SDK's
  `BasePushNotificationSender`'s concern, not audited here.
- **No repo-cache eviction/GC** — fine for a bounded set of well-known
  repos.
- **Secrets in Temporal's event history are not encrypted.** Temporal
  persists activity/workflow *inputs* as plain base64 in its event
  history, decodable via the Temporal UI/API. Since `RequestConfig` (LLM
  key, repo PAT) is passed as workflow input, every secret in a request is
  currently readable by anyone with Temporal UI/API access to the
  namespace. Needs a Temporal **Payload Codec** (encrypting data
  converter) before any non-local deployment — a real gap, not
  hypothetical.
- Concurrency/volume expectations are not yet established — informs
  worker replica count and Temporal task-queue sizing.
- Git providers beyond GitHub/GitLab are untested.
