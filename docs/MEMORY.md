# Development memory

A running log of *why* things are built the way they are and what's been
verified for real (not just designed on paper). `docs/design.md` is the
current-state design; this file is the decision/verification history
behind it — read it before changing an architectural choice, so you know
whether it was arbitrary or load-bearing.

Append to this file (don't rewrite history) as new decisions are made or
new things get verified against real systems (a live opencode binary, a
real repo, real API keys). Keep entries dated.

## 2026-09-15 — Scaffolded as a standalone service

Built at `on-call-ari-agent/` — a top-level folder in the `multi-agent-ai`
repo, sibling to `services/`, not inside it.

**Why standalone:** the agent must be completely independent of the rest
of the monorepo (own deploy on EC2/K8s, no `shared/` dependency, no
database). The monorepo's `services/orchestrator-service` was mid-deletion
at the time (a separate deprecation effort moving Temporal usage into
`agent-service`), so this agent deliberately does **not** plug into either
`agent-service` or `orchestrator-service` — it's a fresh, separate Temporal
client + worker against an externally hosted Temporal cluster, with its
own vendored copy of A2A types (`a2a/types.py`) rather than importing
`shared/a2a/`.

**Key architecture decisions:**
- **Temporal-only execution**, no inline fallback engine. Both request
  shapes start the same `AriOnCallWorkflow`: `POST /v1/chat` is
  synchronous (`client.execute_workflow`, follow-ups via caller-supplied
  history — this agent stores nothing); `POST /v1/tasks` is
  fire-and-forget (`client.start_workflow`, 202 + `request_id` ==
  Temporal workflow ID, result delivered later via callback). Task
  requests run to completion with zero human-in-the-loop follow-up.
- **Every setting travels in the request payload** — LLM provider/key/
  model, repo URL + PAT/SSH key, callback URL + key, optional system-prompt
  override, free-form `options` (e.g. `skip_tests`) — via `RequestConfig`
  (`extra="allow"` so new sections don't need a schema change). No env-var
  indirection, no server-side config store, fully stateless. Inbound auth
  is a static `x-api-key` checked against `AGENT_API_KEYS`.
- **Capabilities and tools are declarative**, not hardcoded: `clone_repo`,
  `analyze`, `generate_fix`, `verify`, `publish_pr` are registered in
  `configs/capabilities.yaml` / `configs/tools.yaml` (dotted module path +
  description + retry/on_failure policy) and loaded dynamically at plan
  time. Adding either is a new module + a new YAML entry — zero engine
  changes.
- **A planner, not a fixed pipeline.** `planning/planner.py` sends the
  prompt + the capability catalog to *that request's own* LLM and gets
  back a dynamic ordered step plan, so "skip tests," "just open a PR," or
  a bare question with no repo work at all are all just different plans,
  never special-cased in code. The `plan_task` Temporal activity enriches
  each step with its capability's retry/on_failure policy before handing
  it to the workflow, specifically so `orchestration/workflow.py` never
  has to read the registry itself (filesystem/import calls inside a
  Temporal workflow definition break its determinism/sandbox guarantees).

## 2026-09-15 — opencode + GitHub MCP integration

opencode (https://opencode.ai) does the actual code analysis/editing/
test-running *and* the GitHub PR operations, via its own built-in MCP
client pointed at GitHub's remote-hosted MCP server
(`https://api.githubcopilot.com/mcp/`) — there is no separate custom
Python GitHub MCP client.

- **Per-request `opencode.json`**, written into the cloned repo right
  after clone (`clone_repo` capability), git-excluded via
  `.git/info/exclude` (not `.gitignore`, which opencode itself could
  touch): that request's own LLM provider `apiKey`, and an `mcp.github`
  entry with `Authorization: Bearer <repo.pat>` + `oauth: false` (skips
  opencode's OAuth-discovery attempt in favor of the PAT).
- **Four scoped opencode agents** — `ari-analyze` (read-only), `ari-fix`
  (can edit, no build tooling), `ari-verify` (broad `bash: allow`, no
  edit — build/test commands are unpredictable per-repo), `ari-publish`
  (the *only* agent with `github_*` MCP tools enabled). Each of
  `analyze`/`generate_fix`/`verify`/`publish_pr` runs one ephemeral
  `opencode run --agent <name> --format json --auto` process — deliberately
  not a long-lived `opencode serve`, because a Temporal activity is
  supposed to be a self-contained unit of work and Temporal doesn't
  guarantee two activities for the same workflow land on the same worker;
  a subprocess kept alive across activity boundaries would be exactly the
  hidden cross-activity state that model is meant to avoid. Continuity
  across steps comes from opencode's own `--continue` flag (resumes the
  most recent session for that `--dir` from disk), not a session ID
  threaded through Temporal.
- **`llm/client.py` uses the official SDKs** — `openai>=3.14` (Responses
  API: `client.responses.create(instructions=, input=)` →
  `.output_text`) and `anthropic>=1.6` (`client.messages.create(system=,
  messages=)` → concatenated text blocks) — used only by the planner;
  opencode manages its own LLM calls independently via its `opencode.json`.

### Verified against a real opencode binary (auth intentionally invalid)

Installed `opencode-ai` (npm, v1.18.31) and ran it against a scratch repo
with a bad key — no live model call, but real CLI/config parsing, not just
documentation. Found two real bugs this way:

1. **`agent.<name>.mode` must be `"primary"`, not `"subagent"`.** With
   `"subagent"`, `opencode run --agent <name>` silently falls back to the
   built-in `build` agent (a one-line warning, no error) — which would
   have made every permission scope defined in `tools/opencode_cli.py`'s
   `_AGENT_DEFS` a no-op. Fixed: all four agents use `"mode": "primary"`.
2. **A fatal error arrives as `{"type": "error", "error": {"data":
   {"message": ...}}}` on stdout, not stderr** — stderr was empty even on
   a hard 401. `run_opencode` originally read `stderr` for its error
   message, which would've produced a blank error in production. Fixed via
   `_extract_error()` in `_parse_events`.

Also confirmed: `--format json` is NDJSON (one JSON object per stdout
line), and every CLI flag relied on (`--dir`/`--agent`/`--model`/`--auto`/
`--continue`) matches what's implemented.

### Full live run with real OpenAI/Anthropic keys

Ran `ari-analyze` → `ari-fix` → `ari-verify` in one continued session
against a scratch repo seeded with a real bug. Confirmed
`_parse_events`'s success-path handling (`part.type == "text"`/`"tool"`
inside each event's `part` field) was correct as originally written — the
wire format is `{"type": "<envelope-kind>", "part": {...}}` where the
envelope kind (`step_start`/`tool_use`/`step_finish`/`text`) is noise we
ignore; what matters is `part.type`, which matched the assumption exactly.
`llm/client.py`'s OpenAI and Anthropic paths both worked against live
APIs, and the planner correctly omitted the `verify` step when told "skip
tests."

**Found and fixed one more real gap:** `ari-verify` left a scratch test
file + `__pycache__/` untracked in the working tree despite being asked to
clean up, and `ari-publish`'s `git *` bash permission could have swept
those into a PR via a naive `git add -A`. Fixed by having `generate_fix`
also capture `git diff --name-only HEAD` (`tools/git_ops.py:changed_files`)
and telling `ari-publish` explicitly which files to stage, never `git add
-A`/`git add .`.

### Full pipeline run against a real public repo, including publish_pr

Ran the complete pipeline — `clone_repo → analyze → generate_fix → verify
→ (retry) generate_fix → verify → publish_pr` — against
`github.com/neyaadeez/go-script` (a small Go TCP port scanner), with a
real GitHub fine-grained PAT and a real OpenAI key, on a deliberately
open-ended "introduce some new feature" ask:

- `ari-analyze` picked a genuinely good improvement unassisted: the
  scanner spawned unbounded concurrent `net.DialTimeout` calls, so it
  proposed bounding them with a buffered-channel semaphore.
- `ari-fix` implemented exactly that, a clean one-file diff.
- `ari-verify` (`go build`/`go vet`/`gofmt`/`go test`) caught a **real
  gofmt violation** in the fix and reported `RESULT: FAIL` — the
  verify→retry loop was exercised for real: feeding the failure back to
  `ari-fix` produced a gofmt-clean fix on the second attempt, and verify
  then passed.
- `ari-publish` **failed the first time in a way that revealed a real
  gap**: its instruction never stated which GitHub repo to target, so
  opencode asked for clarification instead of inspecting the working
  tree's git remote itself. Fixed `capabilities/publish_pr.py` to state
  `Repository: <owner>/<repo> (<url>)` explicitly (`_repo_slug()` helper).
- After that fix, `ari-publish` correctly called the `github_create_branch`
  MCP tool — which then hit a **real `403 Resource not accessible by
  personal access token`** from GitHub's own API. Confirmed independently
  (outside opencode entirely, a raw `POST` to the same endpoint with the
  same PAT) that this is a token permission-scope issue (that fine-grained
  PAT lacked `Contents: Read and write` on the repo), not a bug in this
  agent — the integration made the right call, got a real API response,
  and surfaced the failure with actionable next steps rather than hanging
  or failing silently.

**Not yet exercised:** an actual successful push + PR creation. Needs a
token with `contents:write` + `pull_requests:write` on the target repo
(fine-grained) or classic `repo` scope. That's the next thing to confirm.

Real API keys/PATs used across all of the above were used transiently in
throwaway venvs/scratch directories, never written to any file in this
repo, never logged, and not retained anywhere once each test finished.

## 2026-09-15 — Full end-to-end test through the real HTTP API + local Temporal

Everything above exercised opencode/GitHub MCP directly, bypassing the
actual API/Temporal layer. This test drove the real thing: local Temporal
(Docker, `deploy/temporal-dev/docker-compose.yml` — postgres +
`temporalio/auto-setup:1.22` + `temporalio/ui:2.21.0`, the same image set
already used elsewhere in the monorepo's `infra/docker/docker-compose.yaml`),
the real FastAPI app (`scripts/run_api.sh`) and real Temporal worker
(`scripts/run_worker.sh`), a real callback receiver
(`scripts/e2e_callback_receiver.py`), and a real test client
(`scripts/e2e_client.py`) — against `github.com/neyaadeez/go-script` again,
with a real OpenAI key and a **second** GitHub PAT (properly scoped this
time — confirmed independently via a direct `POST .../git/refs` before
running anything, to avoid repeating the 403 from last time).

**Both request shapes worked end to end, for real:**
- `POST /v1/chat` with a repo-less conversational prompt returned
  synchronously in ~2.6s with `status: "no_action"` — confirms the planner
  correctly recognizes when a chat prompt needs no repository work and
  the whole request/response cycle (HTTP → Temporal `execute_workflow` →
  wait → HTTP response) works.
- `POST /v1/tasks` returned 202 + `request_id` immediately; the full
  pipeline (`clone_repo → analyze → generate_fix → verify → publish_pr`)
  ran in the worker (~6 minutes wall clock, mostly LLM/opencode latency),
  `verify` passed on the first attempt (no retry needed this time), and
  the callback arrived at the local receiver with the complete result
  payload. **A real PR was opened and independently confirmed via the
  GitHub API**: https://github.com/neyaadeez/go-script/pull/1 (open, 3
  files changed). This is the first time `publish_pr` has actually
  succeeded, not just been validated up to the point of a permission error.
  Notably, this run's `analyze` step found a more substantive issue than
  the earlier manual test — a real `sync.WaitGroup` Add/Wait race across
  the discovery/scan phases — and `generate_fix` fixed *that* in addition
  to adding the requested feature (CLI flags), unprompted. Confirms the
  pipeline's usefulness isn't limited to trivial bugs.
- Confirmed `.sandbox/` (this run's `SANDBOX_ROOT`) was empty again after
  the workflow finished — `cleanup_workspace` self-cleans correctly even
  through the full real Temporal path, not just in the earlier direct
  capability-level tests.

**One real finding worth acting on later, not fixed in this pass:**
Temporal stores activity/workflow **inputs in its event history in plain
base64** (not encrypted) — confirmed by decoding the `WorkflowExecutionStarted`
event's input payload via the Temporal UI's own REST API and finding the
real LLM key and GitHub PAT in it. Base64 is encoding, not encryption:
anyone with Temporal UI/API access to this namespace can read every
secret that ever passed through a request. For a real deployment this
needs a Temporal **Payload Codec** (data converter with encryption) so
secrets are encrypted before they hit Temporal's persisted history, or a
redesign where secrets are fetched at activity-execution time from a
secrets manager keyed by a reference ID instead of being passed as
workflow input directly. Out of scope for this test pass; flagged for
before any non-local deployment.

## 2026-09-15 — Fixed: responses had no real synthesis

User feedback after the full E2E test: the task response's `message` was
just `"Opened PR: <url>"` (no synthesis of what was actually found/fixed),
and — more seriously — the chat request had asked a real question
("what causes a NullPointerException...") and gotten back `{"status":
"no_action", "message": "No repository action was needed for this
request."}` — a real question given a non-answer. Both traced to the same
root cause: `orchestration/workflow.py`'s `_build_response` only ever
templated a one-line `message` from structured fields; nothing ever
actually *answered* the request, and `history` (already captured by the
API layer for chat follow-ups) was never threaded past the API into
planning or anywhere else.

**Fix:** a new `respond` capability (`capabilities/respond.py`), marked
`internal: true` in `configs/capabilities.yaml` so the planner never
chooses it — instead `workflow.py` calls it directly, unconditionally,
after the planner-chosen steps finish *or* the pipeline gives up
(deliberately not appended to the dynamic `steps` list: the
`fail_needs_human` `break` would have skipped it, same class of bug as
skipping cleanup). It uses the request's own LLM (a new `llm_complete`
tool wrapping `llm/client.py:complete`) with the prompt, conversation
history, and every step's results (empty if none ran) to write the actual
final `message` — a direct answer for a bare question, a real synthesis
for a full pipeline run, a plain explanation when something gave up. If
`respond` itself fails after retries, `workflow.py` falls back to the old
templated one-liner rather than raising — synthesis quality can degrade,
the request must never come back empty-handed.

Also fixed while here: `history` is now threaded from the API layer
through `plan_task` (the planner sees conversation context, so "yes, go
fix it" can resolve against what was discussed) and into every
`StepContext` (a new `history` field, `[]` by default — only `respond`
uses it today, but it's there for any future capability that needs it).

**Verified against the live local stack** (same Temporal/API/worker from
the run above, restarted to pick up the change): the exact NPE chat
question now gets a real, substantive answer; a repo-touching chat
request ("analyze HostsScan, fix the most important bug, skip tests, no
PR") produced a properly organized summary — root cause, what changed and
why, explicitly noting no tests were run and no PR was opened, matching
what was actually asked for. New tests in `tests/test_respond.py` (26
total passing) cover the direct-answer path, the full-synthesis path,
history/failure-reason inclusion, and that `respond` never appears in
`capabilities.registry.catalog()`.

## 2026-09-15 — Fixed: task responses asked dangling follow-up questions

User feedback on a real chat response: `respond`'s message ended with
"Which should I do next? 1) ... 2) ... 3) ...". Fine for chat (a human is
in that exact conversation and can reply next turn) — **wrong** for task
requests, which run autonomously and deliver their result once,
asynchronously, to a callback. There is no one to answer a task's dangling
question, ever.

**Root cause:** `respond` never knew which mode (`chat`/`task`) it was
responding to — its system prompt had no rule distinguishing them.

**Fix:** `orchestration/workflow.py` now passes `mode` into the `respond`
step's params; `capabilities/respond.py`'s system prompt has an explicit
rule: `mode: "chat"` may end with a clarifying question or an offered next
step, `mode: "task"` must state definitively what was found/changed/
verified/published (or why it's blocked) and phrase any follow-up worth
mentioning as a plain recommendation, never a question awaiting a reply.

**Verified live**: reran the full task pipeline against
`github.com/neyaadeez/go-script` (a different real bug this time — the
HostsScan function shelled out to the platform-specific `ping` binary,
non-portable across OSes) end to end through the real API/Temporal stack.
Real PR opened and independently confirmed:
https://github.com/neyaadeez/go-script/pull/2. The final `message` was a
definitive summary ending in "Next steps (recommended): ..." — a plain
recommendation, not a question. Added regression tests in
`tests/test_respond.py` (28 total passing) that pin the mode-specific rule
text in the system prompt and that `mode` reaches the LLM prompt correctly
for both chat and task.

## 2026-09-15 — Fixed: the Dockerfile didn't actually work

User asked directly: "when I build docker image and push, is everything
installed and setup right?" It wasn't. `python:3.12-slim` has **no `git`**
and **no Node.js/npm** — confirmed by actually running `which git`/`which
node` against the bare base image, both empty. `clone_repo` would have
failed immediately, and even past that, every opencode-backed capability
would fail with "opencode: command not found," since nothing in the
Dockerfile ever installed it. The Python SDKs (openai, anthropic,
temporalio, etc.) *were* fine — `pip install .` from `pyproject.toml`
already handled those correctly.

**Fixed:** the Dockerfile now installs `git`, Node.js 22, and
`opencode-ai` pinned to **1.18.31** — deliberately the exact version
everything above was actually verified against (a newer opencode release
could change the `mode: primary` requirement, the stdout-error-JSON shape,
or the `--format json` event schema; floating to `latest` would silently
invalidate all of that testing).

**Then found a second, deeper gap while verifying the fix**: `verify`
needs whatever build/test toolchain the *target* repo requires (`go
build`, `pytest`, `npm test`, ...), and the image had none beyond its own
Python runtime — my earlier Go-repo tests only worked because they ran
directly on this host machine, which happens to have Go installed; that
was never something the actual container could do. Asked the user how to
handle this (bundle common toolchains vs. document the limitation vs.
design a per-request build sandbox) — **chose to bundle**: added
`golang-go` to the Dockerfile (apt gives Go 1.24 on this base image,
satisfying `go-script`'s `go 1.23.1` requirement). This is a bounded,
explicit bet (Go/Node/Python only), not full multi-language coverage — a
repo needing Java/Rust/etc. will still fail `verify` with "command not
found," correctly reported as a real failure, just not automatically
fixable without extending the image further.

**Verified by actually building and running the image**, not just reading
the Dockerfile: built it, confirmed `git`/`node`/`opencode`/`go` all
present and each independently functional inside a running container
(`go version`, `git --version`, `opencode --version` all correct), booted
the container and hit `/healthz` + the agent card over real HTTP, and —
the strongest check — cloned the real `go-script` repo *inside the
container* and ran `go build ./...`, `gofmt -l .`, `go vet ./...` against
it for real, all clean.

Local test stack after this run: Docker containers `temporal-dev-postgres-1`
/ `temporal-dev-temporal-1` / `temporal-dev-temporal-ui-1`
(`deploy/temporal-dev/`), plus a worker + API + callback-receiver process
started via nohup, logging to `logs/{worker,api,callback,callback_server,client}.log`.
None of this is meant to be committed (`.env`, `.venv-run/`,
`.opencode-bin/`, `logs/`, `.sandbox/` are all gitignored) — `deploy/temporal-dev/`
and `scripts/e2e_*.py` are the only additions from this session meant to
stay in the repo, as reusable local-dev/test tooling.

## 2026-09-15 — Added Java, found two more real gaps testing against InfrastructureCore

User asked for Java support and to specifically confirm the agent could
"test, build and suggest fixes for" a real repo they use:
`insightfinder/InfrastructureCore` (private, remote
`git@github.com:insightfinder/InfrastructureCore.git`, a Maven `war`
project requiring Java 21 per `maven-compiler-plugin`). Added
`openjdk-21-jdk-headless` + `maven` to the Dockerfile (apt gives Java
21.0.12 / Maven 3.9.9 on this base image) and verified them independently
(`java -version`, `mvn -version`) — both correct.

**Found real gap #1: local-path cloning was broken.** The user pointed at
a local checkout path, not a URL — a case `git_ops.clone()` already
supported in principle (it just execs `git clone <url> <dest>`, and `url`
can be a filesystem path), but testing it for real (bind-mounting the real
repo into a container, as any deployment providing a repo via a mounted
volume would) hit git's "detected dubious ownership" refusal — a
container-owned process cloning a host-owned directory. Confirmed live
that `-c safe.directory=*` on the clone command itself does **not**
suppress this for a local clone's source (tried it — identical failure);
only real global/system git config does. Fixed via `git config --system
--add safe.directory '*'` in the Dockerfile (system-level, not
`--global`, so it applies regardless of which uid the container actually
runs as). Verified: our actual `git_ops.clone()` Python function,
unmodified, now clones the real bind-mounted `InfrastructureCore` repo
successfully.

**Found real gap #2, and it's the repo's own, not this agent's:** `mvn
compile` against a fresh clone reliably **fails** —
`com.Insightfinder:InsightfinderSDK:jar:1.45` and
`com.Insightfinder:IFUtility:jar:1.1.0` can't be resolved from Maven
Central. Investigated properly before concluding anything: no
`<repositories>` entry in the pom, no `~/.m2/settings.xml` pointing at an
internal Artifactory/Nexus (checked directly — doesn't exist on this
host), no README/script documenting a bootstrap step. The two jars are
vendored directly in the repo root (`InsightfinderSDK-1.45.jar`,
`IFUtility-1.1.0.jar`) and appear to only ever get into a working `~/.m2`
by a developer manually running `mvn install:install-file` by hand — an
undocumented, tribal-knowledge step, not something any clean CI
environment (ours or anyone else's) would have out of the box. (Caught my
own false-positive here too: an earlier quick check seemed to show `mvn
compile` succeeding — turned out to be a bash `pipefail` mistake, `$?`
after a `| tail` pipe reports `tail`'s exit code, not `mvn`'s. Redid it
piping to a log file instead and got the real answer: BUILD FAILURE, full
error captured.)

**Response, in keeping with "the LLM figures it out, not hardcoded
per-repo logic":** rather than special-casing this one repo anywhere,
broadened `capabilities/verify.py`'s default instruction with a general
heuristic — if a build fails on an unresolvable dependency, check whether
a matching artifact is vendored in the repo (root or a `lib/`-style
directory) and self-install it into the local package cache (Maven,
npm, pip, whatever applies) before concluding failure. `ari-verify`
already has broad `bash: allow`, so it's capable of doing exactly this
itself. **Not yet live-tested** against a real LLM key for this specific
scenario — no key was available this round, so this is a reasoned
improvement (the same category of self-resolution opencode has already
demonstrated live elsewhere in this project — see the gofmt-retry-loop
verification above), not yet confirmed against this repo's actual failure.
Next real work, if this comes up again: rerun `ari-verify` against
`InfrastructureCore` with a real key and confirm it actually resolves the
two vendored jars and gets to a real PASS.

## 2026-09-15 — Added repo caching + dynamic source/target branches

User's ask, directly: multiple chat/task requests can target the same
repo — don't re-clone from the remote every time (time, bandwidth, disk).
And prompts are dynamic about branches — "check the repo in dev branch...
raise a PR against staging" — so the branch to work from and the branch a
PR targets can genuinely differ per request, not a single static field.

**New: `tools/repo_cache.py`** — one `git clone --mirror` per unique repo
URL under `REPO_CACHE_ROOT` (a new setting, deliberately separate from
`SANDBOX_ROOT` since the cache must survive across requests while the
sandbox gets wiped per-request by `cleanup_workspace`). `ensure_cache()`
does a `git fetch` on *every* call, not just first use — "latest every
time." `clone_repo` (`capabilities/clone_repo.py`) now: ensures/refreshes
the cache, then does a fast **local** clone from it into the request's own
sandbox (`git_ops.clone()` needed zero changes — it already worked against
filesystem paths, confirmed against `InfrastructureCore` earlier today),
then re-points that clone's `origin` at the real authenticated upstream
(new `git_ops.set_origin()` — otherwise `git remote -v` would show the
cache path, which could confuse a tool or opencode itself poking around).

**Isolation, concretely verified**: a `--mirror` cache has no working
tree to edit in the first place, and each request's local clone from it
is a fully separate copy — proved this with a real test
(`tests/test_repo_cache.py`): cloned the same cache into two separate
working directories, edited the same file differently in each, confirmed
neither edit touched the other or the cache's own HEAD. Also proved
cache-freshness (`ensure_cache` again after a new commit → new commit
visible) and credential-scrubbing (after the initial clone, the cache's
persisted `origin` URL is the plain URL, never the PAT-embedded one) —
all with real local git repos, no mocking.

**Locking**: two-layered — an in-process `asyncio.Lock` (concurrent
activities in one worker process) plus `fcntl.flock` (concurrent worker
processes sharing a cache root on one host). Documented, not silently
assumed: multiple worker *replicas* each get an independent cache unless
`REPO_CACHE_ROOT` is a shared volume mounted at the same path on all of
them; no eviction/GC yet.

**Dynamic branches**: `clone_repo` takes an optional `ref` param (planner
sets it when the prompt names a source branch), `publish_pr` takes an
optional `base_ref` param (planner sets it when the prompt names a target
branch) — independent of each other. Updated `planning/planner.py`'s
system prompt with an explicit rule about this and **verified live**
against a real LLM: "check the repo in the dev branch... raise a PR
against staging" produced exactly `clone_repo.params.ref: "dev"` and
`publish_pr.params.base_ref: "staging"` in one real plan, `verify`
correctly omitted since testing wasn't asked for.

38 tests passing (10 new: 6 in `tests/test_repo_cache.py` using real git
repos, 4 in `tests/test_capabilities.py` for the ref/base_ref param
plumbing).

## 2026-09-15 — Adopted the real A2A protocol (this agent wasn't spec-compliant before)

User asked directly: "it is industry standard A2A agent right?" It wasn't.
The Agent Card discovery pattern and `Task`/`Message`/`Part` vocabulary were
borrowed, but the actual wire protocol (`POST /v1/chat`, `POST /v1/tasks`,
custom JSON bodies) was not the real A2A spec's JSON-RPC/gRPC/REST
transports — a real A2A client library couldn't have talked to this agent.

**Researched before writing any code** (a forked research pass, not
assumption): confirmed an official Python SDK exists —
[`a2a-sdk`](https://pypi.org/project/a2a-sdk/) on PyPI, backed by the Linux
Foundation's `a2aproject/a2a-python`. `pip install "a2a-sdk[fastapi]"`,
implement one `AgentExecutor` interface, the SDK owns Task/Message/Part
modeling, Agent Card serving, and JSON-RPC/REST/gRPC framing. JSON-RPC and
REST mount straight into an existing FastAPI app
(`add_a2a_routes_to_fastapi`); gRPC needs a genuinely separate server
process (HTTP/2 + protobuf, not ASGI) and `grpcio`/`grpcio-tools` codegen —
a real, separate lift, not a checkbox next to the other two. Asked the user
to scope this explicitly rather than assuming: chose **JSON-RPC + REST now,
gRPC as an explicit later phase**, and to **replace** `/v1/chat`/`/v1/tasks`
outright rather than keep them alongside the new surface (no real external
callers yet, and the old split was always just two ways of invoking the
same Temporal workflow — A2A's `message/send` + `return_immediately` +
`pushNotificationConfig` already covers both, so keeping the old endpoints
would just be maintaining two APIs doing the same thing).

**Before writing the executor, spent real effort getting the SDK's actual
protobuf-based API right** — installed it locally and introspected the real
package (`a2a.types`, `a2a.server.*`) rather than trusting the initial
research summary's paraphrased method names, which turned out to be wrong
in places (e.g. JSON-RPC methods are PascalCase gRPC-style names —
`SendMessage`, `GetTask` — not the `message/send`-style slash names an
earlier spec draft used). Confirmed via direct construction+serialization
tests (not just reading signatures) before writing `agent_card.py`: `Part`
fields (`text`/`data`/... — flat fields, not a oneof), how a `DataPart`
round-trips through `google.protobuf.json_format` + `Value`, the exact
`AgentCard`/`SecurityScheme`/`AgentInterface` shapes.

**New module `a2a_server/`** (`agent_card.py`, `auth.py`, `executor.py`)
replaces the old vendored `a2a/` package entirely (deleted). One
`AriAgentExecutor.execute()` bridges every request to the *same*
`AriOnCallWorkflow` as before — `context.task_id` is still the Temporal
workflow ID, one identifier end to end. Removed: `mode` from the workflow
entirely, the `deliver_callback` Temporal activity (the SDK's own
`BasePushNotificationSender` delivers webhooks now — this agent's code
never POSTs one itself), the `chat` vs. `task` distinction in
`capabilities/respond.py`'s prompt (now: never end with a dangling question,
full stop, regardless of how the caller is waiting).

**Found three real, undocumented bugs by actually calling the endpoints —
not by reading the SDK's source or docs:**
1. Skipping the `A2A-Version: 1.0` header (or writing a naive custom
   `ServerCallContextBuilder` that doesn't populate `state['headers']`,
   which is exactly what our first `ApiKeyCallContextBuilder` did) makes
   the SDK assume you meant protocol version `0.3` and reject the request.
   Fixed by delegating to `DefaultServerCallContextBuilder.build()` for
   everything except the actual API-key check.
2. A bare `TaskUpdater.submit()` (just a `TaskStatusUpdateEvent`) is
   rejected as the first event with "Agent should enqueue Task before
   TaskStatusUpdateEvent event" — the actual `Task` object has to be
   constructed and enqueued manually first.
3. `plan_task`'s Temporal activity had no retry-attempt cap (a pre-existing
   gap, not caused by this work) — a deliberately-bad LLM key during live
   testing made a *blocking* `message/send` call hang far past any
   reasonable timeout instead of failing, since Temporal's default retry
   policy has no maximum attempts. Fixed with an explicit
   `RetryPolicy(maximum_attempts=3, ...)` — also applied the same fix to
   `prepare_workspace` for consistency.

**Verified live, end to end, not mocked** — all three interaction patterns,
against the real running server:
- Blocking `message/send`: real OpenAI answer back in ~2–3s.
- Non-blocking (`return_immediately: true`): response in **0.03s** with
  `TASK_STATE_SUBMITTED`, polled via `GET /tasks/{id}` through `WORKING` to
  a real `TASK_STATE_COMPLETED` with `root_cause`/`pull_request` artifacts.
  One of these ran the full pipeline for ~5 minutes and opened a real PR:
  https://github.com/neyaadeez/go-script/pull/3 (independently confirmed
  via the GitHub API).
- Push notification webhook (`configuration.task_push_notification_config`
  inline on the request): the SDK delivered **progressive** updates itself
  — submitted → working → per-artifact updates (`root_cause`,
  `pull_request` individually) → completed — more granular than this
  agent's old single final callback, confirmed via a real local receiver.
  Opened a second real PR this way: https://github.com/neyaadeez/go-script/pull/4.
- Both transports: REST (`POST /message:send`) and JSON-RPC (`POST /` with
  `SendMessage`/`GetTask`) — same behavior through the same executor.

New: `tests/test_a2a_agent_card.py`, `tests/test_a2a_executor.py` (12
tests), plus `docs/DEVELOPER_GUIDE.md` (real captured request/response
examples throughout, not hand-written) and a runnable
`docs/examples/sample_client.py` demonstrating all three patterns with
plain `httpx` — tested against the real running server, including the
webhook mode (confirmed real progressive deliveries arriving).

## 2026-09-17 — Fixed: the published image could never complete a workflow

Deploying `insightfinderinc/ari-oncall-agent:test` to a real cluster (Test
EKS, via the `oncallari` Helm chart) surfaced a bug that never shows up in
development: **every containerised run died in its first activity.**

```
FileNotFoundError: '/usr/local/lib/python3.12/configs/capabilities.yaml'
```

Both registries located their YAML with a fixed number of hops —
`Path(__file__).resolve().parents[3] / "configs" / ...`. That is the repo
root when running from the source tree, but the Dockerfile runs
`pip install .`, so in the image the package sits in site-packages and
parents[3] resolves to `/usr/local/lib/python3.12`, while
`COPY configs ./configs` under `WORKDIR /app` puts the files at
`/app/configs`. Nothing bridged the two.

What made it easy to miss: **the API pod looks perfectly healthy.** It never
loads a registry at import time, so `/healthz` and the Agent Card both
answer 200. Only the worker's first activity (`plan_task`) touches
`catalog()`, so the failure reads as "the workflow failed", not "the image
is broken". Confirmed it was the image and not the deployment by exec'ing
into the pod: `/app/configs/` held both YAMLs all along, and symlinking
`/usr/local/lib/python3.12/configs -> /app/configs` made `catalog()` return
5 capabilities immediately.

**Fix — new `src/ari_agent/config_paths.py`.** Resolution is now a search,
not a hop count: `ARI_CONFIGS_DIR` if set, then the source-tree layout, then
`Path.cwd() / "configs"` (which is what covers the image, without
hard-coding `/app`), then `/app/configs`. The same wheel now works from a
checkout, from an editable install, and from the image. When nothing
matches, the error lists every location tried and names the env var, rather
than quoting one meaningless absolute path. Both registries call
`config_file(...)`; their `_CONFIG_PATH` constants and now-unused `pathlib`
imports are gone.

Verified inside a clean image, with no symlink:

```
configs_dir = /app/configs
capabilities loaded = 5
```

### Same session — made docs/examples/sample_client.py runnable as published

Three separate things forced an edit before the sample could run against a
deployed agent; all three are now environment-driven, with the previous
behaviour as the default.

- **The PAT was attached only in the modes that open a PR.** A private
  `REPO_URL` cannot even be *cloned* without it, so `chat` failed with git's
  own unhelpful `could not read Username for 'https://github.com'`. It is
  now included in every mode whenever `GITHUB_PAT` is set, and omitted when
  it isn't, so public repos still work untouched. `_config(with_pat=...)`
  is gone.
- **The provider was hardcoded to OpenAI.** `LLM_PROVIDER` / `LLM_API_KEY` /
  `LLM_MODEL` now override it, defaulting to openai + gpt-4o-mini exactly as
  before — an OpenAI key still needs no edits, and Anthropic no longer needs
  a patch. Missing keys and unknown providers exit with a message naming the
  variable to set.
- **`chat` timed out at 60s** while a full pipeline run takes ~2 minutes, so
  it reported a client-side `ReadTimeout` for tasks that had actually
  completed. Now 600s, `ARI_CHAT_TIMEOUT` to override.
- **The webhook callback was hardcoded to `http://localhost:9300/callback`.**
  In Kubernetes that address resolves to the agent's *own pod*, so no
  delivery can ever arrive — the existing note in the file covers the Docker
  case, but K8s is one layer further out. `WEBHOOK_URL` overrides it.
  Verified by deploying a receiver Pod + Service in-cluster and pointing the
  callback at its Service DNS: one task produced five deliveries — `task`,
  `statusUpdate WORKING`, `artifactUpdate root_cause`,
  `artifactUpdate pull_request`, `statusUpdate COMPLETED`. Artifacts stream
  as they are produced rather than arriving batched at the end.

### Tests

`tests/test_config_paths.py` (6) and `tests/test_sample_client.py` (8) are
new; the sample is loaded by path since `docs/examples/` isn't part of the
installed package. Full suite: **49 passed** on Linux. Three files
(`test_a2a_agent_card`, `test_a2a_executor`, `test_repo_cache`) import
`fcntl` and cannot be collected on Windows — run the suite on Linux, or in
the image, to cover them.

### Deployment notes (the chart lives in insightfinder/charts)

Two things the agent can't fix from its own side, handled in the
`oncallari` chart:

- **The worker exits if its Temporal namespace doesn't exist**
  (`Worker validation failed: Namespace oncall-ari-agent is not found`), and
  `temporal server start-dev` only ever auto-creates `default`. An init
  container on the worker now registers it on every pod start (idempotent).
- **`start-dev` keeps everything in one SQLite file.** On an `emptyDir` that
  file dies with the pod: a node rotation took the registered namespace and
  all workflow history with it and left the worker CrashLooping for hours.
  The chart's dev-mode persistence is now enabled by default.

Note for whoever tests this: the agent picks a genuinely different problem
each run against the same repo — a `None` guard, float rounding on money, a
return-type mismatch — so two runs do not produce the same PR. That is the
analysis working, not a flake.

### The persistence change broke the install, and only a clean one showed it

Turning dev-mode persistence on swapped the `emptyDir` for an EBS claim, and
Temporal then CrashLooped:

```
failed setting up schema: unable to create SQLite admin DB:
unable to open database file (14)
```

`temporalio/temporal` runs as `uid=1000(temporal)` (confirmed with
`docker run --entrypoint id`), but a freshly provisioned block volume mounts
root-owned, so `start-dev` cannot create its database file. Fixed with
`securityContext.fsGroup: 1000` on the pod, rendered only when persistence is
enabled — an emptyDir is already writable.

Worth recording *why this was nearly missed*: `helm upgrade` over the
existing release would never have hit it. The old release was still using its
emptyDir, so no claim was ever provisioned and nothing changed ownership.
The bug only appeared after `helm uninstall` + deleting the PVC + installing
from nothing. "It is running fine right now" is not evidence that a fresh
install works.

### Image tags, and why the test tag currently holds a hand-built image

CI only builds `main`/`staging`/`test`, so a feature branch produces no
image at all. The chart's default was `main`, which has never been built —
a release pinned to it fails ImagePullBackOff before anything else can be
diagnosed. The default is now `test`, with other branches left to an
install-time `--set ariOncallAgent.image.tag=...` rather than a chart edit.

That default is only correct once this fix is merged to the `test` branch.
Until then the `test` tag was **manually overwritten** with a build off
II-24719 (digest `sha256:9ee0dafa…`), so the Test EKS deployment runs exactly
what it will run post-merge and needs no change when that happens. The same
build was also pushed as `ii-24719`, which CI cannot clobber.

**The risk that buys:** a push to the `test` branch before this PR merges
rebuilds `test` without the configs fix, and the cluster silently reverts to
a worker that dies in `plan_task` — no alert, nothing visibly wrong until
someone runs a task. Recovery is one command:

```
helm upgrade oncallari <chart> -n insightfinder -f test-eks-values.yaml   --set ariOncallAgent.image.tag=ii-24719 --wait
```

Delete that override again once CI has rebuilt `test` from the merged code.

## 2026-09-17 — Fixed: `clone_repo` hardcoded "main" as the default branch

Live test against the deployed `test-ari-oncall-agent` (a real request against
`github.com/insightfinder/InsightAgent`, whose default branch is `master`, no
`ref` named in the prompt or request config) failed 3/3 attempts:

```
fatal: Remote branch main not found in upstream origin
```

**Root cause, traced through three layers, all assuming the same fixed
string:** `api/schemas.py`'s `RepoConfig.ref` defaulted to the literal
`"main"` (so `repo.get("ref")` in `clone_repo.py` was never actually falsy —
the "fall back to the remote's own default" branch could never be reached
even before it existed), `capabilities/clone_repo.py` had its own `"main"`
fallback, and `capabilities/publish_pr.py` re-derived `base_ref` from the same
repo config with a third independent `"main"` fallback. Three hardcodes,
none of them checked against what the remote actually reports.

**Fix, consistent with this project's "declarative, not hardcoded" rule for
capabilities/tools (see the 2026-09-15 scaffolding entry):** `RepoConfig.ref`
is now `Optional[str] = None` — "unset" is representable instead of silently
becoming `"main"`. New `tools/git_ops.py:default_branch()` reads
`git symbolic-ref --short HEAD` against the local mirror cache — a `--mirror`
clone mirrors the remote's `HEAD` symref verbatim, so this reports whatever
the remote's actual default branch is (`main`, `master`, anything else), no
guessing. New tool `git_default_branch` (`configs/tools.yaml`), added to
`clone_repo`'s tool list (`configs/capabilities.yaml`). `clone_repo.py`'s
resolution order is now: planner-named `ref` → request-configured `ref` →
the remote's real default branch — only hitting the cache lookup in the
genuinely-nothing-specified case. `publish_pr.py`'s `base_ref` fallback now
reads `clone_repo`'s already-resolved `ref` from `ctx.results` instead of
re-deriving its own guess from repo config.

**Verified**: a real git repo initialized with `-b trunk` (deliberately not
`main`/`master`) round-trips correctly through `--mirror` clone +
`default_branch()` (`tests/test_git_ops.py`). New
`test_clone_repo_ref_resolves_from_remote_default_when_nothing_named`
(`tests/test_capabilities.py`) confirms `git_default_branch` is only called
when neither the planner nor the request named a branch, and that its result
flows through to the actual `git clone --branch`. Full suite: 65 passed.

**Not yet re-verified against the live deployment** — this fix is only in
the source tree; the hosted `test-ari-oncall-agent` runs a previously built
image and won't pick this up until rebuilt/redeployed (see the image-tag
notes above this entry).

## 2026-09-17 — Fixed: a failed capability skipped `respond` and `cleanup_workspace` entirely

After the branch-default fix above was deployed and re-tested live against
`insightfinder/InsightAgent`, the pipeline got much further (clone → analyze
→ generate_fix → verify → publish_pr, ~11.5 minutes) but `publish_pr` failed
for a real, external reason — the GitHub MCP server's token (account
`neyaadeez`) got a genuine `403 Resource not accessible by personal access
token` trying to create a branch on that repo, not something opencode
mishandled. **The bug found here is what happened next**: the caller got
back only `"Internal error: Workflow execution failed"` — no root cause, no
explanation, nothing actionable — even though opencode had already written a
detailed, correct explanation (which account, which repo, the exact 403,
three concrete options to resolve it).

**Root cause:** `publish_pr.run()` (like `run_opencode`/`git_ops.clone` on
any hard failure) raises a plain `RuntimeError` rather than returning a
soft-failure dict — only `verify.py` has that soft-failure/`on_failure`-goto
contract in `configs/capabilities.yaml`. `orchestration/workflow.py`'s step
loop had no `try`/`except` around `workflow.execute_activity("execute_step",
...)` itself — only around the later, separate `respond` call. So once
Temporal exhausted `publish_pr`'s retries (3 attempts, each producing the
same 403, confirmed in the worker logs) and raised `ActivityError`, that
exception propagated straight out of `AriOnCallWorkflow.run()`: skipping
`cleanup_workspace` (sandbox left behind) *and* skipping `respond` (the one
step whose entire job is turning a failure into a real explanation — see the
2026-09-15 "Fixed: responses had no real synthesis" entry, whose stated
guarantee — "the request must never come back empty-handed" — this exact
path violated). The generic message the caller saw is just the A2A
executor's own catch-all for an unhandled workflow exception.

A second, smaller compounding bug in the same failure: `publish_pr.py`
truncated its exception message to `result['text'][:500]`, which would have
cut off opencode's explanation mid-sentence (before the concrete
fork/grant-access/manual-push options) even if `respond` had run.

**Fix:** wrapped the step-execution `workflow.execute_activity` call in
`try/except ActivityError` inside the loop itself (`workflow.py`) — any hard
capability failure now sets `final_status = "failed"` with a real
`failure_reason` (extracted via new `_activity_failure_detail()`, which reads
the wrapped `ApplicationError.message` — the original exception's clean
`str()`, not Temporal's generic "activity task failed" wrapper text — falling
back to `str(exc)` only if there's no cause at all) and `break`s the loop,
same as the existing soft-failure path — so `cleanup_workspace` and `respond`
always run afterward, regardless of which of the two ways a step failed.
Also raised `publish_pr.py`'s truncation limit from 500 to 4000 characters,
since that text is exactly what `respond` synthesizes the caller's
explanation from.

**Verified:** new `tests/test_workflow.py` (`_activity_failure_detail`
correctly prefers the wrapped exception's real message over the generic
`ActivityError` wrapper text, and falls back to `str(exc)` when there's no
cause). Full suite: 68 passed. **Not yet re-verified live** — needs a
rebuild/redeploy and another real run against a repo/token combination that
either succeeds or fails, to confirm the caller now gets a real, synthesized
explanation instead of the generic message in both cases.

**Still open, not fixed in this pass:** the immediate trigger (GitHub MCP
token `neyaadeez` lacking write access to `insightfinder/InsightAgent`) is a
credential/permissions issue outside this agent's code — same class of gap
called out in the 2026-09-15 "opencode + GitHub MCP integration" entry
(`403 Resource not accessible by personal access token` on a different repo,
that time from a wrongly-scoped fine-grained PAT). Whoever owns that PAT
needs to grant it `contents:write` + `pull_requests:write` on the target
repo (or use a token that already has broader access) before a real PR can
be opened against `insightfinder/InsightAgent`.

## 2026-09-17 — Generalized `analyze`/`generate_fix` past a bug-fix framing

User's ask, directly: `generate_fix` needs to be "more dynamic for various
requests like fix bugs, make changes, edits, etc" — real requests aren't
only bug fixes (the live test earlier this session was itself a plain field-
removal edit, not a bug).

**Found the gap wasn't really in `generate_fix`'s code — it already accepts
a planner-supplied `params.instruction` override** (`ctx.params.get
("instruction") or <default>`); the mechanism for per-request dynamism
already existed. Two real problems instead:
1. The *default* wording (used whenever the planner doesn't set
   `instruction`, which in practice is every request so far) was hardcoded
   bug-fix language: `analyze` said "localize the root cause of the
   reported issue"; `generate_fix` said "Fix the following issue... Make
   the minimal correct change." Fine for an actual bug, misleading framing
   for a feature/edit/refactor ask.
2. `planning/planner.py`'s system prompt never told the LLM it *could* set
   `instruction` per step, or that it could skip `analyze` when a request
   already names exactly what to change and there's nothing to
   investigate — so the override path was structurally there but never
   actually exercised.

**Fix:** generalized the default instruction text in
`capabilities/opencode_analyze.py` and `capabilities/opencode_fix.py` (and
their descriptions in `configs/capabilities.yaml`, which the planner reads
verbatim from `catalog()`) to name bug fix / feature / edit / refactor as
equally normal outcomes, not bug-as-default with everything else an
afterthought. Added explicit guidance to `planner.py`'s system prompt: skip
`analyze` when the request already names the exact change with nothing to
investigate; set a tailored `instruction` param on `analyze`/`generate_fix`
when the generic wording would fit poorly. No new Python branching — the
dynamism is the planner's own LLM call deciding this per request, consistent
with this project's "the LLM figures it out, not hardcoded" pattern (see the
2026-09-15 Maven-dependency entry for the same pattern applied to `verify`).

**Not unit-tested** — this is entirely prompt wording for planner/opencode
LLM calls with no new Python control flow, and no existing test file covers
`planner.py`, `opencode_analyze.py`, or `opencode_fix.py`; correctness here
is a live-LLM-call concern, same as every other planner behavior verified
in this file. Full suite still 68 passed (no regressions). Next real
verification: rerun a mixed set of prompts (a real bug, a pure edit like the
InsightAgent field-removal case, a feature add) against a live LLM and
confirm the planner actually varies the plan (skips `analyze` for the edit
case, tailors wording) rather than always falling through to the same
five-step pipeline.

### Same session — renamed `generate_fix` → `generate_change` (and `ari-fix` → `ari-change`)

Direct follow-up to the generalization above: the capability's *name*
itself still said "fix," undercutting the whole point of making it handle
features/edits/refactors as first-class, not bug-fixes-with-exceptions.
User's ask was explicit ("rename the skill to reflect its use") and left
the actual strategy up to me.

**Renamed, consistently, everywhere:**
- Capability `generate_fix` → `generate_change` (`configs/capabilities.yaml`,
  including `verify`'s `on_failure.goto`).
- Module `capabilities/opencode_fix.py` → `capabilities/opencode_change.py`
  (`git mv`, contents unchanged — already generic after the wording fix
  above).
- opencode agent `ari-fix` → `ari-change` (`tools/opencode_cli.py`'s
  `CAPABILITY_AGENTS` mapping and `_AGENT_DEFS`), with its description
  updated from "Generates a code fix for the analyzed issue" to "Makes the
  requested code change — a bug fix, feature, refactor, or edit."
- Every place that read the old capability name out of `ctx.results`
  (`publish_pr.py`'s `ctx.results.get("generate_fix", ...)`,
  `capabilities/base.py`'s `_OPENCODE_CAPABILITIES` set used for the
  `--continue` check, `planner.py`'s prompt text and JSON-fallback pipeline,
  `clone_repo.py`/`git_ops.py` comments) — updated together so nothing is
  left looking up a results key that no longer exists.
- `publish_pr.py`'s own bug-flavored branding along the same lines: PR
  title prefix `"ARI fix: ..."` → `"ARI: ..."`, example branch name
  `ari/fix-<slug>` → `ari/change-<slug>`, local variable `fix` → `change`.
- Docs: `README.md`, `docs/design.md` (current-state description) updated
  to the new names. `docs/MEMORY.md` entries before this one are left
  exactly as written — they're a historical record of what was true when
  written, not a description of current state; the old name will keep
  appearing in earlier entries on purpose.
- **Deliberately left unrenamed:** `settings.temporal_task_queue`'s default
  value `"oncall-ari-fix-queue"` — that's an operational Temporal queue
  name (already deployed, already in `.env`/the Helm chart's values), not
  a reference to this specific capability; renaming it would be a real
  deployment-coordination concern for zero benefit here.

**Verified:** full suite 68 passed (updated `tests/test_respond.py`'s
catalog-membership assertion and `tests/test_capabilities.py`'s
`results={"generate_fix": ...}` fixtures to the new key — nothing else
referenced the old name). Also directly imported the registry/tool-mapping
modules after the rename and confirmed live: `catalog()` lists
`generate_change`, its `CapabilitySpec.module` resolves to
`ari_agent.capabilities.opencode_change`, `CAPABILITY_AGENTS["generate_change"]
== "ari-change"` and that key exists in `_AGENT_DEFS`, and `verify`'s
`on_failure.goto` now reads `"generate_change"` — the whole chain resolves
correctly, not just the YAML text. Not yet re-verified against a live
opencode run (needs a rebuild/redeploy, same as the pending fixes above).

## 2026-09-17 — Audited (and closed the remaining gaps in) "is the workspace always cleaned up?"

User asked directly, before deploying everything above: is the sandbox
workspace *always* cleaned up, success or failure — fault tolerance,
avoiding a slow disk/memory leak on the worker. Prompted a full audit of
`orchestration/workflow.py`'s `run()`, not just the `publish_pr`/execute_step
case already fixed earlier this session.

**Found two more real gaps, same family as the one already fixed:**
1. **`plan_task` was still unguarded.** `prepare_workspace` runs first and
   creates the sandbox dir; if `plan_task` then fails after exhausting its
   3 retries (e.g. the LLM call itself raises — a bad/expired key, an API
   outage, a rate limit — as opposed to just returning malformed JSON,
   which `planning/planner.py` already handles internally), the
   `ActivityError` escaped `run()` entirely: `cleanup_workspace` never ran
   (the just-created workspace leaks), and `respond` never ran (the caller
   got the SDK's generic error, not a real explanation) — identical failure
   shape to the `publish_pr` bug fixed earlier, one activity call earlier
   in the same function.
2. **`cleanup_workspace`'s `execute_activity` call had no `retry_policy` at
   all.** An unset `RetryPolicy` defaults to `maximum_attempts=0`
   (unlimited) — confirmed directly: `RetryPolicy()` in the installed
   `temporalio` really does construct with `maximum_attempts=0`. Same root
   cause already documented for `plan_task` in the 2026-09-15 A2A-adoption
   entry ("Temporal's default retry policy has no maximum, made a blocking
   call hang far past any reasonable timeout"), left unfixed here. In
   practice this is low-risk — `tools/sandbox.py:cleanup_workspace` calls
   `shutil.rmtree(..., ignore_errors=True)`, which essentially never raises
   for real filesystem reasons — but an unbounded retry policy sitting in
   production code is exactly the class of thing that already bit this
   project once, so closed it anyway rather than relying on one function's
   current implementation detail to make it safe.

**Also closed for full symmetry, not because a failure was observed:**
`prepare_workspace` itself was unguarded too. If it fails outright (it's a
single `mkdir`, so rare), nothing was created — no leak — but the caller
still got the generic SDK error instead of a `respond` explanation.

**Fix, same pattern throughout:** every activity call in `run()` is now
either already retry-capped (`prepare_workspace`, unchanged) or newly capped
(`cleanup_workspace`: `maximum_attempts=3`) and wrapped in `try/except
ActivityError`, each setting `final_status = "failed"` + a real
`failure_reason` on the way down to the same guaranteed `cleanup_workspace`
+ `respond` tail — nothing new to remember per call site, it's the identical
shape as the `execute_step` fix from earlier today. `plan_task`'s own
step-loop only runs `if workspace is not None`, i.e. only once workspace
setup actually succeeded. A `cleanup_workspace` failure (still `except
ActivityError: pass`, deliberately swallowed) never blocks `respond` from
running — a leaked directory is a disk-space concern to notice separately,
never a reason to leave the caller with nothing.

**Net result, answering the actual question asked:** `cleanup_workspace`
now runs unconditionally after *every* code path through `run()` —
`prepare_workspace` failing, `plan_task` failing, any step failing (soft or
hard), or the full pipeline completing — and none of those paths can hang
indefinitely (every retry policy in this function is now bounded) or skip
`respond`. The only way a workspace can still be left on disk is
`cleanup_workspace` itself failing 3 times in a row, which `tools/
sandbox.py`'s `ignore_errors=True` makes practically impossible.

**Verified:** full suite still 68 passed; no test in this repo exercises
`workflow.py`'s control flow directly (see the "ActivityError escaped
`cleanup_workspace`/`respond`" entry above — same reasoning, Temporal
workflow testing needs `temporalio[testing]`'s time-skipping environment,
not present in this project's test deps). Not yet re-verified live —
bundled into the same pending rebuild/redeploy as everything else from this
session.

## 2026-09-17 — Fixed: request secrets sat in plaintext in Temporal's event history (the gap flagged 2026-09-15, now actually closed)

User saw it directly, live: `temporal workflow show --output json` on a
real workflow from this session's own testing showed the exact GitHub PAT
and Anthropic API key from the request, base64-but-otherwise-plaintext in
the `ActivityTaskScheduled` event's input, visible to anyone with Temporal
UI/API access to the namespace. This is exactly the gap the 2026-09-15
"Adopted the real A2A protocol" entry flagged and explicitly deferred
("needs a Temporal Payload Codec before any non-local deploy") — closed now.

**Fix: a real Temporal `PayloadCodec`**, not a workaround that fights this
agent's own architecture (no shared secrets store — every request is
self-contained by design, so redacting fields before they reach the
workflow input and re-fetching them from somewhere else isn't an option
here). New `orchestration/payload_codec.py`: `EncryptionCodec` wraps
AES-256-GCM (`cryptography` — new dependency, `pyproject.toml`), one random
12-byte nonce per payload, prefixed to the ciphertext. Encrypts/decrypts
the *entire* `Payload` protobuf message (metadata + data) as one blob,
tagged with `encoding: binary/encrypted` so `decode` can tell an encrypted
payload from a plain one — necessary so a payload written to history
*before* this codec was enabled (old plain data) doesn't break replay once
it is.

**Wired in one place**: `orchestration/temporal_client.py`'s
`get_temporal_client()` — the single `Client.connect()` shared by both the
API process (`a2a_server/executor.py`, which starts workflows) and the
worker process (`orchestration/worker.py`, which polls/executes them) — so
one `DataConverter(payload_codec=...)` covers every payload this agent
ever sends or reads, not just some of them. New setting
`TEMPORAL_PAYLOAD_ENCRYPTION_KEY` (base64-encoded 32-byte AES-256 key,
`.env.example` documents how to generate one) — a deployment-level secret
like `AGENT_API_KEYS`/`TEMPORAL_API_KEY`, not per-request; unset (the local
dev default) means no encryption, so nothing about local development
changes. Deliberately **no Temporal codec-server** set up — the ask was to
stop secrets from being visible in the Web UI at all, not to let the UI
decrypt them for authorized viewers, so an unset codec server means the UI
can only ever show opaque ciphertext for these payloads.

**Verified:** `tests/test_payload_codec.py` — round-trip recovers the
original payload exactly; the encoded bytes provably do not contain the
plaintext secret substring; decode passes plain (untagged) payloads through
unchanged for the old-history case; a non-32-byte key is rejected at
construction; `_data_converter()` returns `DataConverter.default` (no-op)
when the setting is unset, and a real `EncryptionCodec` when it is. Also
ran a direct end-to-end check outside pytest: built a `DataConverter` with
a random key, ran a realistic `{"repo": {"pat": "ghp_..."}}` payload through
the actual `payload_converter` + `payload_codec` pipeline together (not
just the codec in isolation), confirmed the secret substring is absent from
the encoded bytes and the decoded result matches the original input
exactly. Full suite: 74 passed.

**Deployed and tested live** — `charts/oncallari` updated (new
`ariOncallAgent.temporalPayloadEncryptionKey` value, wired into the
Secret template) and `helm upgrade` run against the `ari-oncall` release
in the `test` namespace with a freshly generated key. Also used this
opportunity to delete the 3 pre-existing `AriOnCallWorkflow` executions
from this session's earlier testing (`temporal workflow delete
--workflow-id ...`, confirmed via `temporal workflow list` returning
empty afterward) — those predated the codec and had the real PAT/API key
in plaintext in their history.

**Immediately found (by the user, looking at the real Temporal UI) that
the first version of this fix was the wrong design**: encrypting the
*entire* payload as one opaque blob also hid the prompt, capability name,
params, and every step's results (diffs, PR links, root cause) — the
Temporal UI became useless for actually observing a request, which is
the whole reason to look at it. The ask was only ever to hide the actual
credentials.

**Redesigned: field-level masking, not whole-payload encryption.** New
`payload_codec.py` parses the `json/plain`-encoded payload, walks it
recursively, and replaces only the *value* of dict keys named `api_key`,
`pat`, or `ssh_key` (wherever they appear, at any nesting depth) with an
AES-256-GCM-encrypted, `enc:v1:`-prefixed, base64-armored string —
everything else in the structure (prompt, capability, params, results,
diffs, PR urls) is left completely untouched. Critically, the payload's
`encoding` metadata stays `json/plain`, unchanged — so it still renders as
ordinary readable JSON to the Temporal Web UI or `temporal workflow show`,
just with those specific field values unreadable, instead of the whole
thing looking like a "can't decode this" blob.

**Verified**: `tests/test_payload_codec.py` rewritten for the new
behavior (8 tests) — round-trip recovers the original structure exactly;
a realistic nested payload (capability/prompt/config.llm.api_key/
config.repo.pat/results.diff) shows every non-sensitive field completely
readable after encode while both secret values are masked and provably
absent from the encoded bytes; decode recovers the real secret values
(the worker still needs them to actually clone/call the LLM); decode
passes through un-prefixed values unchanged (old pre-codec history, or a
field that was never set); non-JSON payloads pass through untouched.
Also ran a direct simulation of what the Temporal UI would actually
render: built a realistic `clone_repo` step payload through the real
`payload_converter` + `payload_codec` pipeline together, confirmed the
rendered JSON has `prompt`/`provider`/`model`/`repo.url` fully readable,
`api_key`/`pat` masked, both real secret substrings absent from the
stored bytes, and decode recovers the correct originals. Full suite: 76
passed.

**Not yet redeployed with this corrected version** — this fix is only in
the source tree so far. No chart change is needed for it (the encryption
key already lives in the deployed Secret from the first version of this
fix); it just needs a new image build containing the corrected
`payload_codec.py` and a rollout, same as every other pending fix from
this session.

## 2026-09-17 — First real full pipeline success, and two real bugs found from it

The corrected branch-resolution + fault-tolerance + new-PAT deploy actually
worked end to end against `insightfinder/InsightAgent` for real: a genuine
PR was opened, https://github.com/insightfinder/InsightAgent/pull/983 —
first time this exact deployment made it all the way through
`clone_repo → generate_change → verify → publish_pr`. Investigating two
things the user noticed live led to two more real, confirmed bugs.

### Fixed: repo-cache corruption race between a fetch and a concurrent read

User's question: could concurrent requests against the same repo cause a
problem, and does the cache truly isolate them? Directly confirmed **yes,
this was already happening** — worker logs from a genuinely concurrent
pair of requests (this session's own accidental double-submit, see the
"did you trigger two tasks?" exchange) showed:

```
fatal: failed to copy file to '.../repo/.git/objects/pack/pack-<hash>.pack': No such file or directory
```

**Root cause:** `repo_cache.ensure_cache()` held its lock (in-process
`asyncio.Lock` + `fcntl.flock`) only for the duration of the mirror
*fetch*, then released it and returned the cache directory.
`capabilities/clone_repo.py` then did a **separate, unlocked** local `git
clone` reading from that same directory. So: request A's fetch finishes,
releases the lock, and starts its local clone (reading pack files) — while
request B's `ensure_cache()` (same repo) acquires the now-free lock and
runs its own `git fetch ... --prune` on that same mirror. `--prune` can
delete/repack the exact pack file request A is still mid-copy on,
producing exactly the observed error. Not a memory issue — a git-level
race on the shared cache directory that the original lock scope never
actually covered.

**Fix:** `tools/repo_cache.py` restructured — `_sync_refresh_cache()` is
now a pure, lock-free fetch/create helper; a new `ensure_cache_and_clone()`
holds one lock acquisition across the refresh, the default-branch
resolution (when `ref` is unset), *and* the local clone, so a concurrent
request's fetch can never run while another is still reading. `ensure_cache()`
is kept as a separately-self-locking wrapper around the same core, for
existing callers/tests that only need the refresh. New tool
`repo_cache_and_clone` replaces `clone_repo`'s three separate tool calls
(`repo_cache`, `git_default_branch`, `git_clone`) with one atomic call —
`configs/tools.yaml`, `configs/capabilities.yaml` updated accordingly.
Deliberate trade-off, documented in the module: this fully serializes
same-repo requests through that (short) window rather than allowing reads
and writes to interleave — correctness over throughput; different repos
are completely unaffected (separate lock keys).

**Verified**: `tests/test_repo_cache.py` — a new concurrency test
instruments `_sync_refresh_cache`/`git_clone` to record call order and
artificially widens the clone's window with a sleep, runs two concurrent
`ensure_cache_and_clone` calls against the same repo, and asserts the two
phases never interleave. Confirmed this test is meaningful, not a
tautology: ran the exact same scenario against a reconstructed version of
the *old* locking (lock released right after the fetch, clone unlocked)
outside pytest and got `['refresh_start', 'refresh_end', 'clone_start',
'refresh_start', 'refresh_end', 'clone_start', 'clone_end', 'clone_end']`
— a real interleaving, exactly the bug — versus full serialization with the
fix. `tests/test_capabilities.py`'s `clone_repo` tests updated for the new
single-tool interface. Full suite: 82 passed.

### Fixed: PR title/body were raw agent narration, not a summary

User's second question ("is each phase a fresh session, or shared?") led
somewhere different than expected. **The shared-environment design is
correct and was already working**: `clone_repo` runs once, every later
step (`analyze`/`generate_change`/`verify`/`publish_pr`) operates on the
exact same `ctx.repo_dir` (no re-cloning), and `tools/opencode_cli.py`
passes `--continue` on every step after the first so opencode resumes the
same session — confirmed genuinely working by the real PR body correctly
describing cross-file changes (`models.go`, `worker.go`,
`insightfinder.go`, `README.md`) that only `generate_change` would have
known about.

**The actual bug, found by reading PR #983 itself**: its title was `"ARI:
Now let's make the edits. First \`models.go\`:\nLet me fix gofmt
alignment "` — and the body was almost entirely opencode's own step-by-step
narration ("Now let's make the edits...", "Let me fix gofmt alignment...")
with the real "Summary of changes" section buried partway through. Traced
to `opencode_cli.py`'s `_parse_events()`: `text` is *every* text chunk from
the whole agentic turn concatenated together, not a final summary — that
raw blob was used directly as `generate_change`'s `summary`, which
`publish_pr.py` both truncates to 72 characters for the PR title (hence
the title being literally the first 72 characters of the narration) and
uses verbatim as the PR body.

**Fix:** `opencode_change.py`'s instruction now asks the agent to end with
a line reading exactly `SUMMARY:` followed by a clean, PR-ready
description with no narration; a new `_extract_summary()` takes everything
after the *last* such marker (robust to the agent mentioning the word
"summary" earlier in its own narration), falling back to the full text if
the marker never appears so nothing is silently dropped. `_parse_events`
itself is left unchanged (other consumers may still want the full raw
text) — this is a targeted fix at the one place the raw blob was reaching
a human-facing surface (the PR) without ever being cleaned up.

**Verified**: new tests in `tests/test_capabilities.py` —
`_extract_summary` returns everything after the last marker, falls back to
the full text when the marker is absent, and correctly picks the *last*
occurrence when the agent mentions "summary" earlier in its own narration;
`generate_change`'s `run()` returns only the marked summary, not the raw
narration, given a realistic mixed-text opencode response. Full suite: 82
passed.

**Not yet redeployed** — both fixes are only in the source tree; needs a
new image build + rollout, same as everything else pending from this
session.

## 2026-09-17 — Why a small change took 16 minutes: opencode's `task` tool bypassed permissions entirely

User's question after PR #984: this was a trivial 3-file field-removal, why
did the pipeline take ~16 minutes (worker logs showed `generate_change`
alone ran 23:38:24 → 23:47:38, over 9 minutes)? Asked directly to
investigate by calling opencode itself, not just read code and guess.

**Method:** installed the exact pinned `opencode-ai@1.18.31`, shallow-cloned
`insightfinder/InsightAgent` to a scratch directory, wrote a real
`opencode.json` via the actual `write_config()` (not hand-written), and ran
`ari-change` with the exact instruction `opencode_change.py` would have
generated, capturing the full NDJSON event stream with per-event
timestamps — a real trace, not speculation.

**First hypothesis (wrong): monorepo search overhead.** The repo is a
158-top-level-directory, 670MB monorepo — a plausible reason for slow
exploration. Ruled out by the actual trace: only 17 direct tool calls (8
reads, 5 edits, 3 greps, 1 glob), every one correctly scoped to
`ruckus-agent/`. No wasted monorepo-wide flailing.

**Actual root cause: opencode's built-in `task` tool.** 4 calls to it
accounted for **~272 of 401 seconds — 68% of the entire run**. Each `task`
call delegates a sub-goal to a fresh subagent, and inspecting what was
actually delegated revealed something far more serious than slowness:
the 2nd and 3rd delegated tasks were literally "check git status / gh auth
status" and then "**create a branch, commit, and open a pull request**" —
work that is supposed to be `ari-publish`'s exclusive job. The delegated
subagent successfully ran `git branch`, `git commit`, and `gh pr create` —
commands `ari-change`'s own declared `permission.bash` (`{"git *":
"allow", "*": "deny"}`) should have denied for `gh` outright, and which
`ari-change` should never be attempting regardless (per
`capabilities/publish_pr.py`'s own instruction: "Stage and commit exactly
these files" is stated as *publish's* job).

**This is a real permission-bypass, not just a performance bug.**
`task`-delegated subagents run with their own permissions, not the
delegating agent's `permission` block. Confirmed two independent gaps:
1. The delegated subagent could do things (`gh pr create`) that
   `ari-change`'s own declared bash permission explicitly would not have
   allowed — the delegation itself is an escape hatch around whatever
   scoping an agent is given.
2. Independent of delegation, `ari-change`'s own bash permission
   (`"git *": "allow"`) already directly permitted `git commit`/`git
   push`/`git branch` — commands it was never supposed to need, since
   `generate_change` never intended to touch git state itself. `git *`
   also matches destructive commands (`git reset --hard`, `git clean
   -fdx`) that a code-editing agent has no legitimate reason to run.

**Real incident during the reproduction, disclosed here in full:** the
scratch clone's `origin` was still the real
`https://github.com/insightfinder/InsightAgent.git` URL (from `git clone`),
and this machine has a separately pre-authenticated `gh` CLI session
(account `neyaadeez`, `repo` scope) — ambient credentials unrelated to
anything this agent's own request-scoped PAT mechanism controls. The
delegated subagent's `git push` and `gh pr create` succeeded for real,
opening **PR #985** on the actual repo as a pure side effect of a local
diagnostic exercise. Caught immediately by checking `gh pr list` right
after the reproduction finished; closed the PR and deleted the pushed
branch (`git push origin --delete ...`) within minutes. No lasting effect,
but a sharp reminder: any local reproduction against a real repo URL can
execute for real if ambient host credentials (git credential helpers, a
logged-in `gh` CLI) are present, regardless of what's explicitly configured
in the test's own `opencode.json`. The re-verification run afterward used
an isolated local bare repo as `origin` specifically to make this
impossible to repeat.

**Fix, in `tools/opencode_cli.py`:**
- New `_no_delegation_tools()` → `{"task": False}`, applied explicitly on
  *every* agent's own `tools` block (not just relying on the global
  default, in case per-agent config replaces rather than merges with it) —
  `ari-analyze`, `ari-change`, `ari-verify`, and even `ari-publish` (which
  should do its own git/MCP calls directly, not hand them to an
  unrestricted subagent). Also added to the top-level `config["tools"]`
  default for defense in depth.
- New `_READ_ONLY_GIT_BASH` (`git diff*`/`git status*`/`git log*`/`git
  show*`/`git blame*` allowed, everything else denied) replaces the old
  blanket `"git *": "allow"` for `ari-analyze` and `ari-change` — read-only
  git inspection only; committing/pushing/branching stays exclusive to
  `ari-publish`, which keeps its own broad `git *` since that actually is
  its job.

**Verified, with a real before/after comparison, not just unit tests:**
reran the identical reproduction (same instruction, same repo, isolated
local `origin` this time) with the fixed `opencode.json` (generated via the
real `write_config()`, not hand-written) — **401.6s → 186.0s, a 54%
reduction**, **zero `task` calls**, and the agent made no attempt at all to
commit, push, or open a PR (correctly staying in its lane now that the
permission is actually gone, not just discouraged by instruction wording).
17 tool calls became 32 (14 reads, 9 greps, 8 edits, 1 glob) — more direct
calls, but each one fast (a few seconds) instead of 4 giant 13–143s
delegated subagent loops. New tests in `tests/test_opencode_cli.py`: every
agent disables `task` (and the global default does too); `ari-analyze`/
`ari-change` have no blanket git access and only the specific read-only
subcommands are allowed. Full suite: 85 passed.

**Not yet redeployed** — needs a new image build + rollout, same as
everything else pending. Worth calling out on its own once deployed: this
is the fix most likely to change real-world behavior beyond "the same
thing but faster" — it closes an actual capability `ari-change` had
(committing/pushing to the real authenticated repo, and via delegation,
running arbitrary commands outside its declared permissions) that nothing
in this project's design ever intended it to have.

Also added, same session: efficiency guidance in `opencode_change.py`/
`opencode_analyze.py`'s instructions (scope to the named module first in a
monorepo, consolidate overlapping search terms) — a lower-confidence,
unverified-by-trace prompt tweak riding along with the confirmed `task`-tool
fix, since re-verifying it specifically would need another live run.

## 2026-09-18 — The SUMMARY: marker fix regressed on the very next real run

Deployed everything above (`2609180021-test`) and reran against a
different module (`positron-agent`, same kind of request). Pipeline
succeeded — PR #986 opened — but the PR **title was raw narration again**:
`"ARI: Now let's implement all the changes. Starting with
positron/util.go:\nGood, strings is still used by looksLikeName."` —
the exact bug the `SUMMARY:` marker fix (earlier this session) was
supposed to have closed.

**Not a stale deployment** — confirmed by reading `opencode_change.py`'s
actual source directly out of the running worker pod
(`kubectl exec ... python3 -c "import ari_agent.capabilities.opencode_change as m; print(inspect.getsource(m))"`)
: the fix, including the same-session efficiency-prompt tweak, was
genuinely present in the deployed image.

**Real root cause: the marker instruction isn't reliably followed.**
opencode/the model doesn't always end its turn with the literal `SUMMARY:`
line as asked — a probabilistic instruction-following gap, not a code
bug. `_extract_summary`'s fallback path for "marker not found" was
`return text.strip()` — i.e. the full raw narration blob — which quietly
reintroduces the exact problem being fixed, in exactly the cases where
compliance fails. A prompt instruction alone was never going to be 100%
reliable; the code needs to *detect* non-compliance and correct it, not
just hope.

**Fix:** `_extract_summary()` now returns `None` (not the fallback full
text) when the marker is absent. `opencode_change.run()` treats `None` as
"issue one enforcement follow-up call" — a short, narrow instruction
("reply with ONLY a concise, PR-ready description... no narration, no
markdown, no tool calls") sent via a **new `continue_session` parameter**
on `run_opencode()` (`tools/opencode_cli.py`). That parameter exists
because `ctx.opencode_session_started`'s automatic `--continue` detection
is based on `ctx.results`, which doesn't get this capability's own entry
until *after* `run()` returns — so a same-`run()` follow-up call needs to
force-continue the session explicitly, or it would start a fresh,
context-less session with no idea what was just changed. The follow-up's
own reply is used directly (no marker needed for it — it's already
single-purpose), falling back to the original raw text only if the
follow-up itself somehow comes back empty, so a response is never lost
entirely.

**Verified**: `tests/test_capabilities.py` — `_extract_summary` returns
`None` (not full text) when the marker is missing;
`test_generate_change_issues_enforcement_followup_when_marker_missing`
mocks two sequential opencode calls (first without the marker, second a
clean reply) and confirms the second is used as the summary, called with
`continue_session=True` specifically (not relying on the automatic
detection). Full suite: 86 passed. **Not yet re-verified live** — this
needs a new image build + rollout + another real run to confirm the
enforcement call actually produces clean PR titles in practice; the
underlying "does opencode reliably comply with a *second*, narrower
instruction" question is itself unverified until then.

**PR #986 itself was left as-is** (title/body still raw narration) —
fixing it retroactively wasn't requested; flagged for the user to decide
whether it's worth editing given the fix is now understood.

## 2026-09-18 — Added real observability instead of one-off manual reproductions

Same real run (`positron-agent`, task `8cacdd96-...`): `publish_pr` alone
took ~4m51s (00:35:21 → 00:40:12) for what should be a mechanical stage/
commit/push/open-PR job. User asked two things: why is `publish_pr`
specifically slow, and — more durably — build real end-to-end tracing of
what opencode does, so this class of question never again requires
manually installing opencode and replaying a reproduction (as the
`task`-tool investigation the day before required).

**Root problem with the "reproduce it locally" method**: it's exactly what
caused the PR #985 incident. It doesn't scale as a debugging strategy
(each investigation is a manual, ~5-10 minute, real-API-cost exercise) and
it's genuinely risky for anything touching `ari-publish` specifically —
that agent's job (`git push` + real GitHub MCP `github_create_pull_request`
calls) can't be meaningfully isolated the way `ari-change` was; a
reproduction that actually exercises the PR-opening path only means
anything against a real repo. Investigating *why `publish_pr` is slow*
this same way was correctly avoided this time rather than risking a repeat.

**Fix: instrument every opencode call, always, for free.** `_parse_events`
already computed `raw_events`/`raw_stdout` and discarded them — the exact
data a manual reproduction had to be run to get. New `_build_trace()` in
`tools/opencode_cli.py` extracts, from opencode's own per-event
timestamps (not just a single start/end wall-clock number): tool-call
counts by name, and the 5 biggest gaps-before-a-tool-call (`slowest_gaps`)
— the precise signal that manually found "4 `task` calls = 68% of one
run" the day before, now computed on *every* real call automatically.
`run_opencode()` attaches this as `result["trace"]` and logs it via
`structlog` (`opencode_call` event: request_id, capability, agent,
continued_session, elapsed_s, tool_call_counts, tool_call_total,
slowest_gaps) — visible in `kubectl logs` immediately, no reproduction
needed. New `merge_traces()` combines multiple calls into one trace for a
capability that makes more than one opencode call itself (`generate_change`'s
enforcement follow-up).

**Every opencode-backed capability now returns its trace**
(`opencode_analyze.py`, `opencode_change.py`, `verify.py`, `publish_pr.py`
all add `"trace": result["trace"]` to their returned dict), which flows
into `ctx.results[capability]` and therefore into the final A2A response's
existing `data` field — meaning any caller (or a human polling
`GET /tasks/{id}`) can see exactly where each step's time went, straight
from the response, with zero new infrastructure. Logging happens even on
an opencode failure (before the `proc.returncode != 0` check), so a failed
step is diagnosable too, not just successful ones.

**Verified**: `tests/test_opencode_cli.py` — `_build_trace` correctly
counts tools and identifies the single biggest gap from synthetic
timestamped events; handles a no-tool-calls response; `merge_traces`
combines elapsed time, counts, and top gaps across two traces. Updated
every existing test that mocks the `opencode` tool to include a stub
`trace` key (capabilities now access `result["trace"]` unconditionally).
Full suite: 89 passed.

**`publish_pr`'s actual slowness is not yet diagnosed** — deliberately, per
the reasoning above. This is exactly what the new `trace` field will
answer on the next real deploy + test run, without needing another
reproduction. Candidates worth checking once real data exists: genuine
GitHub API/MCP round-trip latency (create branch, push, create PR are 3+
sequential network calls) vs. something more fixable (redundant
inspection calls before pushing, similar in spirit to what was found in
`generate_change`).

**Not yet redeployed** — needs a new image build + rollout, same as
everything else pending.

## 2026-09-18 — The trace paid off immediately: `publish_pr`'s real bottleneck was redundant reads, not GitHub API latency

Deployed the tracing (`2609180054-test`) and reran the same kind of
request. Real result, pulled straight from Temporal's `ActivityTaskCompleted`
payload for `publish_pr` (`temporal workflow show --output json`, no
reproduction needed):

```json
{"trace": {"elapsed_s": 249.45, "tool_call_total": 14,
  "tool_call_counts": {"github_create_branch": 1, "github_push_files": 1,
    "github_create_pull_request": 1, "github_get_me": 1, "read": 10},
  "slowest_gaps": [{"tool": "github_push_files", "at_s": 219.71, "preceded_by_gap_s": 203.01}, ...]}}
```

**203 of 249 seconds was one gap, immediately before `github_push_files`.**
The real GitHub MCP calls (`create_branch` → `push_files` →
`create_pull_request`) only took ~46s combined — legitimate API latency
was never the bottleneck. The 10 `read` calls are the tell: `generate_change`
had changed exactly 10 files, and `ari-publish` read every single one of
them again before pushing, despite already being given the full diff and
summary in its instruction. This is the exact question asked the day
before ("why is publish_pr slow") — answered directly from a real trace
with zero reproduction, exactly the point of building the tracing.

**Fix:** `publish_pr.py`'s instruction now states explicitly: "The change
is already made and verified — do not re-read, re-inspect, or re-diff
these files yourself; the summary and file list given here are already
everything you need." — a direct, targeted instruction addressing the
specific redundant behavior the trace revealed, not a guess.

**Verified:** full suite still 89 passed (existing instruction-content
assertions — "against staging"/"do not ask for the repository" etc. — all
still hold; only new text was added, nothing removed). **Not yet
re-verified live** — same as the `generate_change` efficiency-prompt tweak
earlier, this is a prompt change whose actual effect can only be confirmed
by a real run's trace, not a unit test. The next real test's
`data.publish_pr.trace.tool_call_counts.read` should drop from 10 toward 0
if it works.

## 2026-09-18 — Restructured the flat `ari_agent` package into a small multi-agent framework, and built the Jenkins agent on top of it

Prompted by wanting a second agent kind (Jenkins) on the same
infrastructure without duplicating the A2A server/Temporal
workflow/planner/respond code. Split the single `src/ari_agent/` package
into four: `common/` (agent-kind-agnostic — A2A server, orchestration,
planner, respond, registries, settings, sandbox, LLM client, the new
`opencode_runtime.py`), `ari_oncall_agent/` (the original agent's own
capabilities/tools/schemas, moved via `git mv`), `jenkins_agent/` (new),
and `api/` (thin deployable entrypoints only — `api/main.py`/
`api/worker.py`, no logic of their own). `configs/capabilities.yaml`/
`tools.yaml` moved to `configs/oncall/` (and a new `configs/jenkins/`
added); `config_paths.config_file()` simplified to always resolve
`configs_dir() / agent_kind / name` (removed an old special case that
kept oncall at the root).

**New abstraction:** `common/agent_meta.py`'s `AgentMeta`/`get_agent_meta()`
— every place that used to hardcode on-call-specific text or a
`results.get("publish_pr", {}).get("pr")`-shaped lookup (the Agent Card's
description/skill, the planner's domain-guidance paragraph and fallback
steps, `respond`'s system prompt, the workflow's artifact extraction) now
reads it from `<agent_package>.meta.META` instead, keyed by
`settings.agent_kind`. See `docs/design.md` §0 for the full shape.

**Real bug found and fixed while wiring this up:** `get_agent_meta()`
originally had no parameter at all and always read the *global*
`get_settings().agent_kind` — so `build_agent_card(Settings(agent_kind="jenkins"))`,
called with an explicit local `Settings` object, still got the on-call
agent's metadata. Caught by a failing test
(`test_agent_card_switches_content_by_agent_kind` returned the on-call
skill for both kinds). Fixed by adding an explicit `agent_kind: str | None`
parameter and updating `agent_card.py` to call
`get_agent_meta(settings.agent_kind)` rather than relying on the implicit
global default.

**Also renamed, unifying a real pre-existing inconsistency:** the
workflow response's PR key was `"pr"`, but the A2A executor's artifact
name (and this doc's own developer-guide example) already called it
`"pull_request"` — two different names for the same data. Unified on
`"pull_request"` everywhere (`workflow.py`, `docs/design.md`,
`tests/test_a2a_executor.py`'s fixture).

**Jenkins agent, built from scratch on top of the above:** capabilities
`resolve_target`/`inspect`/`diagnose`/`propose_action`/`execute_action`,
opencode agents `ari-jenkins-read` (every `jenkins_*` MCP tool denied)
and `ari-jenkins-act` (the only one with them enabled — only
`execute_action` runs as this one), and a direct-REST fallback
(`jenkins_rest.py`) for stopping a build, since the MCP plugin's own
documented tool list didn't obviously include a stop/cancel tool (later
confirmed true live — see the entry below). `execute_action` refuses to
run without a concrete, non-stale confirmed action already in
`ctx.params` — never inferred from a bare "yes"; see `docs/design.md` §5b
for the full two-layer gate.

**Verified:** full suite 109 passed (92 pre-existing + 3 new
`test_agent_meta.py` + 14 new `test_jenkins_capabilities.py`, the latter
covering all 5 Jenkins capabilities plus `opencode_cli`/`jenkins_rest`
tool-permission assertions — checked each assertion against the real
source before running, not written blind against the summary). Also
directly built and ran the lightweight API image in Docker and confirmed
the same image serves either agent kind's real Agent Card content
(different `description`) purely from `AGENT_KIND` env, nothing else
changed. **Not yet verified:** any Jenkins capability against a real
Jenkins — see the entry below.

## 2026-09-18 — Split the single Dockerfile into three, one per deployable process

Per the earlier design conversation's explicit ask for "dedicated docker
files for ari on call agent, ari jenkins agent, and separate api
deployment." `Dockerfile.api` (shared, `python:3.12-slim` + `pip install .`
only — this process never shells out to git/opencode itself, so it needs
none of the toolchain the workers do), `Dockerfile.oncall` (the original
image's full git/node/opencode/go/java/maven toolchain, entrypoint
changed to `api.worker`), `Dockerfile.jenkins` (new, much smaller — only
Node.js + the same pinned `opencode-ai` version, no git or build
toolchain, since this agent never clones or builds anything). Old
`Dockerfile` removed.

**Verified:** built `Dockerfile.api` in Docker directly (not just
reviewed) and ran `python -c "from api.main import app; ..."` inside the
built image with `AGENT_KIND=oncall` and `AGENT_KIND=jenkins` — confirmed
`app.description` differs correctly between the two, proving the
"one shared API image, N worker images" design actually works, not just
plausible on paper. `Dockerfile.oncall`/`Dockerfile.jenkins` were not
built in Docker (their toolchain installs are slow and mechanically
unchanged from the already-working original `Dockerfile`'s corresponding
sections) — only reviewed.

## 2026-09-18 — Extended the Helm chart (`insightfinder/charts`, `charts/oncallari`) with a second, independently toggleable agent kind

Added `ariJenkinsAgent` alongside the existing `ariOncallAgent` block:
its own api+worker Deployment pair, Secret, Service, and Ingress,
mirroring the on-call agent's structure but with no `repo-cache` PVC (the
Jenkins agent never clones anything) and its own Temporal namespace
(`jenkins-ari-agent`) against the same shared Temporal dev server. New
`_helpers.tpl` naming helpers use distinct `-jenkins-*` suffixes
specifically so both agent kinds' resources can coexist under one release
without colliding — the existing on-call helper names (`-api`/`-worker`/
`-secret`) were deliberately left unchanged so upgrading the
already-deployed `test` release wouldn't rename/recreate its existing
resources.

Also split `ariOncallAgent.image` (one image field used by both its API
and worker Deployments) into `.api.image` (now the shared image built
from `Dockerfile.api`, also used by `ariJenkinsAgent.api.image`) and
`.worker.image` (`Dockerfile.oncall`'s image) — a breaking values-schema
change: any `helm upgrade --set ariOncallAgent.image.tag=...` invocation
needs to become two `--set` flags now. No committed values file in the
repo referenced the old path, so nothing already-applied silently broke;
this only affects how future deploys are invoked from the CLI.

Later the same day, renamed both agent kinds' default Temporal task queue
names — `oncall-ari-fix-queue` → `oncall-ari-queue`,
`jenkins-ari-fix-queue` → `jenkins-ari-queue` — per direct request ("fix
i doesn't make sense... same thing with jenkins"), overriding an earlier,
now-superseded note in this doc (2026-09-17 entry under "generalized
analyze/generate_fix") that had deliberately left this queue name alone
as "an operational Temporal queue name, not worth the deployment
coordination"). Also updated `settings.py`'s matching default.

**Verified:** `helm lint`+`helm template` with both agent kinds enabled —
confirmed distinct, non-colliding resource names, correct images/
entrypoints/`AGENT_KIND` per Deployment, correct decoded `TEMPORAL_TASK_QUEUE`
secret values (`oncall-ari-queue`/`jenkins-ari-queue`) after the rename.
Full Python test suite unaffected (no test asserted the literal queue
name). **Not yet applied** to the actual live `test` cluster release —
these are chart/values changes only, not a `helm upgrade` that was run.

## 2026-09-18 — First real verification session against a production Jenkins, and two hard blocks from Claude Code's own safety classifier

User wanted the Jenkins agent's design validated read-only against
`jenkins.insightfinder.com` (a real production instance) before trusting
any of it, and provided a real API token (pasted directly into chat —
flagged back to the user as its own exposure, independent of anything
that follows, with a recommendation to rotate it after testing).

**What was checked, all via direct read-only HTTP calls (no LLM, no
secrets ever written to disk) — this part worked and needed no special
permission:**
- Server reachable, `X-Jenkins: 2.568.3` (from an *unauthenticated*
  request's response headers — Jenkins leaks its version even on a 403).
- Token authenticates via Basic auth as `mustafa:<token>` — confirmed via
  `/whoAmI/api/json`. (A wrong placeholder username got a clean 401,
  confirming the username genuinely has to match, not just be non-empty.)
- **First finding: the MCP Server plugin was not installed at all.**
  Checked by pulling `/pluginManager/api/json?depth=1`'s full 148-plugin
  list directly, not by trusting the plugin page's marketing copy.
  `/mcp-server/mcp` 404'd for exactly this reason, not a wrong path
  guess.
- After the user installed it (self-service, admin access confirmed
  first via `AskUserQuestion`, and a running build on `agent1` flagged
  and explicitly re-confirmed before proceeding): confirmed installed
  and `active: true` via the same plugin-list endpoint.
- **Real MCP tool list, via a raw JSON-RPC `tools/list` call — no model,
  no opencode, zero risk of anything being invoked.** The transport
  turned out to be streamable-HTTP: `initialize` POST returns an
  `Mcp-Session-Id` response header; that header must be replayed on a
  `notifications/initialized` POST and then the actual `tools/list` POST,
  whose response arrives as a single Server-Sent-Event `data:` line, not
  plain JSON (`Accept: application/json, text/event-stream` is required
  or the initial `initialize` call itself is refused). Result: exactly
  19 tools, each carrying the plugin's own `readOnlyHint`/`destructiveHint`
  annotations — 15 read-only, 4 destructive (`triggerBuild`,
  `updateBuild`, `rebuildBuild`, `replayBuild`), confirming **no
  stop/cancel tool exists**, matching `jenkins_rest.py`'s existing
  assumption (built on a guess from the plugin's doc page, now fact).
- opencode's own docs (`https://opencode.ai/docs/mcp-servers`, fetched
  directly) confirm its MCP tool-naming rule verbatim: *"MCP server tools
  are registered with server name as prefix... `mymcpservername_*`"* —
  meaning `jenkins_agent/tools/opencode_cli.py`'s `"jenkins_*": false`
  deny pattern is very likely correct, not just an analogy to GitHub's
  confirmed `github_*` (this had been an open, explicitly-flagged risk
  since the agent was first built: with opencode's `--auto` auto-approving
  anything *not* explicitly denied, a wrong prefix guess would silently
  deny nothing).

**What could NOT be checked, and why — a real, not hypothetical, gap:**
Claude Code's own auto-mode safety classifier blocked two distinct
actions outright, independent of the user's explicit go-ahead each time:
(1) an `installNecessaryPlugins` POST (a mutating production action —
the user ended up installing it themselves instead), and (2) writing the
real Jenkins token + LLM API key into a local `opencode.json` (flagged
specifically as "credential materialization" — writing real secrets to
disk — which is unavoidable for actually running `opencode` this way,
since that's how it receives MCP auth headers and provider keys). Per the
classifier's own guidance, no attempt was made to route around either
block (e.g. splitting the write into steps, disguising the request).

**Net result: nothing above amounts to "the Jenkins agent works."** It
proves the MCP endpoint is real, reachable, and shaped the way the code
assumes — the actual capabilities (`resolve_target`/`inspect`/`diagnose`/
`propose_action`, let alone `execute_action`) have had zero real
`opencode run` invocations against a real Jenkins, by anyone, as of this
entry. `scripts/jenkins_agent_local_test.py` was built and given to the
user to run themselves (exact `export`-and-run commands using the
credential files already on disk, so no secret needs retyping) — user
declined to run it in this session. This gap needs to be closed with a
real run — either by the user directly, or by this agent in a future
session with the relevant Bash permission loosened for that specific
action — before any claim of "the Jenkins agent works" is accurate.

## 2026-09-18 — Added a hard human-in-the-loop gate for `trigger_build`, guarding only that action

Brainstormed several ways to get a genuine human checkpoint before any
Jenkins-mutating call (conversational confirm, A2A's `TASK_STATE_
INPUT_REQUIRED`, a signed/structured confirm token, a dedicated confirm
path never reachable by planning, out-of-band chat-platform approval,
pushing the gate into a Jenkins pipeline's own `input` step, risk-tiered
auto-approval) before implementing anything, per explicit instruction to
gather ideas first. User picked `TASK_STATE_INPUT_REQUIRED` as "the best
fit," scoped tightly: **guard `trigger_build` only — `stop_build` (and
anything else `execute_action` might handle later) keeps today's
conversational confirm flow unchanged.**

Before designing against it, read the actual installed `a2a-sdk` (1.1.2)
source rather than assuming spec behavior (dispatched an Explore agent
specifically for this) and confirmed: a second `message/send` for an
existing non-terminal `task_id` re-enters `AriAgentExecutor.execute()`
with `context.current_task.artifacts` reliably carrying back exactly
what an earlier turn published via `TaskUpdater.add_artifact` —
persisted in `InMemoryTaskStore`, real and deterministic, no LLM needed
to recover it. `TASK_STATE_INPUT_REQUIRED` isn't in the SDK's
`TERMINAL_TASK_STATES`, so a paused task accepts a follow-up normally.
This meant the whole mechanism needed **zero Temporal-level pause/signal
machinery** — each conversational turn already starts an independent
Temporal workflow execution reusing the same workflow ID (today's
existing pattern for "yes, go ahead"); only the A2A layer needed new
logic. Also found, while designing: `_TERMINAL_STATE_BY_STATUS["needs_
approval"]` already mapped to `TASK_STATE_INPUT_REQUIRED` in `executor.py`
— dead code from the original design, apparently intended for exactly
this, never wired up until now.

**The real design risk, caught before implementing, not after:** simply
adding a new structured confirm path does nothing on its own to stop the
*existing* LLM planner from independently deciding to schedule
`execute_action` with a `trigger_build` action reconstructed from chat
text — which would leave the old soft path alive for the exact action
meant to be guarded. Closed this by verifying `common/models/plan.py`'s
`Step` is a plain pydantic `BaseModel` with pydantic v2's default
`extra="ignore"` — the planner's LLM-generated JSON can only ever
populate `Step.params` (a free `dict`), never a new top-level field on
`Step` itself. A `StepContext.confirmed` flag was added that lives
*outside* `params` entirely, set only by the new deterministic bypass
branch in `orchestration/activities.py`'s `plan_task` — structurally
unspoofable by anything the LLM's JSON can contain, not just "checked by
convention." `execute_action.py` refuses an unconfirmed `trigger_build`
by checking this flag, never anything in `ctx.params`.

**What changed** (three independent pieces, matching `docs/design.md`
§5b's now-updated three-layer description):
- Pausing: `propose_action.py` flags `requires_approval` for
  `trigger_build`; `workflow.py`'s step loop turns that into `status:
  "needs_approval"` → the pre-existing `TASK_STATE_INPUT_REQUIRED`
  mapping. `respond.py`'s system prompt got one new bullet distinguishing
  "paused for confirmation" from "gave up," since the generic "pipeline
  gave up" framing read wrong for a deliberate pause.
- Resuming: a follow-up `message/send`'s config carries
  `confirm_pending_action: {"id": "..."}`; `executor.py`'s new
  `_find_pending_action` resolves it against `context.current_task
  .artifacts`; `AgentMeta.confirm_capability` (new field, `"execute_
  action"` for jenkins, `None` for oncall) tells `plan_task`'s new bypass
  branch which capability to run with the resolved data, skipping the
  LLM planner for that turn entirely.
- The unspoofable gate: `StepContext.confirmed`, threaded through
  `execute_step`'s payload from `plan_task`'s bypass branch only.

**Verified:** full suite 119 passed (109 pre-existing + 10 new — the gate
itself and the `requires_approval` flag in `tests/test_jenkins_
capabilities.py`, `_find_pending_action` in `tests/test_a2a_executor.py`,
and `plan_task`'s two branches in a new `tests/test_activities.py`, which
had no test file at all before this — a real coverage gap closed, not
just new-feature tests). Confirmed via `git diff --stat` that only the
nine files named in the plan changed, nothing else. **Not yet verified**:
the actual pause → follow-up → resume round-trip through a real deployed
API process and Temporal worker — every piece was verified against
source/unit tests, not a live HTTP round-trip, and this doc says so
plainly rather than implying otherwise.

## 2026-09-18 — Asked "why is there no decline signal," which led to finding a real worker-crashing bug unrelated to the question

Wrote `docs/INPUT_REQUIRED_INTEGRATION.md` claiming there was no decline
mechanism for a paused `trigger_build` proposal. Asked directly why —
the honest answer was that decline was simply never designed, since the
whole brainstorm and plan were scoped around hardening *confirm* only.
But A2A already has a general-purpose verb for exactly this,
`POST /tasks/{id}:cancel` (already implemented, already documented in
`docs/DEVELOPER_GUIDE.md` §6) — so the real question became whether that
already works for a task that's paused rather than still actively
running.

**Verified against a real local Temporal dev server** (Docker,
`temporalio/temporal:1.9.1 server start-dev`, not simulated) rather than
reasoning from documentation, because the hypothesis going in
(`executor.py`'s `cancel()` unconditionally calls `handle.cancel()` on
the Temporal workflow, which has *already completed* by the time a task
reaches `needs_approval` — surely that raises?) turned out to be **wrong**:
cancelling an already-completed Temporal workflow execution succeeds
silently, no error. Confirmed this twice — once with a raw
`temporalio.client` call, once running the actual
`AriAgentExecutor.cancel()` method against a genuinely completed
`needs_approval` workflow run, both producing a clean
`TASK_STATE_CANCELED` with no exception. **No code change was needed for
this — `/tasks/{id}:cancel` already works as the decline mechanism.**
Also checked, for completeness: cancelling a `task_id` that was *never
started at all* does raise (`RPCError: workflow not found`) — a real,
pre-existing (not new) asymmetry worth documenting, not fixing, since a
real decline always targets an id that actually came from a paused task.

**What the attempt to verify this DID find, before any of the above
could even be tested: a genuine, severe, pre-existing bug, unrelated to
decline.** Standing up a real sandboxed Temporal `Worker` for
`AriAgentWorkflow` (the default configuration — what
`common/orchestration/worker.py` actually uses in production) failed
outright: `RuntimeError: Failed validating workflow AriAgentWorkflow`,
several layers down a `sniffio`/`anyio` restricted-import failure.
Root-caused by bisecting with `UnsandboxedWorkflowRunner` (confirmed the
same workflow runs fine unsandboxed — a sandbox-compatibility issue, not
a logic bug) then with a minimal reproduction: `workflow.py`'s own
`from common.agent_meta import get_agent_meta` (added during the earlier
multi-agent restructuring, for `_artifacts()`) transitively imports
`a2a.types` for `AgentSkill` — pulling the a2a-sdk's async HTTP stack
into the *workflow's own sandboxed module chain*, which Temporal's
default `SandboxedWorkflowRunner` cannot tolerate. This is exactly the
kind of import `workflow.py`'s own docstring already warned against
("no filesystem, HTTP, subprocess, or registry lookups here") — the
`agent_meta` restructuring violated its own stated rule, and nothing
caught it because every verification since that restructuring (full
test suite, a lightweight API-only Docker smoke test) never actually
started a real sandboxed `Worker`. **With the default worker
configuration, both agent kinds' workers would have crashed on startup
before processing a single request** — this would have been caught the
moment anyone actually deployed the restructured worker image, not
before.

**Fix:** moved artifact resolution out of `workflow.py` entirely into a
new `resolve_artifacts` activity (`orchestration/activities.py`) —
exactly the same shape as `plan_task` already being the one that calls
`get_capability()`, never `workflow.py` itself. `workflow.py` no longer
imports `common.agent_meta` at all. `_fallback_message`/`_build_response`
now take a precomputed `artifacts` dict instead of calling `_artifacts()`
(deleted) themselves. Registered the new activity in
`common/orchestration/worker.py`.

**Verified:** full suite 124 passed (119 + 5 new — `resolve_artifacts`'s
two branches in `tests/test_activities.py`, and `_fallback_message`'s
`needs_approval`/`completed` branches plus `_build_response` in
`tests/test_workflow.py`, none of which had real coverage before). Then,
critically, re-ran the exact same real-Temporal-server reproduction that
originally caught this: the default `SandboxedWorkflowRunner` now
constructs a `Worker` for `AriAgentWorkflow` successfully, and a full
real run through `prepare_workspace → plan_task → execute_step →
cleanup_workspace → resolve_artifacts → respond` against the local
Temporal server completes correctly with `status: "needs_approval"` and
a populated `pending_action` artifact — not just unit-tested in
isolation.

## 2026-09-21 — Live "why is on-call ARI slow" investigation on the `test` cluster

Asked to check the live worker pod's logs and figure out why requests
were slow. Investigated against the real `test` kubectl context
(`insightfinder` namespace) rather than guessing from code — `kubectl
logs`/`describe` on `ari-oncall-worker-85c7444df6-tgwlk`, and, critically,
`temporal workflow list`/`show` run *inside* the `ari-oncall-temporal`
pod (it ships the `temporal` CLI) against the real Temporal dev server,
since the deployed image predates any of this session's work and had no
way to expose that data otherwise.

**Found, all confirmed live, not inferred:**
- The worker pod had **OOMKilled 6 times in ~16h** (2Gi limit / 512Mi
  request) — `describe pod` showed `Last State: Terminated, Reason:
  OOMKilled`.
- Both `repo-cache` and `sandbox` were plain `emptyDir` volumes (the
  deployed `deploy/k8s/deployment-worker.yaml` and the Helm chart both
  confirmed this) — every OOM restart wiped the whole mirror cache, so it
  was rarely warm.
- Two concurrent requests hitting `clone_repo`/`analyze` within ~2s of
  each other both failed with `opencode (...) failed (exit 1): ...
  database is locked` — opencode keeps its own local session state under
  `$HOME` (one shared SQLite file regardless of `--dir`), and nothing
  isolated `HOME` per activity, so two concurrent opencode subprocesses in
  the same worker process collided on it.
- `common/orchestration/worker.py`'s `Worker(...)` had no
  `max_concurrent_activities` — Temporal's SDK default (100) let far more
  concurrent opencode/build subprocesses pile into one 2-CPU/2Gi container
  than it could sustain, very likely a real contributor to the OOM kills
  above.
- The account had also hit its Anthropic **API usage cap** at the moment
  of the initial check (`"You will regain access on 2026-10-01"`) — not a
  code bug, but explains why everything looked completely stuck that day.

**Fixed, all in this same pass:**
- `common/opencode_runtime.py`: `run_opencode` now isolates only
  `XDG_DATA_HOME`/`XDG_CONFIG_HOME`/`XDG_CACHE_HOME`/`XDG_STATE_HOME`
  under the request's own `ctx.workspace` — deliberately **not** the real
  `$HOME`, so `verify`'s build-tool dependency caches (`~/.m2`, Go's
  module cache, `~/.npm`) stay shared/warm across requests. (First pass
  isolated the whole `$HOME`, which would have silently forced a cold
  dependency download on every `verify` run — caught and corrected before
  this was ever deployed, see the entry below on build caching.)
- `common/settings.py` + `common/orchestration/worker.py`: new
  `max_concurrent_activities` setting (default `3`, conservative for a
  2 CPU/2Gi pod), wired into `Worker(...)` in place of the SDK default.
- Helm chart (`insightfinder/charts`, `charts/oncallari`): `repo-cache`
  backed by a real PVC instead of `emptyDir` (a `ariOncallAgent.
  persistence.repoCache` toggle + PVC template already existed in the
  chart but was off — just flipped it on). `accessMode` made configurable
  and defaulted to `ReadWriteOnce`, not the PVC template's original
  hardcoded `ReadWriteMany` — checked the actual `test` cluster's
  StorageClasses (`kubectl get storageclass`) and found only `gp3`
  (AWS EBS via `ebs.csi.aws.com`), which is RWO-only; `worker.replicaCount`
  is `1` there today anyway, so RWO still fully fixes the restart-loses-
  cache problem, just doesn't share the cache across replicas (documented
  as a follow-up for whenever an EFS/NFS StorageClass exists).

**Verified:** full suite 127 passed (124 + 3 new — `test_opencode_runtime.py`
covering the XDG isolation). `helm lint`/`helm template` confirmed the PVC
renders and the worker mounts it in place of `emptyDir`.

## 2026-09-21 — Temporal workflow history showed `verify` had *never* completed in the entire retained window

Asked to check why every workflow in the Temporal UI "takes a huge time."
Pulled the full event history (not just logs) for all 13 workflows
retained (72h retention) via the in-pod `temporal workflow show -o json`.
Found a single, consistent, load-bearing pattern:

- 10 of 13 workflows: `verify` starts, then **7.5–10 minutes later** an
  `EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED` fires (`identity:
  1@ari-oncall-api-<pod>` — the API pod forwarding a real `tasks/cancel`
  call it received), killing the in-flight `verify` activity. The whole
  workflow then falls back through `cleanup_workspace → resolve_artifacts
  → respond` and reports `pipeline_status: "failed"`.
- 1 workflow: `verify` ran the *full* ~19 minutes, essentially hitting
  `workflow.py`'s then-hardcoded 20-minute `start_to_close_timeout`.
- The remaining 2 were the "database is locked" fast-failures above.

**In the entire retained window, `verify` never completed successfully
once.** Root cause, confirmed by reading the code (`capabilities/verify.py`,
`tools/sandbox.py`, `settings.py`): **there was no dependency cache
anywhere** — every `verify` run did a fully cold Maven/Go-module/npm
dependency download before even running the actual build/test, on top of
`workflow.py` unconditionally aborting the *entire* pipeline on any raised
`ActivityError` (timeout or cancel) even for a step (`verify`) that
already had an `on_failure: goto generate_change` retry-loop defined for
a *reported* failure — the retry loop just never triggered for a killed
activity, only for one that returned `{"status": "failed"}` on its own.

**Fixed:**
- `configs/oncall/capabilities.yaml`: `verify.timeout_seconds: 480` (new
  per-capability override, threaded through `capabilities_registry.py` →
  `activities.py`'s `plan_task` → `workflow.py`) — well inside the
  observed ~8-10 minute external-cancel window, so the pipeline's own
  timeout wins the race deterministically instead of an external caller.
- `common/orchestration/workflow.py`: a raised `ActivityError` for a step
  that *has* an `on_failure` policy is now folded into the same
  `{"status": "failed", "failure": ...}` shape a reported failure already
  produces, and goes through the identical retry loop. Only a step with no
  `on_failure` policy still aborts the pipeline outright on a raised
  error. Verified with a real `temporalio.testing.WorkflowEnvironment`
  (time-skipping) run, not just mocks — confirmed `verify` retries via
  `generate_change` on a simulated kill, and a step with no `on_failure`
  still aborts immediately (`tests/test_workflow_execution.py`).
- New persistent, shared HOME for `verify`'s build subprocesses only
  (`settings.build_cache_root`, `ari_oncall_agent/tools/build_cache.py`),
  scoped narrowly to the `verify` capability in `opencode_cli.py` — see
  the XDG-isolation correction in the entry above for why this had to be
  a *separate* HOME from opencode's own per-request state, not the same
  one. Backed by a new PVC in the Helm chart (`ariOncallAgent.persistence.
  buildCache`, mirroring the `repoCache` PVC pattern).

**Verified:** full suite 143 passed. `helm lint`/`helm template` confirmed
the new `build-cache` PVC renders and mounts.

## 2026-09-21 — Repo-cache and build-cache PVCs kept small (5Gi) on purpose → LRU eviction

User asked to size both PVCs at 5Gi and add LRU eviction so a small cache
doesn't just fill up and start failing. Built a shared, generic utility
(`common/cache_gc.py`) rather than one-off logic in each cache: sizing
signal is `shutil.disk_usage(cache_root)` (the real filesystem backing
the PVC), triggers below 15% free, evicts least-recently-used entries
(newest mtime found scanning a few levels into each candidate — cheap,
no full recursive walk) until back above 30% free. Both thresholds are
settings (`cache_evict_low_water`/`cache_evict_high_water`), shared by
both caches, wired through the Helm chart too.

Two different locking shapes were needed, matched to how each cache is
actually structured:
- **repo-cache** (`ari_oncall_agent/tools/repo_cache.py`): one
  independently-lockable entry per repo already existed (its own
  `<hash>.lock` flock, from the earlier same-repo-concurrency fix) —
  eviction reuses those exact lock files, non-blocking, so it only ever
  deletes a repo that isn't currently being fetched/cloned.
- **build-cache** (`ari_oncall_agent/tools/build_cache.py`, new): no
  natural per-entry lock — every immediate child (`.m2`, `go`, `.npm`,
  ...) is one shared tool's own cache, not an independent per-repo unit.
  Used a classic reader/writer lock over the whole root instead: a
  `verify` run holds it `LOCK_SH` for the call's duration, eviction only
  ever attempts `LOCK_EX | LOCK_NB` — it either runs while genuinely idle
  or skips that round entirely, never blocking a build or deleting out
  from under one.

**Verified:** 8 new tests (`test_cache_gc.py`, `test_build_cache.py`)
covering the no-op-when-fine case, oldest-first eviction with high-water
stopping, and that a locked/in-use entry is never touched (confirmed via
real `fcntl.flock` contention across separate `open()` calls on the same
path, not mocked). Full suite 140 passed. Helm chart PVC sizes rendered
as 5Gi.

## 2026-09-21 — `publish_pr` made deterministic (git + REST), opencode+MCP kept as fallback

`publish_pr` used to be entirely opencode-driven: one `ari-publish` agent
with the GitHub MCP server's tools enabled, doing `git add`/`commit`/
`push` via its bash tool and opening the PR via an MCP tool call — every
one of those steps paid a full LLM reasoning round-trip, on top of an MCP
network hop, for work that was **already fully deterministic** by the
time this step runs (title, body, branch name, and file list are all
computed in plain Python before opencode is ever invoked). Confirmed via
the same Temporal history above that this pattern (LLM-mediated
mechanical git operations) was a real, generalizable cost, not specific
to `verify`.

**Primary path now**: `git_ops.add`/`commit`/`push` (new plain subprocess
helpers, same shape as the existing `clone`/`diff`/`changed_files`) plus a
direct GitHub REST API call (new `tools/github_rest.py`,
`POST /repos/{owner}/{repo}/pulls`, same PAT already available) — zero
LLM/opencode/MCP involvement. Branch name is now derived deterministically
(`ari/<title-slug>-<request-id-prefix>`) instead of left for the LLM to
invent.

**Fallback, explicitly requested, not removed**: if any step of the
primary path raises, `publish_pr` falls back to the original opencode +
GitHub MCP flow (`ari-publish` agent, kept in `opencode_cli.py` exactly as
before) — the fallback instruction tells opencode a prior attempt may
have partially completed (check `git status`/`git log`/`git branch`
first, don't blindly redo finished work) rather than assuming a clean
slate. Only if *both* the primary path and the fallback fail does the
step actually error out. Kept because the MCP-driven route is more
flexible for whatever an unusual repo/auth state might need that a fixed
git/REST sequence can't adapt to.

**Verified:** 12 new tests — `test_git_ops.py` exercises `add`/`commit`/
`push` against a real local bare-repo remote (not mocked: confirms `push`
creates a genuinely new remote branch without moving `main`'s tip);
`test_github_rest.py` mocks only the HTTP transport layer
(`httpx.MockTransport`), not the function under test. Rewrote every
`publish_pr` test in `test_capabilities.py` for the new primary/fallback
shape, including one that forces the primary path to fail and asserts the
fallback actually runs and finds the PR url. Full suite 152 passed.

## 2026-09-21 — `analyze` merged into `generate_change`; `verify` flipped from opt-out to opt-in

Two more asks from the same optimization pass:

**Merge**: `analyze` (read-only investigation, its own `ari-analyze`
opencode agent/session) used to always run before `generate_change`
(edit, a second opencode agent/session) for anything that needed
scoping out first — two separate opencode subprocess starts and LLM
session boundaries for a single logical "figure out what to do, then do
it" unit of work. Merged: `generate_change` (agent: `ari-change`, `edit:
allow`) now investigates as needed and edits in one continued session —
opencode's own agentic loop already reads/greps before editing within a
single turn, same as how a capable engineer actually works, so the split
bought nothing but a second subprocess/session-start cost every time.
`analyze.py` deleted; `ari-analyze` removed from `_AGENT_DEFS`/
`CAPABILITY_AGENTS`. The `root_cause` artifact (`agent_meta.artifacts`)
now comes from `generate_change.root_cause` — reuses the same
`SUMMARY:`-marked text as the change summary, since for a single-pass
agent "what I found and did" is one coherent narrative, not two separate
extractions.

**Opt-in verify**: capability selection is entirely LLM-planner-driven
(`common/planner.py` — there is no hardcoded pipeline to branch on), so
the only lever is what the planner is told. Flipped `verify`'s
`capabilities.yaml` description and `meta.py`'s `planner_domain_guidance`
from opt-out ("skippable when asked to skip") to explicit opt-in ("only
include this step when the request explicitly asks for tests/
verification... omit it for an ordinary bug fix/feature/edit request") —
building/testing an arbitrary target repo is this pipeline's slowest,
least predictable step (see the two entries above on why), so it's a real
cost only worth paying when actually asked for. Also removed `analyze`
and `verify` from `planner_fallback_steps` (the template used only when
the planner's own JSON output fails to parse) so that fallback matches
the new default pipeline shape too: `clone_repo → generate_change →
publish_pr`.

**Verified:** full suite 154 passed, including an explicit
`"analyze" not in catalog` assertion and one asserting `verify`'s catalog
description actually says "opt-in"/"not part of the default pipeline"
(can't unit-test the LLM planner's actual behavior, so this is the
closest verifiable proxy — the existing `test_fallback_steps_only_name_
capabilities_that_kind_actually_has` test already generically guards the
fallback-steps list against referencing a capability that doesn't exist).

## 2026-09-22 — Real Docker sandbox e2e run against 3 live repos, using gpt-4o-mini (Anthropic key was over quota) — found and fixed 4 live bugs

Built `Dockerfile.api`/`Dockerfile.oncall` locally, wired to the existing
`temporal-dev` stack, and ran real requests (real GitHub PAT, real repos:
`InsightAgent`, `insightfinder-mcp-server`, `test-fluxcd`) instead of unit
tests alone. Found, fixed, and re-verified live:

- **Missing `clone_repo`**: the planner (a cheaper model) sometimes
  scheduled `generate_change` without `clone_repo` first, so opencode ran
  against an empty, non-git `--dir` and crashed opaquely. Fixed with
  `StepContext.require_cloned_repo()` (raises clearly) plus sharper
  `planner_domain_guidance`.
- **`publish_pr` inconsistently included**: same request, two separate
  runs — one opened a PR, the other didn't, despite neither prompt
  declining one. Fixed with a new `AgentMeta.default_step_policy` hook
  (`ari_oncall_agent/meta.py`'s `_ensure_publish_pr_by_default`): always
  appends `publish_pr` when `generate_change` ran, unless
  `options.skip_pr` or the planner's own explicit `skip_publish_pr: true`
  (a new `Plan` field — omitting the step alone isn't a reliable enough
  signal that the user declined, vs. the model just forgetting).
- **Wrong branch guessed**: planner set `ref="main"` for `InsightAgent`,
  whose real default branch is `master` — hard failure. Fixed by having
  `ensure_cache_and_clone` check `git_branch_exists` *before* cloning and
  fall back to the dynamically-detected `git_default_branch` (works for
  any name — `main`/`master`/`dev`/anything — confirmed with a
  same-shaped `dev`-default test repo too, not special-cased to two
  names).
- **Ugly PR title**: title and body came from the same `SUMMARY:` text,
  title just being a blind 72-char slice — cut off mid-word live (PR
  insightfinder/insightfinder-mcp-server#55: "...that aggreg-"). Fixed by
  asking opencode for separate `TITLE:`/`SUMMARY:` sections
  (`generate_change.py`), with a word-boundary-safe `_safe_title()`
  fallback only if the model omits the title even after one retry.

All three repos completed real end-to-end runs with real PRs after the
fixes: insightfinder/InsightAgent#989, insightfinder/insightfinder-mcp-
server#55 (pre-title-fix), insightfinder/test-fluxcd#7. Full test suite
at 189 passed throughout.
