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
