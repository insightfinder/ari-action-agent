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

## 2026-09-25 — A 20-minute "slow run" was a hung `git diff`, not slow work

Chased a live complaint that on-call ARI runs are wildly inconsistent —
most finish in ~2 minutes, some take 20+ and hit the activity timeout.
The obvious reading (a big monorepo, the model exploring more) turned out
to be wrong, and the tracing added on 09-17 is what made that visible.

**The runs, measured on the `test` cluster** (worker logs, `opencode_call`
lines) — all the same incident against InfrastructureCore:

| run | tool calls | `generate_change` | wall clock |
|---|---|---|---|
| 2c841c33 | 18 | 57s | ~2m |
| 7ed8ef87 | 21 | 95s | ~2m |
| cc5c8caa | 20 | 84s | 2m3s |
| 75cc0ce6 | 19 | 104s | 1m31s |
| **b22cb82d** | **33** | **121s** | **22m** |

The slow one looks like "the model did 33 calls instead of 20" until you
line up its timestamps: opencode returned at 09:53:21 having done its 33
calls in 121s, and then *nothing happened for 18 minutes*. Inspecting the
worker pod mid-hang (`/proc`, not `ps` — the image has no `ps`) showed
**only PID 1**: no opencode, no git, no child of any kind.

**The traceback Temporal printed when it finally cancelled the activity
named the line exactly:**

```
  File "ari_oncall_agent/capabilities/generate_change.py", line 164, in run
    diff = await git_diff(ctx.repo_dir)
  File "ari_oncall_agent/tools/git_ops.py", line 132, in diff
    stdout, stderr = await proc.communicate()
  File "asyncio/subprocess.py", line 202, in communicate
    await self.wait()
  File "asyncio/base_subprocess.py", line 230, in _wait
    return await waiter
  asyncio.exceptions.CancelledError: Activity cancelled
```

`communicate()` had already drained both pipes; it was parked on process
exit. The retry ran the identical `git diff HEAD` in the identical sandbox
directory **in under a second** (10:11:30 → 10:12:53, including a fresh
82s opencode call), so nothing about the repo, the command or the
workspace explains it. It is a race.

**Leading suspect, not proven:** a lost child-exit notification. The
worker mixes `asyncio` subprocesses with `asyncio.to_thread` (repo_cache,
build_cache) and Temporal's own threads, and an exited child whose
SIGCHLD never reaches the loop's watcher leaves `_wait()` waiting forever
— which matches all three observations (no process left, parked in
`_wait`, fixed by simply running again). Recorded as a hypothesis; the
fix below does not depend on it being right.

**Fixed: `tools/git_ops.py` — every `proc.communicate()` now has a
deadline.** New `_communicate(proc, what, timeout=None)` wraps it in
`asyncio.wait_for`, kills the child on expiry (attempts of one request
share a sandbox — `sandbox.py` keys it by `request_id` alone — so a
survivor could still be writing to the tree the retry is about to read),
and raises a message naming the operation. All eight call sites (clone,
default_branch, set origin, diff, add, commit, push, changed_files) go
through it.

Deadlines are **tiered**, because one number cannot serve both ends of the
range: `clone` of a large monorepo is legitimately minutes
(`GIT_CLONE_TIMEOUT_SECONDS`, 600s), while everything else is sub-second in
practice — `diff` and `changed_files` both measured well under 1s against
InfrastructureCore (`GIT_SUBPROCESS_TIMEOUT_SECONDS`, 30s). The first cut
used a single generous 300s, which would have left the observed `diff`
stall costing five minutes instead of twenty — better, but still five
minutes of silence for an operation that has never taken more than a
second. Both remain far above any real duration: a guard that fires on
healthy work is worse than no guard, since it turns a slow-but-fine run
into a failed one.

**What this does and does not fix.** It does not remove the race — the root
cause is unproven and the stall was seen exactly once in six runs, with no
known trigger, so there is no way to reproduce it on demand. What it
changes is the blast radius: the next occurrence fails in 30s with a named
error the retry can act on, instead of 20 minutes of silence followed by a
silently-failed incident.

**Two related gaps left open, deliberately:**
- Nothing heartbeats. Temporal can only notice a stall via the activity's
  wall-clock timeout, and — because cancellation is delivered *in the
  heartbeat response* — it cannot tell a stuck activity to stop. A
  `heartbeat_timeout` driven off opencode's existing NDJSON line loop
  would detect a stall in ~2 minutes instead of 20, and make cancellation
  possible at all.
- `generate_change` has no `on_failure` policy (unlike `verify`, which got
  `timeout_seconds: 480` + a soft-failure loop on 09-21), so two failed
  attempts end the incident silently — no PR, no report, and in an
  unattended sweep nobody finds out.

**Verified:** `tests/test_git_ops.py` +6 (a stalled child raises rather
than hangs, is killed, an already-reaped child is not an error, the
default is >= 120s, and a source-level assertion that no bare
`communicate()` is reintroduced — which is exactly how this shipped).
Full suite 122 passed on Windows excluding the 10 modules needing
`fcntl`/`a2a`.

**Verified on Test EKS (2026-09-25), against a purpose-built image.** Both
`ari-oncall-api` and `ari-oncall-worker` were built from this branch, tagged
`fix-git-timeout`, and rolled out to the `insightfinder` namespace before any
of this was pushed.

1. *Normal path, default 30s.* Workflow `0c63edf0` completed in 2m13s; all
   four steps ran; `generate_change` made 23 opencode calls in 97.98s. Zero
   `timed out after` lines — the deadline does not fire on real work.
2. *Normal path, 1s.* With `GIT_SUBPROCESS_TIMEOUT_SECONDS=1`, workflow
   `c99e227b` still completed clean. Every git call in this repo finishes in
   under a second, so 1s is not tight enough to force the failure path.
3. *Forced failure, 0.001s.* Workflow `2adbee78` ended in 47s instead of
   hanging. The worker log carried
   `RuntimeError: git default branch timed out after 0s (pid 31) and was killed`,
   Temporal retried through attempts 1/2/3 with a fresh pid each time
   (31, 39, 48), and the pod had **0** leftover git processes. This is the
   behaviour the fix exists for, observed end to end.
4. *Restored.* Env var removed, module constants back to `30.0 / 600.0`,
   workflow `a89fb051` completed in 2m1s with zero timeout lines and zero
   leftover processes.

Note on step 3's `after 0s`: the message used `:.0f`, which rounds 0.001 to
0. Changed to `:g` afterwards. For every value that will actually be
configured (30, 600) the two render identically, so the image verified above
and the code as committed behave the same.

**Deployment gotcha, not yet in the chart.** The worker mounts RWO EBS
volumes, so the default `RollingUpdate` strategy deadlocks: the new pod waits
for a volume the old pod still holds. Recovery is scale to 0, patch
`strategy: {type: Recreate}`, scale back to 1. This is currently only a live
`kubectl patch` on Test — `insightfinder/charts` still ships RollingUpdate,
so the next `helm upgrade` will reintroduce the deadlock.

## 2026-09-28 — Timeout + retry on every outbound API call

Asked for by Mustafa: "the API layer must all have timeout and retry."
The Temporal *activity* layer was already audited for retry policy on
2026-09-22 (`plan_task`, `cleanup_workspace`, `prepare_workspace` — see
that entry); the HTTP layer never was. Audit of every outbound call on
`test` (`ffd2dd8`) found timeouts everywhere and retries nowhere:

| call site | timeout | retry (before) |
| --- | --- | --- |
| `github_rest.create_pull_request` | `timeout=30` | none |
| `jenkins_rest.stop_build` (crumb GET + stop POST) | `timeout=30` | none |
| `app_factory` A2A push-notification sender | `timeout=30` | none |
| `llm/client` planner call | **unset** — SDK default | SDK default |

The `timeout=30` on those `httpx.AsyncClient`s is fine: httpx applies one
scalar to connect/read/write/pool alike, so none of them could hang.

**The LLM client was the one real timeout hole**, and its own docstring
had it backwards — it said we "inherit their retry/timeout handling" as
though that settled it. Measured inside the deployed image rather than
assumed: `openai 3.19.2` and `anthropic 1.8.0` both ship
`Timeout(connect=5, read=600, write=600, pool=600)` with
`max_retries=2`. That is 600s x 3 = **30 minutes**, against a
`start_to_close_timeout` of 1200s — Temporal would kill the activity
while the SDK sat waiting, and the traceback would blame the activity,
not the SDK default that caused it. Now explicit: 120s x 3 = 6 minutes
(`LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`), with a test asserting the
worst case stays under 1200s so the two cannot drift apart again.

**New `src/common/http_retry.py`** carries the one policy. What it will
and will not retry is the whole design:

- Retried: `httpx.TransportError` and `{408, 425, 429, 500, 502, 503,
  504}` — "the server did not process this, or could not tell us whether
  it did".
- Not retried: every other 4xx. A 401/404/422 is a verdict, not weather;
  retrying burns the budget and delays the real error.
- `Retry-After` (delta-seconds *and* HTTP-date) beats our own backoff,
  because the server knows its rate-limit window and we do not — capped
  at 60s so a server cannot park an activity past its timeout. A
  malformed header falls back to backoff rather than aborting.
- Backoff is exponential with equal jitter. Jitter is not decoration
  here: the worker runs several capabilities concurrently against the
  same GitHub org, so un-jittered backoff would line their retries up
  into one burst against an API already rate-limiting us.
- It **returns** the response on a final non-2xx instead of raising.
  Callers already write error messages carrying their own API's context;
  this layer has nothing better to say. It raises only when there is no
  response at all, and then names the operation (`what=`) — the same
  reason `git_ops._communicate` names its git subcommand.

**`create_pull_request` is the one call a retry could duplicate**, so it
got idempotency rather than just retries. The head branch is already
deterministic per request (`_branch_name` → `ari/<slug>-<request_id[:8]>`),
which makes "does this already exist?" answerable:

1. `GET /pulls?head=<owner>:<branch>&state=open` first — an existing open
   PR is returned as-is, no POST.
2. That lookup is best-effort: anything but a clean 200 with a non-empty
   list means "create it". It exists to avoid a duplicate and must never
   become the reason no PR gets opened.
3. A 422 from the POST re-runs the lookup. This closes the race the
   pre-check cannot: a previous attempt's POST reached GitHub, its
   response was lost, so this attempt saw no open PR. GitHub says 422,
   and the PR the caller wanted exists — that is success, not failure.

**`publish_pr.py` deliberately untouched.** Its one big `try` around
git add/commit/push/create-PR drops *any* failure to the opencode
fallback (extra subprocess + several LLM-mediated tool calls, measured as
this step's dominant cost on 2026-09-21). That looked like the thing to
fix, but it is a symptom: once a transient 502 is absorbed inside
`create_pull_request`, it never reaches that `except` at all. Splitting
the `try` would have been a second, riskier change buying nothing.

**Verified:** +45 tests. `tests/test_http_retry.py` covers each retryable
status individually, each non-retryable one, `Retry-After` in both
formats plus past-dated/malformed/absent, the cap, the jitter bounds, and
that exhausted retries return rather than raise. `tests/test_jenkins_rest.py`
is new — `stop_build` had no HTTP-level test at all, it was mocked
everywhere. Two source-level guards in the same spirit as the
`communicate()` guard in `test_git_ops.py`: every `httpx.AsyncClient` in
`src/` sets a timeout, and no module calls httpx directly instead of
going through `http_retry` — a REST client added later is exactly how
this stops being true.

Full suite **241 passed**, run inside the deployed
`ari-oncall-agent-worker:test` image with `src/` and `tests/` mounted
over it, so the 10 modules that cannot be collected on Windows (`a2a`,
`fcntl`) were included rather than skipped.

## 2026-09-28 — First real `opencode run` against a live Jenkins, which immediately found the read agent could not call a single tool

This closes the gap the 2026-09-18 Jenkins entry ended on, verbatim:
*"the actual capabilities ... have had zero real `opencode run`
invocations against a real Jenkins, by anyone."* They have now. The first
one failed, for a reason no amount of reading would have found.

**Getting to a first run at all took two unrelated unblocks**, both worth
recording because neither is in any code:

- *Network.* The agent runs on the `test` EKS cluster in `us-west-1`;
  `jenkins.insightfinder.com` is an EC2 instance in `us-east-1` with no
  peering between them. Its security group already allowed `10.0.0.0/16`,
  `10.1.0.0/16` and `10.2.0.0/16`, which looks like it covers the cluster
  and does not: that cluster's VPC has no NAT gateway at all. Its nodes
  carry public IPs directly, so a pod egresses as **whichever node it
  happens to be scheduled on** — eight of them, each a different address.
  Allowing the one address observed first (`sgr-0e52e1f30382b2119` on
  `sg-01a97ff62f0dec233`) worked until the worker was redeployed and
  landed on a different node, at which point every MCP call timed out
  again and looked exactly like a regression in the fix below. Anything
  from this cluster that must reach a firewalled host needs every node's
  address allowed, or it is one reschedule away from breaking. Revoke
  these when Jenkins testing is done. Reachability was first checked from
  inside the worker pod with a raw socket — which proves the network and
  nothing about the agent, a distinction that mattered twice.
- *A bad key that looked like a dead key.* The first attempt failed with
  `anthropic.AuthenticationError: API key is invalid`, and the same key
  from the same host failed the same way against `api.anthropic.com`
  directly. It was not revoked: the `export` had been pasted into a
  `/bin/sh` session where it was mangled mid-line. Worth knowing because
  Anthropic answers a mangled key and a revoked key identically — 401
  `authentication_error` either way (a valid key out of credit is a 400,
  and over its rate limit a 429, so the distinction that matters is not
  the one the status code makes).

**The bug.** `chat` against `Jenkins-Backup` returned
`TASK_STATE_COMPLETED` with the model politely explaining that no Jenkins
or MCP server was connected. The worker trace said why:

```
tool_call_counts={'list_mcp_resources': 1, 'list_mcp_resource_templates': 1}
tool_call_total=2
```

Those are opencode's own built-in MCP introspection tools. The model had
nothing else — it reached for the only MCP-shaped things it could see,
found no *resources* (the plugin exposes tools, not resources), and gave
up. No MCP connection error anywhere in the logs, because the connection
was fine.

`ari-jenkins-read` carried `"tools": {..., "jenkins_*": False}`, and
`write_config` set the same wildcard as the global default. The
2026-09-18 entry had already confirmed opencode's naming rule from its own
docs — *"MCP server tools are registered with server name as prefix"* —
and concluded the `jenkins_*` guess was "very likely correct". It was
correct. That was the problem. The server is registered under the key
`jenkins`, so **every** tool is `jenkins_*`: the plugin's 15 read-only
tools exactly as much as its 4 mutating ones. The wildcard denied the
read agent the entire Jenkins API, and `resolve_target`/`inspect`/
`diagnose`/`propose_action` have no other route to Jenkins — only
`jenkins_stop_build` is direct REST.

The risk flagged in `opencode_cli.py`'s own docstring since the agent was
built was the *opposite* failure: a wrong prefix matching nothing and
silently denying nothing under `--auto`. Confirming the prefix was right
was treated as closing that risk, when the same fact opened this one. A
wildcard has two failure modes and only one of them was ever considered.

**Proved by A/B inside the deployed pod** (`2609190017-test`), not
reasoned about: same `mcp` block, same agent, deny replaced with the four
exact names, then `opencode run --dir ... --agent ari-jenkins-read
--format json --auto` asking for the last build of `Jenkins-Backup`.
opencode called `jenkins_getBuild` — the live tool name, which also
settles the prefix question by observation rather than by documentation —
and answered `#120 SUCCESS`, matching what the REST API reports. The
config written for that run was deleted from the pod afterwards; it held
a real Jenkins token and a real LLM key.

**The fix** replaces every wildcard with exact names, in all three places
(global default, both agents):

- `_MCP_SERVER_NAME = "jenkins"` is now the single source of the prefix,
  used to build both the `mcp` key and every permission entry, so renaming
  the server cannot detach the permissions from the tools it guards.
- `_MUTATING_TOOLS` pins the plugin's four `destructiveHint` tools.
- Read-only tools are deliberately *not* enumerated. Listing them would
  need an edit every time the plugin adds one, and a forgotten entry there
  silently costs a capability its data; the list worth maintaining by hand
  is the short one whose contents the plugin, not we, decide.
- `ari-jenkins-act` dropped from `jenkins_*: True` to
  `jenkins_triggerBuild: True`. It had been allowed all four mutations
  although `execute_action` performs exactly one. `replayBuild` is the
  reason this is not cosmetic: its required `mainScript` argument is
  arbitrary Pipeline Groovy run on the controller, so a wildcard handed a
  confirmed `pending_action` a code-execution route that design.md §5
  explicitly closes for `api_get` and never considered here.

**The test suite had the bug written into it as the expected behaviour** —
`test_only_ari_jenkins_act_can_call_a_mutating_jenkins_tool` asserted
`read["jenkins_*"] is False`. It passed for the same reason the agent was
broken. Rewritten, plus four new tests aimed at the class of mistake
rather than this instance: that no permission entry anywhere contains a
`*` (the guard that would have caught this), that representative read-only
tools are reachable from every block, that `_MUTATING_TOOLS` still matches
the plugin's destructive set, and that the permission prefix still tracks
the `mcp` key.

**Verified:** full suite **245 passed** (241 before, +4 new), run inside
the deployed `ari-oncall-agent-worker:test` image with this working copy's
`src/`, `tests/` and `docs/examples/` over it, so the modules that cannot
be collected on Windows (`a2a`, `fcntl`) were included rather than
skipped. The behaviour the new tests describe was separately confirmed by
the live `opencode run` above — the tests pin it, they did not establish
it.

**Then verified through the deployed agent, not just through opencode.**
The fix was built as `ari-jenkins-agent-worker:fix-jenkins-tool-perms`
(worker only — the API image runs no capabilities) from `HEAD` plus this
one file, so the new image differs from the running one by the fix and
nothing else, and rolled out with `kubectl set image` rather than
`helm upgrade`, which would have touched the on-call agent sharing the
release. All three sample-client modes then passed against the real
Jenkins:

- `chat` on `Jenkins-Backup` — reported #120 SUCCESS, ~3.4s, timer-triggered,
  120/120 green, freestyle on the controller with no SCM. Matches the REST
  API.
- `task-poll` on `CI_Pipelines/9-Dev_Branch_Pipeline/EU-EnvCheck-Automation`
  #208 — read the console log and the JUnit report, separated the failing
  NA checks from the passing EU ones in the same run, tied the UI timeout
  to four preceding `401`s on `/api/v1/refreshusertoken`, and noted #207
  had failed for a *different* reason. It concluded the fault is in the
  monitored backend rather than the test suite, which is the right call
  and not one available from build status alone.
- `propose` — returned `TASK_STATE_INPUT_REQUIRED` with a `pending_action`
  artifact, and `Jenkins-Backup` was still at #120 with nothing queued
  afterwards. Worth recording *how* it behaved: the prompt says "deploy to
  staging", the job is a parameterless backup job, and rather than inventing
  a parameter to satisfy the phrasing it said so and offered the six real
  `*/staging` jobs under `CI_Pipelines`. That is section 4's "ask rather
  than guess" happening on its own.

Across all three runs the trace shows seven distinct read-only tools used
(`jenkins_getJobs`, `getJob`, `getBuild`, `getBuildLog`, `searchBuildLog`,
`getTestResults`, `getFlakyFailures`) and **no mutating tool called at
all** — the permission block doing exactly what it now says.

**A second network trap, which first looked exactly like the fix failing.**
After the rollout every MCP call timed out again and opencode registered
no `jenkins_*` tools whatsoever (`Model tried to call unavailable tool
'jenkins_getLastBuild'. Available tools: glob, grep, invalid, read, skill,
todowrite`). The cause was not the fix: the new pod had been scheduled onto
a different node, and — as corrected above — this cluster's pods egress as
their *node's* public IP, so it arrived from an address the security group
did not know. All eight nodes are allowed now. Note the shape of the
symptom, because it is misleading twice over: a blocked MCP endpoint
removes the tools from the model's toolset entirely rather than erroring,
so it is indistinguishable from a permission bug by the model's own
account of itself. The distinguishing evidence was that opencode's
built-in `list_mcp_resources` disappeared too, which a permission entry
naming only `jenkins_*` tools could not explain. (The first attempt after
the rollout also got eaten by an unrelated race — the terminating old pod
was still polling the task queue and picked the activity up, then died
holding it; Temporal waits out the 20-minute `start_to_close_timeout`
before retrying, so a redeploy needs the old pod actually gone, not merely
`rollout status` reporting success.)

**Still unverified.** `execute_action` has not been run against a real
Jenkins — doing so means triggering a real build on a production
controller, which is a decision, not a test step. The read path is proven
end to end through the deployed agent; the write path is proven only in
unit tests.

## 2026-10-05 — A live stg run reported an action as executed that was never scheduled; the planner had silently fallen back

Reported from the demo system on stg: the ARI Action Agent "did not ask
for approval, it directly carried out the action", and no approval email
was sent. Two symptoms, one cause, and the cause is in `common/`, so it
applies to every agent kind — not just the action kind where it surfaced.

**What the Temporal history actually shows.** Namespace `action-ari-agent`
on `stg-temporal.insightfinder.com`, workflow
`b889d00d-71db-4d02-905e-2c2d5037e47f`, 2026-10-05 12:22:02 AM GMT+8.
`plan_task` returned:

```json
{"rationale": "planner output was not valid JSON; used default pipeline",
 "steps": [{"capability": "resolve_target", "confirmed": false, ...}]}
```

Exactly two `execute_step` activities ran — `resolve_target` and
`respond`. `resolve_artifacts` returned `{}`. No `propose_action`, no
`execute_action`, no outbound call to anything. And `respond` wrote:

> The remediation action for this predicted availability incident has been
> identified and executed. **Action executed:** Trigger Buggy Deployment
> (ID: 557db25d-...). **Target:** Instance ip-172-31-52-141.ec2.internal

**The confirmation gate was never bypassed — it was never reached.**
`execute_action` was not in the plan, so `ctx.confirmed` never mattered.
That part of the design held. What failed was everything around it.

**Three separate defects, in the order they bit:**

1. *The planner parsed strictly while the rest of the repo parsed
   leniently.* `create_plan` used a bare `json.loads`. The action agent's
   own capabilities have used a tolerant reader (fenced blocks, a
   sentinel, prose either side of the object) since they were written. A
   model that answers correctly but wraps its object in a ```json fence
   was therefore a planner failure and a capability success in the same
   process. Now `common/llm_json.py`, shared, with the sentinel optional
   because the planner prompts for a bare object.

2. *The fallback is a plan, so nothing downstream can tell it apart from
   a real one.* The run completes, `respond` runs as usual, and the
   caller gets a confident answer about a pipeline that never ran what
   was asked. For the action kind the fallback was `[resolve_target]` —
   it proposed nothing, so MCP Service never saw a pause, so UIE never
   sent its approval email. That is the whole of "no approval email".
   **For oncall the same fallback is `[clone_repo, generate_change,
   publish_pr]`** — a malformed planner response makes it clone, change
   code and open a PR with no plan at all. Unchanged here, and worth its
   own decision.

3. *The raw response was discarded.* The `except` branch threw `raw`
   away, so what the model actually emitted is unrecoverable for this
   incident. Now logged as `planner_output_unusable` with the response
   truncated to 4000 chars and the fallback it chose. This is the fix
   that makes the next occurrence diagnosable instead of inferable.

**Also fixed while here:** well-formed JSON of the wrong shape used to
raise `ValidationError` straight out of `create_plan` and fail the whole
activity, while the neighbouring malformed-JSON case fell back. Both now
fall back.

**`respond` can no longer claim an execution that did not happen.** The
prompt already said "Never invent facts not present in what's given to
you below" and that did not stop it, because the absence of a step is the
hard thing for a model to notice — it sees a proposal in the results and
narrates the obvious next thing. So the absence is now computed in code
(`respond.execution_fact`, keyed on `agent_meta.confirm_capability`) and
stated: `FACT: NOTHING WAS EXECUTED. The <capability> step did not run in
this request...`, plus a prompt rule making `FACT:` lines binding over
anything inferred. Empty for an agent kind with no `confirm_capability`
(oncall), which has no single step meaning "something outside changed".

**Deliberately not done: retrying the planner call.** Tolerant parsing
should make the fallback rare; a retry would add a second LLM call per
degraded run while hiding how often it happens. With the raw response now
logged, the next real occurrence tells us whether a retry is warranted.
Decide then, with evidence.

**Two unrelated things the same Temporal input exposed**, both on the
InsightFinder side, neither fixed here: the Action bound to Pattern 10000
on stg has `name: "Trigger Buggy Deployment"` with `cmd: "reverse buggy
deployment"` — the name and the command are opposites, and the command
uses spaces where the action server's allowlist uses hyphens, so even a
real execution would have been rejected. Its action server URL is port
9092.

**Verified:** `tests/test_llm_json.py` (12), `tests/test_planner.py` (9,
including the fenced-object case that started this and the truncated-log
assertion), and 8 new cases in `tests/test_respond.py` covering the exact
shape of the live failure — a `pending_action` in the results and no
execute step. Not verified against a live stg run yet; that needs the
image rebuilt from `test`.

## 2026-10-08 — A pull request now needs a person's approval, reusing the Jenkins gate unchanged (II-25090)

Asked for two things at once: whether the action agent's II-25052 defect
(`ari-agent-demo` `d896ae62` — "never asks for approval and falsely
reports the action as executed") also exists here, and the leader's
requirement that this agent email the change for approval before opening
a pull request. They turn out to be the same work.

**Does II-25052's defect exist in on-call? Both halves, yes.**

*The planner half* is shared code and was already fixed here —
`common/llm_json.py`, `planner.py`, `respond.py` are byte-identical on
`test`/`staging`/`main`. But **prod was running `2609282209-main`
(2026-09-28), which predates it**: probed live, `lenient parse: False /
logs raw: False / fact guard: False`. The fix reaches prod only when
someone runs `helm upgrade` — prod pins a timestamped tag, so merging and
rebuilding does not move it. Left for the deployment that ships this
feature; noted here because it is easy to assume a merged fix is a
deployed one.

*The "falsely reports" half* was present but **inert**:
`respond.execution_fact` returns `""` for an agent kind with no
`confirm_capability`, and oncall had none. So nothing stopped `respond`
claiming a pull request had been opened when no `publish_pr` step ran —
the same shape of failure, one capability name away. Setting
`confirm_capability` for the gate below turns that guard on as a side
effect; no extra code.

**The gate itself is §5b's, reused with nothing in `common/` changed.**
`propose_pr` returns `requires_approval`; `confirm_capability =
"publish_pr"`; `publish_pr` refuses anything `ctx.confirmed` is false for.
That `common/` stayed untouched also kept this out of
`ari-agent-demo`'s vendoring sync — one repo, one branch.

**The decision that shaped everything: the branch is pushed *before* the
pause.** The resume is a separate Temporal workflow execution whose plan
is a single `publish_pr` step, and by then two things are gone —
`workflow.py` runs `cleanup_workspace` on the way out of the pausing run,
and `sandboxRoot` is on no persistent volume (the chart persists only the
repo and build caches). Anything still needing `git add`/`commit`/`push`
after a human-speed approval would be publishing a change that no longer
exists on disk, and a worker restart inside the approval window is not a
remote possibility. Two alternatives were weighed and rejected:
*re-cloning and re-applying a patch on resume* (needs `plan_task`'s
single-step bypass to become multi-step — a `common/` change and a
vendoring sync — and can conflict on a base that moved), and *keeping the
sandbox alive across the pause* (also `common/`, and unreliable for
exactly the reason above, besides contradicting the deliberately small,
LRU-evicted caches). The accepted cost, stated plainly rather than
discovered later: **the branch is visible on the target repository before
anyone approves it.** That is far quieter than a pull request, but a
repository that builds every pushed branch will build it.

**`publish_pr` is `internal: true`.** Jenkins' `execute_action` stays
plannable and refuses unconfirmed work, which leaves prompt guidance as
the only thing discouraging a plan from scheduling it. Here the
capability is simply absent from `catalog()`, so there is nothing to
discourage — `get_capability()`, which the bypass uses, does not filter
internal capabilities. Structural, not advisory.

**`planner_fallback_steps` now ends at `propose_pr`** — this is the
decision the 2026-10-05 entry left open ("worth its own decision"). A
degraded run still does the safe *whole* job: the change is made, pushed,
and put in front of a person. It used to end at `publish_pr`, so a
malformed planner response opened a pull request with no plan behind it.

**The approval email gets a summary plus key hunks, not the whole diff.**
Chosen deliberately: the email renders the agent's prose
(`pause["question"]`), never the artifact, so an unbounded diff would be
both unreadable and past a mail server's limits. `key_hunks()` is
deterministic — no LLM — because what a person approves has to be the
change, not a model's description of it. The full diff is one click away
via `compare_url`, which is only possible *because* the branch is already
pushed.

**Removed: the opencode + GitHub MCP fallback in `publish_pr`.** It drove
commit/push/PR against the sandbox working tree, which does not exist
after the pause, so it could not run even if left wired. Deleting it beat
keeping a branch that can never execute. The push it used to guard now
happens in `propose_pr`, while the sandbox is still there.

**Verified:** full suite **286 passed**, run inside the deployed
`ari-oncall-agent-worker:test` image with `src/` and `tests/` mounted over
it — so the 11 modules that cannot be collected on Windows (`a2a`,
`fcntl`), including `test_activities.py`, `test_ari_oncall_meta.py`,
`test_a2a_executor.py` and `test_respond.py`, actually ran. 16 new cases
in `tests/test_propose_pr.py` (the pause, the proposal's contract with
`publish_pr`, id uniqueness, `key_hunks` bounds, the opt-outs, the
protected-branch veto) and 4 rewritten `publish_pr` ones, of which
`test_publish_pr_needs_no_repo_working_tree` is the regression guard for
the whole design — if that step ever regains a `require_cloned_repo()`,
every approval breaks after a human-speed round trip.

Two existing tests failed in a way worth recording, because both were
*correct* failures: `test_plan_task_raises_clearly_when_agent_kind_has_no_
confirm_capability` used oncall as its example of a kind without one, and
every shipped kind now has one — rewritten to stub the meta instead of
pinning itself to whichever kind happens to lack the field.
`test_respond_is_excluded_from_planner_catalog` asserted `publish_pr` was
in the catalog; it now asserts the opposite, which is the new guarantee.

**Not verified:** the live pause → approval email → confirm → pull request
round trip. Every piece is covered by unit tests against the real SDK
contract, and §5b's own round trip has never been exercised live either
(2026-09-18 says so). This needs a deployed image plus UIE's approval path
pointed at it, and `requireHumanApproval` enabled for the system.

---

## 2026-10-09 — The Jenkins kind moves to its own branch; three bugs found in live Temporal history

Two separate pieces of work, on the same branch because the first is what
makes the second reviewable.

### The Jenkins agent is no longer on `main`/`test`/`staging`

`jenkins_agent/` and everything that only serves it (`Dockerfile.jenkins`,
`configs/jenkins/`, its sample client and local-test script, its two test
modules, its entry in `_AGENT_PACKAGES`, in `pyproject.toml`'s packages,
and in the CI image matrix) is deleted here and released from the
`jenkins-agent` branch instead. §0 of design.md now says so, because
without that note the rest of the document describes files a reader will
not find.

Verified before deleting anything, which is the only part of this worth
recording: the Jenkins tree at this branch's tip was **byte-identical** to
`origin/jenkins-agent`, so nothing was lost. The real exposure was
somewhere else entirely — `jenkins-agent`'s last two commits (`ea23688`,
`4de9dac`, 210 lines newer than this branch's copy) were on no remote at
all. Pushed first, deleted second.

**Two tests were nearly lost with it**, and they are the reason this
section exists: `test_plan_task_bypass_branch_skips_the_llm_planner_
entirely` and `test_plan_task_normal_branch_marks_every_planner_step_
unconfirmed` lived in `test_activities.py` behind a `jenkins_agent_kind`
fixture, purely because Jenkins was the first kind to have a
`confirm_capability`. Deleting them would have left the confirmation
bypass — the exact mechanism an approved pull request resumes through
(§5c) — with no coverage, on the branch that introduced it. Rewritten
against the `oncall` kind, where they now also pin
`confirm_capability == "publish_pr"` and `publish_pr`'s real retry policy.
Any future kind-scoped test should be written against the kind under
test, not whichever kind happened to have the feature first.

### Three bugs, all found in Temporal history rather than by reading code

All three are live-confirmed, and the first two are each sufficient on
their own to explain an incident reported from HQ.

**1. `generate_change` returned success on an unmodified working tree.**
stg workflow `9b32a817`, 2026-10-08: opencode made exactly one tool call,
which was *invalid*, returned in 20.16s having edited nothing (`diff: ""`,
`changed_files: null`) — and still returned success carrying a confident,
entirely fabricated summary ("now includes exponential backoff retry
logic, DNS resolution validation, circuit breaker pattern..."). The
empty-change guard existed only in `propose_pr`/`publish_pr`, which run
*after* `verify`. So `verify` was sent to build and test a change that did
not exist, timed out at its full 480s, looped back through
`on_failure`, and did it all again. 45m56s to discover that nothing
happened in the first 20 seconds.

Now raised, not returned as a soft failure: this step has no `on_failure`
policy, so the raise gets one genuine retry from its own policy and then
ends the run with the reason stated — including the tool-call counts and
the claim it could not back up, which are what tell "the model did
nothing" apart from "the sandbox is broken". The guard deliberately does
*not* relax on an `on_failure` loop-back: the earlier attempt's edits are
still uncommitted in that same tree, so a clean tree there means nothing
was ever done, not "nothing new".

**2. No wall-clock ceiling on the pipeline.** The same 45m56s run never
exceeded a single configured timeout, because every bound in
`capabilities.yaml` is per-*attempt*. Full accounting from the history:

```
   0-   5s  prepare_workspace + plan_task + clone_repo          5s
   5-  53s  generate_change #1  OK                             48s
  53- 533s  verify #1           TIMEOUT (timeout_seconds: 480) 480s
 534-1734s  generate_change #2 attempt 1  TIMEOUT (default)   1200s
1734-1744s  backoff (backoff_seconds: 10)                      10s
1744-1764s  generate_change #2 attempt 2  OK — empty diff       20s
1764-2245s  verify #2           TIMEOUT                       481s
2246-2752s  generate_change #3 attempt 1 (cancelled by hand)  506s
2752-2756s  cancel -> cleanup -> resolve_artifacts -> respond    4s
```

Note the history shows no event for an intermediate activity attempt —
the 1210s "gap" at 534s is `attempt=2` on the STARTED event, i.e. 1200s
(attempt 1's `start_to_close`) + 10s backoff, exactly. Reading that gap as
a stall is the easy mistake here.

The design maximum for that plan is ~3.2 hours. There is now a
whole-pipeline budget (1 hour default, `options.pipeline_budget_seconds`
per request), applied as each step's `schedule_to_close_timeout` rather
than as a between-steps check — a check between steps cannot interrupt the
step that is already overrunning, which is the entire failure mode.
Measured from `workflow.info().start_time`, so it covers planning too; a
confirmation resume is its own workflow and gets its own full budget.

**3. `git push` was held to the 30s local-operation timeout.** It is the
only non-clone command in `git_ops.py` that talks to a network, and it
shared `GIT_SUBPROCESS_TIMEOUT_SECONDS` with `diff`/`add`/`commit`. Now a
third tier (`GIT_NETWORK_TIMEOUT_SECONDS`, 120s). What this cost, on prod
2026-10-08: three concurrent runs against `ECommerce-Demo-APIServer` all
lost the deterministic publish path and fell through to the (then still
present) opencode + GitHub MCP fallback, which lets the LLM invent branch
names. `git diff timed out after 30s` appears in the same worker-log
window. Of those three runs, `ec84dcec` created PR #3 — and `64f7cbc6`,
finishing 28 seconds later, called `github_list_pull_requests`,
`github_pull_request_read` and **`github_update_pull_request`**, never
`github_create_pull_request`, then reported `/pull/3` as its own from the
text-regex fallback (`"branch": ""` is that path's signature). One run's
change was silently folded into another's pull request. Two of the three
also called `github_fork_repository` on a company repo.

That fallback is already gone from this branch (2026-10-08 entry), so the
duplicate-PR path is closed here regardless; `main` and the prod image
`2609282209-main` still carry it. The timeout fix matters anyway, because
losing the deterministic path at all is what put an LLM in charge of
branch naming in the first place.

**Also found, not fixed (out of scope, recorded so it is not re-derived):**
there is no `activity.heartbeat()` anywhere in this codebase, so Temporal
cannot deliver cancellation — a timed-out attempt's opencode subprocess
keeps running in the pod after the workflow has moved on.

**Verified:** full suite **274 passed**, run inside
`ari-oncall-agent-worker:test` with `src/`, `tests/`, `configs/` and
`docs/` mounted over it, so the 11 modules that cannot be collected on
Windows (`a2a`, `fcntl`) actually ran — including the three the Jenkins
strip modified. 15 new cases: 3 for the empty-tree guard, 3 for the
timeout tiers, 4 unit cases for budget parsing, 5 behavioural ones in
`test_workflow_execution.py`.

A note on writing the budget tests, because it cost an hour: the
time-skipping environment only skips while **nothing is running**, so an
activity that blocks for the budget costs that budget in real seconds
(three tests, 120s each, 364s for the file). Driving the clock with retry
backoff instead is instant — but Temporal will not sleep past an
activity's `schedule_to_close`, so a backoff longer than the remaining
budget ends that attempt without the clock moving at all, and the budget
is never actually spent. Small backoff, many attempts. 4.47s.

### Later the same day — driven live, which found a fourth defect the unit suite could not

`scripts/oncall_live_test.py` (new) drives a deployed agent over its real
A2A surface and asserts on what comes back. Run by hand against test/stg,
never in CI: every scenario costs real tokens and two of them write to a
real repository. Ran against our patched `src/` mounted over
`ari-oncall-agent-worker:test` on the bastion, on its own task queue
(`oncall-ari-queue-livetest`) so it never competed with the deployed
worker, pointed at the test cluster's Temporal.

**The fourth defect: a change whose content is a NEW file was invisible,
so this agent could not ship one at all.** `changed_files` is `git diff
--name-only HEAD` and `diff` is `git diff HEAD` — neither can see an
untracked path. Confirmed live (workflow `979a2b0e`, against
`OnCall-ARI-InfraCore-testing@dev`): asked for a `CONTRIBUTING.md` the
repository did not have (GitHub API: 404), opencode called `write` (tool
trace `read 2, glob 1, grep 1, write 1`), the file was created — and the
pipeline reported the working tree unmodified. The retry called `edit` on
that same still-untracked file and said the same thing.

This is pre-existing, but the empty-tree guard above made it *lie*:
before the guard it surfaced one step later as `plan_pr`'s
"generate_change reported no changed files to publish", after paying for a
full verify run; with the guard it fails fast and names a cause that is
false. "Add a new test / module / config file" is a large share of real
fixes, so this was worth fixing rather than documenting.

Fixed with `git add -N` on exactly the files that appeared during this
`generate_change` call: `untracked_files()` is snapshotted before the
opencode call and again after, and only the difference is staged. The
before/after snapshot is what keeps verify's scratch files out on an
`on_failure` loop back — those are already untracked when the second
generate_change starts, so they are never "new". Measured first rather
than assumed: `git add -N` puts the path into `git diff --name-only HEAD`
*and* renders the whole file as additions in `git diff HEAD`, while
`--exclude-standard` keeps `.gitignore`d paths out. The diff mattering as
much as the file list is the point — `propose_pr`'s `key_hunks()` reads
it, so without this a human would approve a new file whose contents the
approval email never showed them.

**The default pipeline budget is 3000s, not 3600s, and the caller is why.**
MCP Service's `ONCALL_TASK_AGENT_TIMEOUT_S` is 3600s on test, and its
`agent_timeout_s()` docstring says plainly that "the task path sets no
deadline inside the agent itself" — so this budget is the only in-agent
bound there is. At 3600s the step loop would finish at the exact moment
the caller gives up, and cleanup/resolve/respond add ~360s after it, so
the answer would arrive at ~3960s to nobody. MCP also sends
`"options": {}` (`app/oncall/ari_oncall_agent.py`), so the constant is
what actually runs — the per-request override never fires in production.

**Live results** (test cluster, 2026-10-09):

| scenario | result |
|---|---|
| `card` | `skills=['code_analysis_fix']` |
| `budget` | FAILED at 131s against a 120s budget. History (`613127ee`): `verify` was scheduled with `stc=44.86s`, not the 480s its own `timeout_seconds` asks for, and was cut at exactly 120.0s; `respond` still ran and explained why |
| `empty` | FAILED at 76s with no `root_cause` artifact; the workflow result's `data` held `clone_repo` alone, so `verify` never ran |
| `propose` | 56s, `INPUT_REQUIRED`, branch `ari/...-b17f2793` confirmed pushed via the GitHub API, `pending_action` carrying 597 characters of key hunks |
| `confirm` | `COMPLETED` in **5s**, PR #13 opened — `added CONTRIBUTING.md +40/-0` |
| `reject` | a confirm carrying an id the task never proposed → FAILED in 0s with a clear message |

`confirm` closes **§5c's round trip, which this file has recorded as
unverified since 2026-09-18**. Its 5 seconds are the "push the branch
before the pause" decision paying off: `publish_pr` had only an API call
left to make. And PR #13 being an `added` file is the fourth defect's fix
proven end to end — that change could not have been shipped at all an
hour earlier.

**Three things the live script itself got wrong, worth not repeating:**

1. `empty` first reported INCONCLUSIVE on a run that had worked perfectly,
   because it string-matched `respond`'s prose for "unmodified".
   `respond` is an LLM writing for a human and paraphrased the guard
   differently on each of two runs ("didn't actually modify any files",
   then "didn't actually modify the working tree").
   `docs/INPUT_REQUIRED_INTEGRATION.md` says this outright — "task.status.
   message's text is for a human to read, not for your code to parse" —
   and points at the signal that works: `root_cause` resolves from
   `generate_change.root_cause`, so a `generate_change` that raised leaves
   it absent. Assert on artifacts and state, never on the message.
2. `reject` ran before `confirm` and shared its paused task. A confirm,
   valid or bogus, moves that task to a terminal state and A2A has no way
   back, so `confirm` died on a bare HTTP 400 that said nothing about the
   cause. Each scenario now pauses its own task.
3. The budget's workflow tests cost 364s before they cost 4s. Temporal's
   time-skipping environment only skips while **nothing is running**, so
   an activity that blocks for the budget burns that budget in real
   seconds. Driving the clock with retry backoff is instant instead — but
   Temporal will not sleep past an activity's `schedule_to_close`, so a
   backoff longer than the remaining budget ends that attempt without the
   clock moving and the budget is never spent. Small backoff, many
   attempts.

**Verified:** full suite **281 passed** in
`ari-oncall-agent-worker:test`, plus the six live scenarios above. 7 more
cases for the new-file fix (3 in `test_capabilities.py`, including one
pinning that an already-untracked file is *not* treated as part of this
change; 4 in `test_git_ops.py`).

**Still not verified:** the approval *email* itself, and MCP Service
driving this pause end to end. `scenario_confirm` sends exactly what
`app/tasks/a2a_resume.py:confirm_message` builds (same `task_id` +
`context_id`, original config DataPart plus `confirm_pending_action`), so
the agent side of that contract is now exercised — what remains is UIE
rendering the mail and MCP relaying the click.

**A limit worth knowing before trusting email approval:** the A2A
`TaskStore` is in memory, and `_find_pending_action` scans
`context.current_task`, so an API pod restart loses every pending
approval. `publish_pr` has no staleness window of its own (the 30-minute
check the integration guide describes was `execute_action`'s, and that
kind is gone), and MCP waits `ONCALL_TASK_APPROVAL_TIMEOUT_S` = 86400s —
24 hours. So the protocol permits approving a day later while the pod's
lifetime decides whether it works. Untested, and email approval is by
nature the slow-human case.

### Same day, one hour later — the empty-tree message was accusing an honest model

The guard above shipped with the wording *"opencode reported a change but left
the working tree unmodified"*, written against the only case we had seen: a
model that fabricated a change. An hour after it went live on stg a second
case appeared, and the wording is false for it. opencode read the repository,
correctly concluded nothing needed changing, and said so:

```
Tool calls: {'read': 1}
'Verified that a README.md file exists in the repository root directory. No
 changes were required as per the instructions to only create a minimal README
 if none exists.'
```

It reported no change. The message told whoever read the failure to go looking
for a lying model that was not there.

**Still a failure, and that part is not in question** — this step is only ever
scheduled to produce a change, and distinguishing "lied" from "correctly
declined" would mean trusting the model's account of itself, which is the one
thing this guard exists because we cannot do. What changed is only the wording:
state the tree, the tool calls and the account verbatim, and let a person
judge. `test_the_empty_tree_message_does_not_accuse_an_honest_model_of_lying`
pins both halves — the guard still fires, and the message contains neither
"reported a change" nor "claimed".

Worth generalising: an error message written from one observed instance tends
to encode that instance's *cause* rather than its *symptom*. The symptom here
("the tree is unmodified") was always safe to state; the cause ("it claimed
otherwise") was only true once.

**Verified:** 282 passed in `ari-oncall-agent-worker:test`.

### Same day — the worker was pid 1, and pid 1 was not reaping

Found by looking at the bastion rather than at any test output: a worker up
3h35m had 15 `[git] <defunct>` processes, all parented to
`python -m api.worker` running as pid 1 in its container. All 15 disappeared
the moment that container was removed, which is what confirmed the parent.

**The first explanation was wrong and is worth recording as such.** The obvious
suspect was `git_ops._communicate`, which on timeout calls `proc.kill()` and
raises without ever awaiting `proc.wait()`. That reads like a leak, but it is
not one: asyncio's default `ThreadedChildWatcher` runs a thread per subprocess
that blocks in `os.waitpid()`, so anything *we* spawn is reaped regardless.

The real chain has nothing to do with our subprocess handling. **opencode
spawns its own `git` children.** When an activity is cancelled or times out and
opencode goes away, those grandchildren are orphaned and reparented to pid 1 —
and `CMD ["python", "-m", "api.worker"]` with no ENTRYPOINT makes the worker
itself pid 1. Python reaps only the pids its watcher knows about, so an
inherited orphan is never waited on.

Fixed with `tini` as `ENTRYPOINT` in `Dockerfile.oncall` (`/usr/bin/tini`,
0.19.0 in `python:3.12-slim`, path verified in the base image before writing
it). Reaping from Python instead would mean a SIGCHLD handler calling
`os.waitpid(-1, ...)`, which races the watcher threads for the exit status of
processes we *do* own and makes `proc.wait()` raise `ChildProcessError` — an
init is the mechanism built for exactly this.

`test_the_worker_image_runs_an_init_as_pid_1` guards it, because the failure is
silent, cumulative, and structurally invisible to the rest of the suite: every
other test asserts on what one call returns, and this one only ever shows up in
`ps`. Same blind spot the live scenarios have — they are per-request too.

Deployed workers showed 0 zombies when checked, but they had been restarted
1–2 hours earlier and served a dozen runs; that is not evidence of absence.

**Verified:** 283 passed in `ari-oncall-agent-worker:test`. The image change
itself only takes effect on the next build.

**Correction, same day:** the tini ENTRYPOINT above is **inert in every
deployment that uses the `oncallari` chart**, which is all of them. Kubernetes
maps a container's `command:` onto the image's ENTRYPOINT and replaces it, and
that chart hardcodes `command: ["python", "-m", "api.worker"]` in all six of
its deployment templates with no values hook. Verified live on stg right after
deploying the fix: the image reported
`Entrypoint=[/usr/bin/tini --] Cmd=[python -m api.worker]` while the running
container's `/proc/1/cmdline` was `python -m api.worker`.

So the image half is done and the zombies are **not** fixed in production. The
remaining half is a change to `insightfinder/charts` (drop the `command:`
override, or make it `["/usr/bin/tini", "--", "python", "-m", "api.worker"]`),
which needs its own Chart.yaml version bump.

`test_the_worker_image_runs_an_init_as_pid_1` now says this in its own body,
because a green test that only checks the Dockerfile reads as "zombies fixed"
and is not. Worth generalising past this instance: a guard that asserts on an
artifact this repo produces says nothing about how another repo deploys it, and
the gap is invisible precisely because the assertion passes.

**Resolved, 2026-10-10:** `insightfinder/charts` commit `004bb1b` deletes the
`command:` line from `ari-oncall-agent-worker-deployment.yaml`, published as
chart `0.3.1`, and `test-fluxcd` pins both ARI releases to it. Verified on test
and stg after the upgrade: `/proc/1/cmdline` is now `/usr/bin/tini --`.

Only that one template was changed, deliberately. The other five overrides are
still there and are fine: the three API deployments run `uvicorn` and spawn
nothing, and the action worker's image carries no opencode, no git and no build
toolchain, so neither has orphans to inherit.

## 2026-10-10 — the approval email said "Custom action"

Reported from stg (Ayush, after the first live approval mail): the email's
**Proposed action** read *Custom action* instead of naming what the agent
wanted to do.

Not a UIE bug. MCP Service's `approval_labels()`
(`app/tasks/loop.py`, `origin/test`) resolves that label by scanning the paused
task's artifacts for `data.action_name`, and returns `CUSTOM_ACTION_LABEL` when
no artifact names the action. Our `pending_action` carried `action:
"open_pull_request"` — the machine identifier `publish_pr` checks — and nothing
a person reads. The action agent's own `pending_action`
(`ari-agent-demo`, `propose_action.py`) carries both; this one carried one.

Fixed by adding `"action_name": "Open pull request"` beside `action`. Two
constraints in that function, both now asserted by
`test_proposal_names_the_action_for_the_approval_email`: the name must be
non-empty, and it must not equal the agent's display name — `approval_labels()`
skips that case on purpose ("the agent's name is never sent as the action"), so
`"ARI On-Call Agent"` would have produced *Custom action* all the same.

Chosen as the kind of action, not the pull request's title: the title, root
cause, changed files and key hunks are all already in the body of that mail,
and this is a one-line label in a table.

Path verified end to end in source rather than inferred, since the label is
produced two repos away: `propose_pr` dict → `_publish_result`'s
`add_artifact` data part → MCP `interrupt_payload()` `{"name", "data"}` →
`approval_labels()` reading `data.action_name`.

## 2026-10-10 — the approval window is now 3 hours

`ONCALL_ARI_APPROVAL_WINDOW_SECONDS` was 1500 (25 minutes), which expired
before an on-call engineer had read the mail. Raised to 10800 in `test-fluxcd`
(`values/uie.yaml`) and `eks-fluxcd` (`apps/stg/insightfinder-values.yaml`).

Nothing in this repo had to change, and that is worth recording because the
reason is not obvious: `publish_pr` accepts a confirmation by proposal id with
**no age limit**. UIE's own javadoc justifies the 1500s default as "kept under
the remote agents' own limit: the Jenkins agent refuses a confirmation older
than 30 minutes (`_MAX_PROPOSAL_AGE_SECONDS`)" — that agent is decommissioned
and its code is no longer on this branch, so the constraint it documented is
gone with it.

Downstream the window only widens deadlines: UIE's `isAbandoned()` budget is
investigation timeout **plus** approval window, and MCP derives
`work_timeout_s = timeout_seconds + approval_timeout_s` per request, so neither
clips the other.

The real cost, unchanged by this but three hours wider: the paused A2A task
lives in the API pod's `InMemoryTaskStore`. An API restart inside the window
loses it, and the confirmation then fails with "no such pending action". One
replica, so no split-brain — just the restart.

## 2026-10-10 — four fixes from one stg run, three of them already known

A single sweep-triggered run on stg (workflow `5a0f4506`, image
`2610091640-staging`, the first on the new build) spent 50 minutes and never
reached `propose_pr`. Picking it apart found four separate faults. Three of
them were already written down somewhere in this repo and had simply never
been paid for.

### 1. `opencode_runtime`'s `communicate()` had no deadline

17:09:05 the opencode call returned normally and logged `opencode_call`
(328s, 33 tool calls). The activity then sat in `proc.communicate()` until
17:23:47, when Temporal's 1200s `start_to_close` fired `CancelledError:
Activity cancelled`. Inspected while stuck: **no opencode process anywhere in
the container** (pid 1 tini, pid 7 the worker, nothing else), all eight
threads in `futex_wait`/`ep_poll`, CPU 1m, memory flat at 315Mi for the whole
twelve minutes. Attempt 2 ran the same step on the same image in 93s.

That is, symptom for symptom, the race `git_ops.py`'s own module comment
describes against `git diff` — and the guard written for it in 2026-10-05
was applied to `git_ops` only. **The regression test was the reason nobody
noticed**: `test_every_git_call_goes_through_the_deadline_helper` read
`inspect.getsource(git_ops)`, one module, so it was structurally incapable of
seeing the bare `await proc.communicate()` in `common/opencode_runtime.py`.
It was green the entire time.

Fixed with the same shape of guard, and the test now walks every `.py` under
`src/` instead of one module. The deadline is derived per call rather than
fixed, because the callers are an order of magnitude apart: a healthy
`generate_change` turn was measured at 328s while `verify`'s whole step
budget is 480s. `StepContext.step_deadline_seconds` carries the activity's
**effective** `start_to_close` (Temporal clamps it to the remaining pipeline
budget, so capabilities.yaml's `timeout_seconds` is the wrong number), minus
a margin, floored at 60s.

Worth generalising: a guard that only looks where the last bug was cannot
catch the next one. If a rule is worth asserting, assert it over the whole
tree.

### 2. Nothing heartbeated

Recorded as a known gap **twice** before it cost anything — "Nothing
heartbeats… a `heartbeat_timeout` would detect a stall in ~2 minutes instead
of 20", and "there is no `activity.heartbeat()` anywhere in this codebase, so
Temporal cannot deliver cancellation". Both left open deliberately.

What it cost in one afternoon: the worker was OOMKilled at 17:29:05 and
`verify` was not retried until 17:33:20 (4m15s of a 50-minute budget), and in
workflow `c0296796` a replaced pod cost the full 20 minutes, because
`generate_change`'s `start_to_close` is 1200s. Both gaps are pure dead time
waiting on a worker that was already gone.

`execute_step` now beats every 20s with `heartbeat_timeout` at 90s — four
intervals of headroom, because stg runs on t3/t3a where 50–70% hypervisor
steal has been measured and a briefly starved worker must not be declared
dead. This also opens the channel Temporal delivers cancellation on, so a
timed-out attempt's opencode subprocess no longer keeps running in the pod
after the workflow has moved on.

### 3. The sandbox belongs to a pod; a Temporal activity belongs to none

Workflow `c0296796` failed with `FileNotFoundError: .../sandbox/c0296796-…/repo`
and reported to the caller that "the cloned repository directory became
inaccessible" — true, and useless. The activity identities in its history say
exactly what happened:

```
 5  16:42:56  prepare_workspace   worker=…766fc5765-tr59k    attempt=1
17  16:42:59  clone_repo          worker=…766fc5765-tr59k    attempt=1
23  16:43:04  generate_change SCHEDULED
24  17:03:14  generate_change STARTED  worker=…7c4c9499dd-qq677  attempt=2
25  17:03:14  FAILED  FileNotFoundError .../sandbox/c0296796-.../repo
```

`clone_repo` ran on one pod, a deployment roll replaced it (`strategy:
Recreate`, `sandbox` is an `emptyDir`), and the retry landed on another.

Half of this was already known: `propose_pr`'s module docstring pushes the
branch before the pause precisely because "the sandbox root is not on a
persistent volume at all". That reasoning covered losing the sandbox *across
the approval* and never asked what happens *between two steps of one run*.

`execute_step` now re-clones when the recorded `cloned_path` is no longer a
directory. Healing rather than persisting the sandbox on purpose: not
depending on pod affinity is the property actually wanted, and a PVC would
only move the assumption somewhere harder to see. The re-clone is cheap
because the shared mirror already is PVC-backed. Deliberately not
`require_cloned_repo`'s job — that answers "did the planner include
clone_repo", a planning question, and it runs after the capability has
already started.

### 4. `verify` OOMKills the worker at the chart's 2Gi default

Three kills in eighteen minutes, every one mid-`verify`, which runs the
target repository's own build tooling inside the container. Memory went
981Mi → 1154Mi → killed inside one two-minute scrape window, so the peak is
above 2Gi and still unmeasured. `charts/oncallari/values.yaml` already called
these "the frequent OOMKilled restarts this worker was already hitting" in
its `repoCache` comment — that change moved the cache out of their way and
left the limit alone. Raised to 6Gi/1Gi in chart `0.3.2`.

### What the same run proved working

Incidentally, and worth recording because the evidence is unusually clean:
the II-25090 pipeline budget held under four `generate_change` → `verify`
cycles. `scheduleToCloseTimeout` shrank 2996 → 2989 → 1685 → 1205 → 1169 →
689 → 652 → 172 → 134s across the steps, and the run ended at **50m03s against
a 3000s budget** with Temporal's "Not enough time to schedule next retry
before activity ScheduleToClose timeout, giving up retrying", then ran
`respond` and completed cleanly. Without it this would have looped
indefinitely — which is the shape of the 45-minute run that was reported
against this ticket in the first place.

`tini` is also confirmed live now that chart 0.3.1 dropped the `command:`
override: `/proc/1/cmdline` on stg is `/usr/bin/tini -- python -m api.worker`.

**Verified:** full suite **297 passed** (from 284) in
`ari-oncall-agent-worker:test` with `src/`, `tests/`, `configs/`, `docs/` and
`scripts/` mounted over it. 13 new cases: 4 for the opencode deadline and its
derivation, 1 rewriting the bare-`communicate()` guard to walk the whole
package, 4 for the heartbeat (that it beats, that it stops on success, that
it stops on failure, that the step is told the activity's own deadline), and
3 for the re-clone (lost checkout is re-cloned and the step sees the fresh
path, a present checkout is left alone, a plan without `clone_repo` is left
alone).

## 2026-10-10 — `verify` was silently throwing away finished work

With the memory limit raised, `verify` stopped being OOMKilled and became the
single reason runs failed: **4 of 4 attempts on stg runs `fc532091` and
`1c2396a4`, 3 of 4 on `8b650147`**. Two separate defects, one in the control
flow and one in the cache, and the first is the one that was blocking people.

### 1. An exhausted `on_failure` loop discarded the change

`verify`'s policy ended `then: fail_needs_human`, and workflow.py's handler
for that sets `final_status = "failed"` and **`break`s the step loop** — so
`propose_pr` never ran. The change had already been generated and was sitting
in the sandbox; it was never committed, never pushed, never proposed, and the
caller was told the investigation failed. Nobody saw the diff.

That is the wrong trade here. The agent does not merge anything; `propose_pr`
pauses for a person who reads the diff. An unverified change, **labelled**
unverified, is strictly more useful to that person than nothing — and they can
run the build themselves, which is precisely what the agent could not finish.

`on_failure.then` now also accepts `continue`, which records the failure and
carries on to the next step, and `verify` uses it. `fail_needs_human` is
unchanged for any step whose failure really should stop everything.

Because a proposal can now carry an unverified change, `propose_pr`'s
`pending_action` gained a `verification` field — `passed` / `failed` (with up
to 600 chars of output) / `skipped` / `not run`. Without it this change would
have made the gate *weaker*: an approver who is not told would be deciding on
a false impression. The four outcomes are deliberately distinct; collapsing
"never checked" into "failed" would cry wolf on every plan that omits verify.

### 2. Maven's cache was never on the persistent volume

`reserve_build_cache_home()` sets `HOME` to the build-cache PVC for verify's
opencode call. npm and Go honour that. **The JVM does not** — on Linux OpenJDK
reads `user.home` from the passwd database, not the environment:

```
$ HOME=/var/lib/ari-agent/build-cache java -XshowSettings:properties -version
  user.home = /root
```

Measured in the stg worker the same day: `/root/.m2/repository` held **1.5 GB**
downloaded inside a 25-minute-old pod, while the PVC held only `.npm`. So every
fresh worker paid a full cold Maven download, and on a large Java repo that is
what spent verify's entire 480s budget — the cache that was supposed to prevent
exactly this had been inert for Maven since it was written.

Fixed with `-Dmaven.repo.local` (Maven's own override, which does not go
through `user.home`) and `GRADLE_USER_HOME`, in a new
`opencode_cli._build_cache_env`. `MAVEN_OPTS` is appended to, not replaced, so
Dockerfile.oncall's `-Xmx2g` survives.

Worth keeping in mind generally: `HOME` is not a reliable way to relocate a
build tool's cache. Check each tool's own documented variable.

**Verified:** full suite **307 passed** (from 297) in
`ari-oncall-agent-worker:test`. 10 new cases: 4 on the proposal's
`verification` field (failed/passed/not-run vs skipped/truncation), 2 on the
workflow loop (`continue` reaches the later steps, `fail_needs_human` still
stops), 4 on the build-cache env. One existing case was rewritten: it asserted
`extra_env == {"HOME": ...}`, which pinned the half-working behaviour.

## 2026-10-10 — an unfinished verify and a failed build are not the same thing

The `then: continue` added earlier today let the pipeline reach `propose_pr`
after `verify` exhausted its retries. It treated two different outcomes
identically, which was wrong:

- the build ran and reported `RESULT: FAIL` — the change is broken, and
  proposing it wastes the reviewer's time and makes the gate weaker than it was;
- verification never completed (subprocess deadline, cancel, heartbeat
  timeout) — nothing was learned about the change, so "failed" does not mean
  "broken".

They were already distinguishable at the source and were being flattened:
`verify.py` returns `{"status": "failed"}` for a real `RESULT: FAIL`, while a
killed activity raises and `workflow.py`'s `except ActivityError` manufactured
the same shape. That branch now produces `{"status": "inconclusive"}`, the
`on_failure` loop retries on either, and `then: continue` applies only to
`inconclusive`. A genuine `failed` ends the run as it always did.

`pending_action.verification.status` therefore has five values the approver can
tell apart: `passed`, `failed`, `inconclusive`, `skipped`, `not run`.

Every verify failure seen in production so far has been the second kind
(`opencode (ari-verify) timed out after 435s and was killed`), which is why
continuing was right in practice — but the first kind must not inherit it.

**Verified:** full suite **311 passed**, from 308. Three new cases: a real
`RESULT: FAIL` stops the pipeline and never reaches `propose_pr`; a killed
activity reaches it; and the proposal reports `inconclusive` separately from
`failed`. One existing case was changed to raise rather than return
`{"status": "failed"}`, since it was written to exercise the continue path and
that path is now reachable only by the raising one.

## 2026-10-10 — a paused approval no longer dies with the API pod

Two incidents were awaiting approval on stg when the `ari-oncall-api` pod
restarted (our own chart 0.3.3 rollout). Both approvals then failed:

```
09:24:38 [OnCall-Task] A2A resume: confirming pending action 6c6c8ed4... on task dbc75291...
09:24:38 [OnCall-Task] A2A resume failed: ARI On-Call Agent rejected the confirmation:
         POST /message:send returned HTTP 404: Task dbc75291...
```

The paused A2A task lived in `InMemoryTaskStore`, so the restart lost it. UIE
had already recorded the decision as `approve`, so the record read as approved
while nothing was published — and mcp-service's fallback for a 404 is to start
a **fresh investigation**, which re-ran the whole pipeline and produced a
second branch for one incident.

That was tolerable at the old 1500s approval window. At 10800s (raised earlier
the same day) any deploy, eviction or node roll inside three hours does it.

`common/a2a_server/task_store.py` adds a file-backed store: the in-memory store
is still the hot path, every save also writes the task's protobuf bytes to
`TASK_STORE_ROOT`, and a `get` that misses memory falls back to disk. Chart
0.3.4 mounts a 1Gi PVC on the API pod for it.

Deliberate properties, each one a failure mode that must not become a worse
one than the bug being fixed:

- **Off by default.** An unset `TASK_STORE_ROOT` returns the plain
  `InMemoryTaskStore`, so nothing changes for a deployment that does not mount
  the volume.
- **A write failure never fails the request.** A full or unwritable volume
  degrades to memory-only; it must not turn into a 500 on an approval.
- **A corrupt file reads as absent**, not as an exception.
- Writes are tempfile + `os.replace`, so a restart mid-write cannot leave a
  half-written task that then fails to parse.
- Tasks are pruned at startup past `TASK_STORE_MAX_AGE_SECONDS` (7 days), since
  the volume is small and nothing else ever deletes a finished task.

Owner scoping is preserved by reusing the SDK's own `resolve_user_scope` and
keying files `<owner>/<task_id>.pb` — the in-memory store is keyed by owner
first, and a store that ignored that would let one caller read another's task.

**The other half is not fixed and is not in this repo:** mcp-service re-runs
the investigation on a 404 instead of reporting that the approval could not be
honoured. Persisting the task makes that path rare, not impossible.

**Verified:** full suite **318 passed**, from 311. Seven new cases in
`tests/test_task_store.py`: a task survives a new store over the same
directory, an unknown id is still None, delete removes the file, a write
failure still serves from memory, a corrupt file is ignored, an unset root
keeps the in-memory store, and an uncreatable root falls back instead of
crashing. `OwnerResolver`/`resolve_user_scope` live in
`a2a.server.tasks.inmemory_task_store`, not `task_store`, and
`resolve_user_scope` reads `context.user.user_name` — both found by running
against the installed SDK rather than assuming.

## 2026-10-12 — persisting a task was only half of it: a running one was orphaned

Round four of the stg E2E found the other half of the lost-approval bug, and
it is in this repo. The previous day's `FileTaskStore` fixes a task that was
**already paused** when the API pod went away. It does nothing for a task that
was **still running**, because the thing that writes the pause has itself died.

`a2a_server/executor.py` drives the whole lifecycle from one in-process await:

    handle = await client.start_workflow(..., id=context.task_id, ...)
    result = await handle.result()
    ...
    await _publish_result(updater, result)

Only `_publish_result` writes the artifacts and the terminal/`INPUT_REQUIRED`
state. Everything before it — `enqueue_event(Task(...))`, `start_work()` — has
already been saved, which is why an orphan is left sitting in WORKING.

Measured on stg, task `86a04bdd-2fb0-4669-96bc-03c3e2b1e889`:

    23:25        task saved as WORKING (93 bytes on the PVC)
    23:31        API pod replaced; this coroutine dies with it
    23:43-23:49  the Temporal workflow carries on and verify finally passes
    23:49:51     propose_pr pushes ari/ari-fix-rejectedexecutionexception-with--86a04bdd
    23:49:53     respond; workflow status COMPLETED
    later        the .pb file is still 93 bytes, WORKING, zero artifacts

So the branch is on GitHub with a real two-file fix, the workflow finished
cleanly, and the approval is unreachable: `_find_pending_action` resolves a
confirmation against the task's own artifacts, and there are none.

`a2a_server/reconciler.py` re-attaches at startup. `FileTaskStore.unfinished()`
returns every persisted task still in SUBMITTED/WORKING; for each, the
reconciler takes the workflow handle (the workflow id *is* the task id) and
awaits its result, then republishes it. It runs from a FastAPI lifespan task,
never blocking readiness, since `handle.result()` on a still-running workflow
blocks for as long as the run takes — which is exactly the point.

Nothing about the publish is reimplemented: `_publish_result` is called
unchanged against an `EventQueue` subclass that only collects, and the
collected events go through the SDK's own `TaskManager.process`, which is what
persists them for a real request too.

Four things checked against the installed a2a-sdk (1.1.2) rather than assumed:

- `TaskUpdater` touches nothing on the queue but `enqueue_event`, so a
  collecting subclass is sufficient. `EventQueue`'s own docstring names a
  custom subclass as the supported way to run an executor outside the
  framework; `EventQueueLegacy` is deprecated and was not used.
- `TaskManager.process` -> `save_task_event` -> `_save_task` is the same
  persistence path a request takes.
- A bare `ServerCallContext()` resolves to owner `''`, which is what
  `ApiKeyCallContextBuilder` produces too — hence the flat `<task_id>.pb`
  files at the root of the stg volume, and hence a reconciled task is written
  back where the request would have written it. An owner subdirectory is
  handled by rebuilding the context from the directory name.
- Annotating `unfinished()` as `-> list[...]` raises `'function' object is not
  subscriptable` at import: `FileTaskStore` already binds `list` as a method
  (the `TaskStore` ABC requires it), and a class-body annotation is evaluated
  in the class namespace. Quoted instead. This would have crashed the API on
  startup; the new tests caught it before deploy.

### `verify`'s timeout had no margin

The same run showed `verify` is not hopeless on InfrastructureCore, only
starved: attempts at 435s and 435s were killed, the third finished in 404.32s
and returned PASS. The GitHub Actions job that does strictly more work
(`mvn -B clean test` plus the powermock profile plus setup) ran 4.2-7.0 minutes
across its last twelve runs. 435s was simply too close to the real cost.

`timeout_seconds` 480 -> 900, and `on_failure.max_attempts` 3 -> 2 with it: the
pipeline budget is 3000s, so three 900s attempts would leave nothing for
propose_pr and turn a slow verify into no proposal at all, which is worse than
the bug. Two attempts bound verify at ~31 minutes and leave ~19.

The comment above `timeout_seconds` still warns about callers' "~8-10 minute
patience timeout". That was written 2026-09-21 and no longer binds: mcp-service
polls `GET /tasks/<id>` every five seconds rather than blocking on the send,
and the deadline it reported for this run was 20115s.

**Verified:** full suite **326 passed**, from 318 — run on Linux inside
`ari-agent-api:2610100951-staging`, because four modules import `fcntl` and
cannot run on Windows at all. Eight new cases in `tests/test_reconciler.py`:
an orphaned task gets its `pending_action` persisted, that artifact is the one
`_find_pending_action` resolves against, a finished task is never re-run, an
unreadable workflow leaves its task alone, one bad workflow does not stop the
others, an in-memory store is a no-op, an owner-scoped task is written back
under its owner, and a missing Temporal frontend degrades instead of crashing.

**Still not in this repo:** mcp-service abandons a task permanently when the
connection drops. It stopped polling `86a04bdd` at 23:31 and never came back
once the new pod was serving 56 seconds later, reporting "lost connection
before opening a pull request" as a final answer. Until that retries, this
reconciler restores the task but nothing asks for the approval.

## 2026-10-13 — A platform-neutral chat gateway and an `ari` CLI (II-25116, PR 1 of 5)

II-25116 asks for three things on top of a cleanup: users talking to the
agent from Slack and Teams, users talking to it from a terminal, and
Agent Skills as the way outsiders extend it once it is open source. This
entry covers the first slice, which the other two chat surfaces build on:
`src/chat_gateway/` (the bridge) and `src/ari_cli/` (its first consumer).
The plan for the whole ticket, with the cleanup list and the reasons for
the order, lives outside the repo for now.

**Nothing in the agent changed.** The gateway is a *caller* of the A2A
surface this repo already serves, holding its own credentials and its
own in-memory thread-to-task map, exactly as MCP Service does. The only
pre-existing file touched is `pyproject.toml` (two packages in the wheel,
a `cli` extra, a console script). That was deliberate: another branch was
mid-flight in the same files this week, and a caller cannot conflict with
the server.

**Why a bridge and not three bots.** The parts that differ between Slack,
Teams and a terminal are how a message arrives, how a reply is posted in
a thread, and how a button is drawn. Everything else — building the
`DataPart`, polling, the `INPUT_REQUIRED` pause, who may approve, what a
follow-up means — is identical and has to be right once. So `bridge.py`
owns that, `rendering.py` turns a Task into platform-neutral text plus
abstract buttons, and the CLI is the cheapest adapter: it proves the
bridge end to end without a Slack workspace.

**What the bridge decides, and why:**

- `message:send` is **never retried** (`a2a_client.py` passes
  `max_attempts=1` to `http_retry`). It creates a task; a retry after a
  lost response would clone, run the LLM and push a second branch. Reads
  and `:cancel` keep the shared retry policy.
- A repository URL is accepted only on an **allowed host** (`github.com`,
  `ALLOWED_REPO_HOSTS`, and the hosts of `DEFAULT_REPO_URL`/`REPO_MAP`),
  `https` only, no embedded credentials. The gateway attaches its
  `GITHUB_PAT` to every repo it sends and `git_ops.authed_url` embeds it in
  the clone URL for *any* host, so a `repo=https://evil.example/x` typed
  into a channel would hand the organisation's token to that host on the
  first 401. Checked at the one choke point (`GatewaySettings.repo_config`)
  that both the chat path and `ari approve` go through.
- The **requester is per task**, not per thread; whoever starts a task
  may approve or decline it, plus a configured `APPROVERS` list. While a
  proposal is waiting, only those people can continue the task at all,
  because a continuation that ends without a new proposal makes the old
  one unconfirmable (a terminal task refuses further `message/send`).
- A bare typed `yes`/`ok`/`approve`/`no`/`decline` while a proposal waits
  is treated as the decision, never sent as text. The agent ignores
  free-text confirmations by design (`docs/INPUT_REQUIRED_INTEGRATION.md`
  §5), so sending them would silently burn the task.
- Any other message while a proposal waits **continues the same task**
  (same `task_id`), because `executor._extract_history` reads history off
  the current task only; a new task in the same `context_id` would give
  the planner no conversation at all. The cost is stated to the user when
  the follow-up ends without a new proposal.
- `approve` re-reads the live task before confirming and refuses when it
  is no longer `INPUT_REQUIRED`; the latest `pending_action` artifact
  wins when a task carries two (a follow-up that proposed again).
- A button still works after the gateway lost the thread (TTL or restart)
  for a configured approver, by rebuilding the config from settings — the
  agent side already persists paused tasks (`TASK_STORE_ROOT`), so the
  gateway should not be the weaker half.
- A cached "still working" state is re-checked against the agent before
  refusing a new message, so a client-side wait timeout cannot block a
  thread forever.

**CLI specifics worth knowing:** config precedence is flags, then
environment, then `~/.config/ari/config.toml`; no dotenv file is read.
`config set` without a value prompts (no echo for secrets), validates
through the settings model, and writes the file `0600`. `tasks get`
redacts `api_key`/`pat`/`ssh_key`/`token`/`password` values at any depth,
since a Task can carry the request `DataPart` in its history. `ari
approve` requires `GITHUB_PAT`: the resume is a lone `publish_pr` step
and a confirm without a token fails after the proposal is consumed.
Inside `ari chat`, recoverable errors are printed and the session goes
on; EOF at the approval prompt leaves the task addressable (the full id
is printed before the prompt).

**Review before this landed.** A five-lens adversarial review (wire
contract, secrets/authorisation, repo rules, runtime edge cases, test
quality) over the first draft produced 46 findings; 8 were confirmed by
two independent refuters and the rest were triaged by hand after the
review budget ran out. Fixed from it: the PAT-to-any-host hole, the
`message:send` retry, world-readable config, the first-artifact-wins
bug, a falsy `SessionStore` (`store or SessionStore()` dropped a
caller's empty store because `__len__` is defined), `.env.chat-gateway`
being read from the cwd and not gitignored (dotenv support removed
instead), the frozen per-thread requester, unauthenticated continuation
of a paused task, EOF/`Ctrl-C` at the approval prompt exiting 1, raw
tracebacks on an unreachable agent, a REPL that quit on any error, a
`--no-wait` path outside the error handler, `config set` accepting
values it cannot load, a TOML writer that emitted surrogate escapes,
Slack `<url|label>` and trailing punctuation in `repo=` values, a test
fake whose `wait` never reported the terminal task (the real one does),
and a CLI fake that served the post-confirm state before the confirm.
`tests/test_ari_cli.py` does `pytest.importorskip("typer")` because the
documented full-suite harness installs only `pytest`/`pytest-asyncio`;
a hard import would have failed collection for the whole suite.

**Verified:** 307 passed, 1 skipped (POSIX file-mode check on Windows) on
Windows, where 13 pre-existing modules cannot be collected (`a2a`,
`fcntl`). 99 new cases across six modules, plus the two source-level
`http_retry` guards still green over the new packages. The new code
imports neither `a2a` nor `fcntl`, so it runs on Windows.

**Verified on Linux and against the deployed test agent, 2026-10-14.**
Full suite **426 passed** inside `ari-oncall-agent-worker:test` on the
bastion, with `src/`, `tests/`, `configs/`, `docs/`, `scripts/` and the
Dockerfiles mounted over it. Then the package was installed into that
image (`pip install --no-deps .` plus `typer`), which proves the wheel
carries both new packages and the `ari` console script, and the CLI was
driven against `https://test-ari-oncall-agent.insightfinder.com` (the
public ingress; the kubectl session had expired, so not the
`svc/ari-oncall-api` port-forward MCP uses — same pods either way):

| step | result |
|---|---|
| `ari config show` | secrets masked to 4 characters; none of the three full values present in the output |
| `ari card` | `ARI On-Call Agent ['code_analysis_fix']` |
| plain question | `TASK_STATE_COMPLETED` in 6s, one-sentence answer, no repo work |
| `repo=https://evil.example/x` | refused client-side, exit 1, no request sent |
| `ari chat`, piped | answer in 7s, `/status` re-rendered the finished task, `/quit` exit 0 |
| propose (`.editorconfig` on `OnCall-ARI-InfraCore-testing@dev`) | `TASK_STATE_INPUT_REQUIRED` in 32s; proposal rendered with branch, base, files, `Verification: not run`, compare link and the new-file hunk; task `4e978738-ce56-4e81-a1bb-5bfb123ddf40` |
| `ari tasks get` on that task | `pending_action` + `root_cause` artifacts, `branch_pushed: true`; no secret value present (this server's `tasks/get` returned no request DataPart, so the redaction had nothing to do) |

`ari approve` was then run by a person, and it ran straight into a
rollout. Someone pushed `b7e3ce2` to `test` at 09:42:33Z (a `respond.py`
change); CI published `2610140942-test` at 09:43:28 (api) and 09:44:20
(worker), and Flux image automation rolled the agent while the approval
was being followed. The CLI printed the resume's first poll (`working`,
`produced: root_cause`) and then died on `tasks/get returned HTTP 503:
no available server` — Traefik's literal reply when a Service has no
ready endpoint — after `http_retry`'s three attempts, roughly three
seconds.

The agent side did its job regardless: **PR #23** on
`OnCall-ARI-InfraCore-testing` was created at 09:46:43Z and the task read
back as `TASK_STATE_COMPLETED` at 09:46:45 with `pending_action`,
`root_cause` and `pull_request` artifacts, from the new pod. That is the
full propose → approve → pull request round trip through the CLI.
Whether the old pod published the result during its termination grace
period or the new pod's startup reconciler did is **not established**;
the kubectl session had expired and the pod logs were not read.

The defect was in the client: a follow that gives up after three seconds
of 503 cannot survive the routine event of this environment, which
redeploys on every push to `test`. `AgentClient.wait` now treats a
retryable status or a connection failure as an outage rather than an
answer: it reports it once through `on_outage`, keeps polling, and gives
up only after `AGENT_OUTAGE_SECONDS` (600) of unbroken failure — never on
a 404, which means the agent really no longer knows the task. `ari tasks
follow <id>` was added to re-attach after any give-up. `message:send`
stays unretried: a confirm that hits the outage fails fast and leaves the
task paused, so re-running `ari approve` is safe.

**Verified after the fix:** full suite **433 passed** in
`ari-oncall-agent-worker:test` (7 new cases: an outage ridden out and
reported once, connection failures ridden out, a give-up after
`AGENT_OUTAGE_SECONDS`, a 404 still raised, the bridge forwarding the
outage, `ari approve` through a scripted outage, `ari tasks follow`), and
`ari tasks follow 4e978738-ce56-4e81-a1bb-5bfb123ddf40` against test
rendered PR #23 with exit 0. The outage path itself has not been seen
live yet; that needs a rollout to overlap a run again.

When repeating this on the bastion: the containers run as root, so a
test run leaves root-owned `__pycache__` in the mounted scratch directory
that the `test` account cannot delete. Pass
`-e PYTHONDONTWRITEBYTECODE=1`, and clean up with a container
(`find /work -user 0 -exec chown ...`).

One operational note for anyone repeating this on the bastion: `/work`
carries default ACLs that override `umask`, so a credentials file
written there with `umask 077` still came out `other::r--`. Set the mode
and strip the ACL explicitly (`setfacl -b`, `chmod 600`) on anything
holding a key.

## 2026-10-14 - `ari` gets an interactive chat for people (II-25116, step 1 follow-up)

Feedback after the first hands-on test, verbatim in substance: the CLI
"looks like a demo for developers, not something to hand to users". The
concrete evidence was a misread: `ari send "In one sentence, what is a
deadlock?"` printed the agent's answer bare under a progress line, and the
answer (a definition of being blocked) was taken for an error. Nothing
marked what was an answer and what was a failure, and using it at all
meant knowing subcommands, internal setting names and task ids.

Running `ari` on its own now opens a chat; the one-shot subcommands stay
for scripts and are documented as such (`docs/CLI.md`).

- **Chat first, menus only at decisions.** The main action is free text,
  which a menu cannot express; menus appear where there is a fixed set of
  choices. A proposal is a card (title, repository, branch to base,
  files, test status, compare link, diff excerpt) followed by an
  arrow-key menu: approve, decline, show the diff link, ask a follow-up,
  decide later. Full-screen TUI was rejected because it works badly over
  the SSH/bastion path this team actually uses and defeats copy-paste.
- **Every output says what it is.** Answers under `ARI ›` with Markdown
  rendered, errors as red `Error:`, failed tasks as `✖`, a spinner with
  elapsed time and what the agent has produced so far while it works.
  The agent does not report per-step progress (`executor.py` sends only
  SUBMITTED/WORKING and artifacts), so "cloning / editing / pushing" step
  names would need an agent change and are not shown.
- **First-run setup** asks for the agent URL and key, provider, model, LLM
  key, GitHub token and default repository, checks that the agent answers
  and writes the settings file `0600`. It is skipped when the environment
  already carries an agent URL and an LLM key, which is how the bastion
  wrapper and any scripted install run.
- **Slash commands** with Tab completion and history: `/repo`, `/branch`,
  `/base`, `/status`, `/approve`, `/decline`, `/new`, `/config`,
  `/setup`, `/quit`.
- **Two terminal backends behind one interface** (`ari_cli/terminal.py`):
  `prompt_toolkit` + `questionary` when attached to a terminal, plain
  prompts with numbered menus otherwise. The libraries are imported only
  for the terminal backend and live in the `cli` extra, so the agent
  images and the unit tests on Windows do not need them.
- `Rendered` now carries the raw task (`compare=False`), so the UI builds
  the card from structured fields instead of re-parsing rendered text;
  `bridge.start` keeps it when it appends the "earlier proposal" note.

**Verified:** full suite **450 passed** in `ari-oncall-agent-worker:test`
on the bastion, including `tests/test_ari_cli_tty.py`, which drives the
real terminal UI through a pseudo-terminal against a fake agent: the chat,
the arrow-key approval menu, Tab completion and the setup wizard (the
saved file checked for content and `0600`). 17 plain-mode chat cases in
`tests/test_ari_cli_chat.py`, 2 in `tests/test_ari_cli_session.py`
(Ctrl-C while waiting and at the prompt). Then a pty session against the
deployed test agent: banner, spinner, `ARI ›` answer, `/help`, Ctrl-D,
exit 0. The approval menu has been exercised against the fake agent only.

The pty tests warn that `forkpty()` runs in a process with a server
thread. Harmless here (the child execs Python straight away), but it is
why that fake agent is kept minimal.

### Same day - an adversarial review before handing it over

Three independent reviewers (conversation behaviour, terminal and platform,
secrets/setup/tests), each finding then attacked by two verifiers trying to
refute it: 15 confirmed, 0 refuted, 27 low-severity left unverified. Every
confirmed one is fixed, and most of the low ones. The ones worth knowing
about, because each would have reached a user:

- **`/base` broke approvals.** It appended `base=<x>` to every line, so a
  bare `yes` became `yes base=staging`, which `decision_word` does not
  recognise; the "approval" went to the agent as a follow-up and burned
  the proposal. `Bridge.start` now judges the text with directives
  removed, and only treats yes/no as a decision when a proposal is
  actually pending (a `yes` answering the agent's question is sent as
  text). The appended sentence is now a condition ("if this request leads
  to a pull request, open it against X") instead of an order, because the
  order turned plain questions into change requests. `base_hint()` in
  `chat_gateway/bridge.py` is the one place it is worded.
- **Approving something never seen.** `/status` printed only "a proposal
  is waiting", and `/approve` or `yes` would then confirm it. The chat
  now records the id of the card it last showed and shows the card again
  instead of confirming any other id; `/status` renders the card and menu.
- **Losing a proposal silently.** `/new`, `/setup`, `/quit`, Ctrl-D and a
  `/command` typed at the follow-up prompt all dropped a waiting
  proposal, and no screen ever showed the full task id needed to recover
  it. Leaving now prints the id with `ari approve <id>` or
  `ari tasks follow <id>`; slash input at the follow-up prompt runs as a
  command.
- **Typeahead could approve.** Keys typed while the spinner ran stayed
  queued and the arrow-key menu starts on Approve; a queued Enter picked
  it. Input is flushed (`termios.tcflush` / `msvcrt`) before every menu
  and confirm.
- **Setup wrote unusable or wrong configs.** Pressing Enter through it
  switched an OpenAI setup to Anthropic (the default came from a constant,
  not from what was in effect); "other" with a blank name or model was
  saved; a URL without a scheme was saved. Defaults now come from the
  effective settings, the LLM block is validated with `llm_config()`
  before saving, and `-` clears a value.
- **Secrets on screen.** An API key with a trailing newline (a pasted key,
  or a Secret made with `--from-file`) made h11 reject the header, and
  `http_retry` copied the header value into the error text, which the
  banner then printed. Secret fields are now stripped at the settings
  model, and `AgentClient` replaces a `LocalProtocolError` message with
  one that names no value. An `http://` repo URL carrying a token was
  echoed by the scheme check; credentials are now checked first. Typer's
  `pretty_exceptions_show_locals` is off, since setup frames hold keys.
- **Windows.** Piped stdout uses a code page, and `→`/`✔`/`✖` raised
  `UnicodeEncodeError` mid-proposal; stdout/stderr are reconfigured with
  `errors="replace"` when they are not a terminal. Piped setup hung,
  because `getpass` reads the console on Windows and `/dev/tty` on POSIX,
  not the pipe; secrets are read from stdin when it is not a terminal.
  `test_piped_setup_reads_every_answer_from_stdin_without_hanging` runs
  this as a real subprocess.
- **Agent text read as markup.** Rich parsed `app/[slug]/page.tsx` as a tag
  (the file list on the card lost `[slug]`), and `[/tmp/x]` raised
  `MarkupError`. Every agent string is now wrapped in `Text`.
- **Smaller ones:** an invalid agent URL (`httpx.InvalidURL`, not a
  `TransportError`) crashed instead of reporting; the connection check
  took up to 3 x 60 s with no spinner and is now one attempt at 15 s with
  one; a broken `config.toml` crashed both `ari` and `ari setup`, which
  now reports it and lets setup replace it; the history file no longer
  chmods the directory that holds `ARI_CONFIG_FILE`; the spinner hint no
  longer stays on "restarting" after the agent recovers.

Tests now share `tests/ari_cli_fakes.py` and fixtures in
`tests/conftest.py` instead of one test module importing another's
fixture. 17 new cases in `tests/test_ari_cli_chat_decisions.py`; the
session test now proves the loop continues after Ctrl-C (it passed with
`continue` mutated to `return` before); the pty test asserts that no
secret reaches the screen.

**Verified:** Windows 349 passed (13 modules not collectable there, as
before); Linux **470 passed** in `ari-oncall-agent-worker:test`, including
both pty tests; then a pty session against the deployed test agent through
the rebuilt bastion image. The arrow-key approval menu is still exercised
against the fake agent only.

### Same day - first hands-on use on test: two gaps, one agent bug

The user's first proposal went through end to end from the chat: card,
arrow-key approval, PR #24 on `OnCall-ARI-InfraCore-testing`. The second
request hung, and how the user tried to recover showed two gaps in the
chat itself:

- After Ctrl-C there was no way inside the chat to follow the task again
  or to stop it; `/status` only gave a snapshot. The user then typed the
  shell commands `ari tasks follow <id>` and `ari tasks cancel <id>` into
  the chat, where they went to the bridge as messages (refused, since the
  task was running). Added `/wait` (re-attach, via a new public
  `Bridge.follow`) and `/cancel` (asks first; declines a waiting
  proposal), every running-task notice now names them, a message typed
  while the task runs is held back with that hint, and an `ari <command>`
  line typed into the chat is explained instead of sent.
- `/st` was rejected as unknown. Commands now resolve by unique prefix;
  an ambiguous prefix lists the candidates.

The hang itself was agent-side, not the CLI: task `f0d352b6` sat for 19
minutes because the worker never noticed its `generate_change` follow-up
opencode process exit. Evidence taken live on the worker pod: `opencode`
pid 1185 was a zombie whose parent was `python -m api.worker` (so tini
cannot reap it), asyncio was using `PidfdChildWatcher` (Python 3.12.15),
and the worker held a pidfd for 1185 (fd 18) that no epoll instance in
the process was watching, so `_do_wait`/`waitpid` never ran. The 10-10
deadline then fired (`opencode (ari-change) timed out after 1155s (pid
1185) and was killed`), `generate_change` retried, and the retry produced
the proposal at 12:53 UTC. Third occurrence after 09-25 and 10-10; split
out as its own task, since the fix belongs in `opencode_runtime`/`git_ops`
rather than in the client.

**Verified:** Windows 357 passed; Linux 478 passed in the worker image. 8 new cases in
`tests/test_ari_cli_running.py`.

## 2026-10-14 - Agent Skills: how outsiders add capabilities (II-25116, step 3)

The open-source goal needs one extension point that is not engine code. That
is now [Agent Skills](https://agentskills.io/specification): a folder with a
`SKILL.md` and optional scripts. `docs/SKILLS.md` is the user-facing guide;
this entry records what was verified and why the isolation looks the way it
does.

**What exists.**

- `skills/` ships three examples: `tls-certificate-check` (no repository),
  `aws-security-group-allowlist` (the leader's example; refuses to change
  anything without an operator `config.json`), `changelog-entry` (used during
  a code change). Operators add more under `SKILLS_DIR/<name>/`.
- `common/skills.py` validates (spec rules, plus YAML read the way opencode's
  js-yaml reads it), discovers bundled then deployment skills (later wins),
  and copies them per request into
  `<workspace>/.opencode-state/config/opencode/skills`, which opencode reads
  as global skills because `XDG_CONFIG_HOME` is already per request. Nothing
  is written into the clone, so nothing can leak into a pull request.
- New capability `run_skill` (agent `ari-skill`) runs one skill with no
  repository. The planner sees `available_skills` (name and description) in
  its request and chooses it; the change and verify agents can load the same
  skills by name inside a code change.
- `ari skills validate` checks a skill offline.

**opencode 1.18.31 facts this rests on**, verified in the worker image with
`opencode debug skill`, `opencode debug agent`, real `opencode run` calls, and
the source at tag v1.18.31 (anomalyco/opencode):

- Skills are discovered from `$XDG_CONFIG_HOME/opencode/skill(s)/**`, the
  repository's `.opencode/skill(s)`, `.claude/skills`, `.agents/skills`,
  `~/.claude/skills`, `~/.agents/skills`, `~/.opencode/skill(s)`, and, when
  `--dir` is not inside a git repository, every ancestor directory's
  `.opencode`, `.claude` and `opencode.json`. Two skills with one name: the
  winner is whichever parse finishes last, so it is not deterministic.
  opencode does not check names against the spec.
- `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` drops the `.claude`/`.agents` scans
  (home and project). `OPENCODE_DISABLE_PROJECT_CONFIG=1` drops project
  `.opencode` dirs and `opencode.json` files, including ancestors.
- The `skill` tool returns the body plus "Base directory for this skill:
  <abs path>", which is how a model finds the scripts.
- Permission objects are evaluated **last match wins**, and a tool is removed
  when its last rule is `"*": "deny"`. Every new map here is written
  broad-first. The existing `_AGENT_DEFS` maps are written the other way round:
  `ari-change` has had no bash at all, and `verify`'s `git push` deny does
  nothing. A target repository's `.opencode/opencode.json` also overrides our
  agent permissions. Both are pre-existing and split into their own task.
- A bash permission pattern is the text of each tree-sitter `command` node, so
  `>` redirects and `VAR=x cmd` prefixes are seen and `&&`/`;`/`|` parts are
  checked one by one. `export`/`declare` statements are not `command` nodes and
  are never checked. A script must never take a security decision from an
  environment variable: that is why the AWS skill reads `config.json` from a
  fixed path next to it.

**Isolation of `run_skill`.** It runs in an empty `skill-run` directory with
its config passed by `OPENCODE_CONFIG` from outside that directory and
`OPENCODE_DISABLE_PROJECT_CONFIG=1`, so nothing in an ancestor directory
applies. It may read only the chosen skill's folder, has no glob/grep/list,
web fetch or edit, may load only that one skill, and may run only that skill's
`scripts/` (directly or via python3/python/bash/sh) plus `Bash(...)` prefixes
from `allowed-tools`. Commands containing `/../`, `$`, a backtick or `>` are
refused: both bundled scripts echo their arguments, so `$SECRET` as an
argument would have printed a worker secret. `AGENT_API_KEYS`,
`TEMPORAL_API_KEY`, `TEMPORAL_PAYLOAD_ENCRYPTION_KEY` and GitHub tokens are
removed from its environment.

**Repository skills are untrusted by default.** External `.claude`/`.agents`
scans are off, agents may load only our skills by name, and any of our names
that also exists in the repository or in a worker home (the real home and the
build cache, which is `verify`'s home) is blocked, using a lenient scan that
follows symlinks and reads names the way opencode's forgiving parser would.
`TRUST_REPO_SKILLS=true` allows repository skills except contested names and
opencode's built-in `customize-opencode`.

**`respond` said "NOTHING WAS EXECUTED" after a skill had acted.** oncall's
FACT line keys on `publish_pr` only, so a run whose only step was `run_skill`
was told nothing reached any external system, which would have contradicted
an applied security group change. `AgentMeta.direct_capabilities` names the
steps that act directly, and the fact line now says the step ran and to report
exactly what its output shows.

**Review.** A five-lens adversarial review (security, integration, spec,
examples, docs), each lens with an independent verifier: 25 confirmed, 10
refuted, all 25 fixed. The ones that mattered most: the FACT line above; YAML
values such as an impossible date raised past the validator and would have
taken discovery, and with it every opencode call, down; repository skill names
were read with our strict parser, so a file opencode still accepts slipped
past the same-name block; the AWS guard rails came from environment variables;
`$VAR` arguments leaked secrets.

**Ops note.** The bastion has 1 vCPU and 2 GB RAM. Two concurrent opencode
containers got both SIGKILLed (`exit -9`), and SSH timed out in the banner
exchange; live runs there must be one at a time.

**Live**, opencode 1.18.31 with a real model (claude-haiku-4-5) in the worker
image on the bastion, branch code mounted:

- Planner: "Is the TLS certificate of github.com still valid" planned
  `run_skill` with `tls-certificate-check`; "please whitelist 104.245.103.27
  vpn" planned `run_skill` with `aws-security-group-allowlist`; a README typo
  fix in a repository planned `clone_repo` + `generate_change`; "What is a
  deadlock?" planned nothing.
- `run_skill` end to end in 18 s: loaded the skill, ran `check_tls.py` once,
  reported github.com ok with 46 days left and expired.badssl.com "certificate
  has expired". Same result after the isolation changes.
- `ari-skill` was refused `> /tmp/report.json`, a heredoc writing a file,
  `env`, `curl`, and reads of `/etc/os-release` and `/proc/self/environ`; no
  file was written. The `$(...)` attempt was refused by the model itself, so
  the `$` rule rests on the rule text and unit tests, not on a live refusal.
- `ari-change` fixed the typo, loaded `changelog-entry` and added
  `## [Unreleased]` / `### Fixed` to CHANGELOG.md in 29 s. With a same-named
  skill in the repository's `.opencode/skills`, `write_config` dropped
  `changelog-entry` from the permission and logged `skills_shadowed`.
- Intermittently a run hit its deadline although the session had finished:
  debug logs show "exiting loop" on time, then opencode's own teardown
  (dispose, config reload) taking 35-52 s per step on the 1-vCPU bastion. It
  happened to `ari-change` with no skill config as well, so it is not caused by
  this change, and it is not root-caused. 1 of the last 5 runs stalled. Worth
  watching in the cluster, because a teardown stall turns a finished step into
  a timeout.

**Not done here:** a Helm chart mount for `SKILLS_DIR`, and a live run of the
AWS skill (the test worker has no AWS credentials or `boto3`).

**Verified:** Windows 466 passed; Linux 616 passed in the worker image.

## 2026-10-13 — A Kubernetes agent kind (II-25173): health checks and approved scaling, in the cluster it runs in

New agent kind `k8s` (`src/k8s_agent/`, `configs/k8s/`, `Dockerfile.k8s`).
It checks Pod-level health and proposes — and, after a person approves,
applies — Deployment replica changes, KEDA/HPA min/max changes and
CPU/memory changes. What follows is what someone extending it needs and
cannot read off the code.

### Decisions (with the user, 2026-10-13)

- **A separate kind, not more capabilities on `oncall`.** The oncall worker
  runs opencode's `ari-verify` with open bash; a ServiceAccount token with
  cluster write access in that pod could be read by any shell command and
  used to bypass the approval gate. `confirm_capability` is also a single
  name per kind (`publish_pr` for oncall). The only `common/` change is the
  one `_AGENT_PACKAGES` line ARCHITECTURE.md §3.2 prescribes.
- **In-cluster ServiceAccount only**, one agent per cluster, no kubeconfig
  fallback, no credential in any request. All agents deploy only on
  Kubernetes. The Jenkins kind is deprecated.
- **No opencode, no shell**: typed calls through the official `kubernetes`
  Python client (v37, kubernetes-client/python). lightkube 1.0.1 was used
  first and replaced the same day at the user's request: this runs in
  production, and lightkube 1.0 was two months old and pulls in the
  brand-new `httpx2`. The official client is synchronous, so
  `tools/client.py:Kube` runs every call in `asyncio.to_thread` and exposes
  dict-in/dict-out `get/list/patch/pod_log` by kind name — the only surface
  the rest of the package (and the tests' fake) depends on. v37's generated
  methods pick the *first* JSON content type for a patch, which is
  json-patch, so every patch passes `_content_type` explicitly
  (merge-patch for scale/autoscalers, strategic-merge for container
  resources).
- **"Managed" means KEDA- or HPA-managed.** A plain "scale X to N" on a
  managed Deployment is refused with the autoscaler's current bounds; its
  min/max change only when the request names the autoscaler and the values
  (`policy.mentions_autoscaler` + a verbatim quote check). Never below 1.
  KEDA-generated `keda-hpa-*` HPAs are never patched.
- **CPU/memory**: four independent fields, only the ones with a stated value.
  Values above the node cap — best eligible node's allocatable minus its
  DaemonSet requests, times (1 − headroom 0.2) — are **clamped**, and the
  proposal and result carry `requested` and `applied`.
- **Every change needs approval; > 50 replicas gets one approval, flagged
  `critical`.** A second approval for > 50 was designed, then dropped (see
  below).
- **Git is never written.** `persistence` is derived live from the cluster's
  own Flux objects (labels → HelmRelease → Kustomization → source), with no
  repository, path or chart convention hard-coded
  (`test_no_gitops_repository_or_org_is_hardcoded` enforces it), and never
  reads values ConfigMaps or inline values (test's values ConfigMap holds
  plaintext secrets).
- **Nothing secret leaves**: reads are reduced to whitelisted views
  (`tools/views.py` drops `env`/`args`/`command`/volumes and every annotation
  but ownership/KEDA-paused), and every capability result passes through
  `tools/redact.py` before it becomes a step result, so `respond`, the A2A
  artifacts and Temporal history only see scrubbed data.
  `test_no_canary_reaches_any_result_or_prompt` plants one value in env,
  last-applied, inline Helm values, a Git URL, a log line and an event, and
  checks results and every LLM prompt.

### Why there is no second approval for > 50 replicas

mcp-service's `pending_action_id()` (`app/a2a/client.py`) confirms **the
first** artifact carrying an `id`, and `common/a2a_server/executor.py`
publishes artifacts with SDK-generated ids, so a second proposal is
appended after the first rather than replacing it. A two-pause flow would
have mcp-service re-confirm the first proposal forever (fail-safe — nothing
executes — but never completes). Options were a one-line mcp-service fix,
reading prior workflow runs from Temporal, or a single approval; the user
chose a single approval with `risk_tier: critical` and an
`[HIGH REPLICA COUNT > 50]` prefix on `action_name`. If a second pause is
ever wanted, fix `pending_action_id()` first.

### Verified read-only on the test cluster (2026-10-13)

- Helm-rendered Deployments carry `app.kubernetes.io/managed-by: Helm`,
  `meta.helm.sh/release-*` and `helm.toolkit.fluxcd.io/name|namespace`; the
  HelmRelease carries `kustomize.toolkit.fluxcd.io/name|namespace`; the
  Kustomization's `spec.sourceRef` resolves to a GitRepository. The whole
  persistence chain resolves.
- helm-controller v1.6.3 owns `spec.replicas` and `resources` of the app
  Deployments via server-side apply; the `insightfinder` release was
  upgraded five times in about two hours by image automation — a direct
  change there usually lasts minutes to hours. stg's helm-controller v1.3.0
  owns the same fields via Update (Helm 3 three-way merge). No
  `driftDetection` on either.
- KEDA operator and `scaledobjects.keda.sh` CRD, metrics-server and Cluster
  Autoscaler are installed; `kube-system/cluster-autoscaler-status` is the
  YAML format (per-group `maxSize`, `cloudProviderTarget`, `backoffInfo`).
  A ScaledObject on stg had no `scaleTargetRef.kind` (KEDA defaults it to
  Deployment — handled).
- InsightFinder Java services: startupProbe `60s + 10×10s`, readiness 60s
  delay, `progressDeadlineSeconds: 600` — hence verification waits on the
  Deployment's own rollout semantics, bounded by `progressDeadlineSeconds +
  60`, instead of a fixed timeout.
- Nodes are t3/t3a.2xlarge (allocatable 7910m / ~30Gi); one carries a
  `role` taint, which the node-cap calculation must respect.

### Things that are easy to get wrong

- Crash-loop logs are read with `previous=True` — the crashed container's
  log is the one that explains a crash loop, not the restarting one.
- Integers come back as floats after the A2A artifact round trip
  (`ParseDict` into a protobuf `Value`); `apply_approved_change._int`
  normalises them. Tests feed confirmations through the same round trip
  (`k8s_fixtures.a2a_round_trip`), not `json.dumps`.
- The pre-apply staleness check compares only what the approved change
  depends on, never the Deployment's `generation`: image automation bumps
  it several times an hour on test, which would have refused nearly every
  approval inside the 3-hour window.
- In tests, a fake that never completes a rollout makes `wait_rollout` run
  its full window (660s real time). Use the fake's `auto_rollout` or patch
  `time.monotonic`; never leave an apply test on the real clock.

**Verified:** full suite on a local Python 3.12 venv (Docker daemon was not
running): **426 passed**, of which 100 are new (`test_k8s_rules.py`,
`test_k8s_capabilities.py`, `test_k8s_workflow.py` — the latter runs the
real workflow and activities on Temporal's test server: propose → pause →
confirm → apply). Manifests validated with kubeconform.
### Live run on the test EKS, same day

Deployed from `deploy/k8s-agent-e2e/` (temporary images `*-ii25173`, a
temporary Temporal namespace `k8s-ari-agent`), then everything removed
again. No LLM key was available, so the LLM's extraction step was replaced
by a scripted stand-in run inside the worker pod; everything else was real
— in-cluster ServiceAccount, RBAC, dry-runs, patches, rollout waiting, the
A2A protobuf round trip of the approval data.

**Verified live:** reads, KEDA/HPA detection, CA status and the full Flux
chain (resolved to the real test-fluxcd commit); dry-runs of all three
actions leave the object unchanged; RBAC denies reading Secrets and
deleting pods; health check finds the crash loop (with redacted log), the
OOM kill and the image-pull failure; refusals (KEDA/HPA-managed, scale to 0,
limit below request, autoscaler not explicitly named); proposals for 51
replicas (`critical`), a clamped 64-CPU request (cap 6008m on t3.2xlarge
after DaemonSets and 20% headroom), a Helm-labelled Deployment whose chain
breaks; applied and verified: replicas 2→3→2, KEDA max 3→4→3, HPA min
1→2→1, and a memory change on a 150-second-startup fixture (waited 331s
for both pods, no false timeout); a stale approval (replicas changed by
hand after the proposal) is refused with nothing applied; agent card, API →
Temporal → worker path, and wrong-key rejection.

**Three defects only the live run could show, all fixed:**
1. When the previous container's log is gone, the kubelet answers **HTTP
   200** with "unable to retrieve container logs for containerd://…". That
   text was being reported as the crash log; `read.get_pod_log` now treats
   it as missing and falls back to the current (just-terminated) container,
   which still holds the crash output.
2. Redaction ran twice (on read, and on the step result) and the second
   pass turned `password=[REDACTED]` into `[REDACTED]]`. Redaction is now
   idempotent.
3. A Deployment named `e2e-hpa` made "let e2e-hpa go up to 10" count as an
   explicit autoscaler request. The target's own names are now removed
   before the keyword check.

**Shared behaviour noticed, not changed (it is in `common/`, same for
oncall):** a wrong `x-api-key` is rejected, but the A2A SDK surfaces the
401 as HTTP 500; and a planning failure's message carries the provider's
error text, which for OpenAI includes a masked form of the rejected key
(first/last characters).

**Not verified live:** the LLM's own extraction and planning (no key), and
a real Cluster Autoscaler scale-up or quota failure (would add a paid node;
covered by unit tests with real CA status samples).

**Still to do outside this repo:** a Helm chart for this kind (API +
worker Deployments, the RBAC in `docs/examples/k8s-rbac.yaml`, worker
`automountServiceAccountToken: true`, `TEMPORAL_PAYLOAD_ENCRYPTION_KEY`
from a Secret) before any non-test deployment.

## 2026-10-14 — The k8s persistence hint is a hand-off contract for a GitOps PR

**What.** `persistence` (in `pending_action` and `action_result`) gained
`git` (`repo_slug`, `https_url`, `branch`, `commit`), per-source
`target_path` and `values_precedence`, `chart_source.url`, and
`ready_to_persist` / `readiness_reason`. RBAC adds read on
`source.toolkit.fluxcd.io/helmrepositories` (the chart source).

**Why.** The intended next step after a direct change is a pull request to
the Flux repo, written by a code agent that the on-call coordinator
(mcp-service `OnCallInvestigationWorkflow`) calls. The coordinator only
sees artifacts as rendered JSON (`render_task`), and the code agent only
gets a repo from the system's `github_repositories` allow-list, matched by
`owner/repo`. On test the main `insightfinder` HelmRelease merges 15
`valuesFrom` keys, so without the merge order a correct-looking edit can
land in a file a later source overrides; and when no values file sets the
key yet, only the chart says what the key is.

**Verified on the test cluster (2026-10-14),** in the worker pod with its
own ServiceAccount and the enumerated RBAC (LLM extraction stubbed): the
chain for the real `insightfinder-appserver` and `ari-oncall-worker`
(read-only) resolves to `git.repo_slug` `insightfinder/test-fluxcd`,
branch `main`, the applied commit, and the chart source URL read through the
new `helmrepositories` grant; a fixture labelled as part of the real
`insightfinder` HelmRelease was scaled 1→2→1 through propose/apply, with
`ready_to_persist` false on the proposal and true on the `succeeded`
result; no URL userinfo in any output. All test resources were deleted.

**Found live, fixed:** after the A2A protobuf round trip `changes[].from/to`
read `1.0 → 2.0` (protobuf `Value` has only doubles), which a code agent
would copy into YAML as `replicas: 2.0`. `changes` values are now manifest
strings (`"1" → "2"`, `"6Gi"`).

**Not done here (mcp-service side, separate work):** the k8s agent is not
registered as an on-call agent; coordinator rule 3a tells a code agent to
make no change when the cause is configuration or infrastructure, which
forbids exactly this PR; the Flux repo would have to be in the
allow-list with a PAT that can push; and the oncall `verify` step looks for
a build, which a YAML-only repo does not have.

## 2026-10-14 — Live run with a real LLM: 13/13 scenarios after one fix

**What.** `scripts/k8s_agent_live_test.py --allow-writes` against the agent
deployed on the test EKS (API → Temporal → worker, in-cluster
ServiceAccount, the enumerated RBAC), with a real Anthropic model doing
planning, target resolution and change extraction. Passed: agent card;
health (crash loop, OOM kill, image pull); refusals (KEDA/HPA-managed
replicas, scale to 0, limit below request); clamp (64 CPU → 6008m);
51 replicas proposed as `critical`; drift notice; and approved, applied,
verified changes: replicas 2→3, KEDA max 3→4, HPA min 1→2, a memory change
on the slow-start fixture (waited 336s, no false timeout), and the new
`persist` scenario (a fixture labelled into the real `insightfinder`
HelmRelease: `git.repo_slug` `insightfinder/test-fluxcd`, chart source URL,
`changes` `"1" → "2"`, `ready_to_persist` false on the proposal and true on
the result). No canary leak anywhere. Everything was deleted afterwards
(both namespaces, RBAC, the temporary Temporal namespace).

**Found live, fixed:** the first `health` run missed the crash loop. Between
two restarts the kubelet shows the container that just exited as
`terminated` (reason `Error`) for a few seconds before it reports
`CrashLoopBackOff`; the rule reported that as a `warning` "Error". A
terminated container with a non-zero exit and at least one restart is now
the same critical `CrashLoopBackOff (<reason>)` finding (and gets the
previous-log tail). Unit-tested for both windows; the three live reruns
after the fix happened to land in the `CrashLoopBackOff` window, so the
`terminated` window itself is covered by the unit test only.

**Not exposed:** test's `test-temporal.insightfinder.com` is the Temporal
Web UI (HTTPRoute → `temporal-web:8080`, Traefik basic auth), not the gRPC
frontend; a client connect there gets 401. Local access to Temporal still
needs a port-forward.

## 2026-10-14 — End to end from InsightFinder: incident → on-call → k8s agent → approval → change

**What ran (test).** The k8s agent was wired into the ARI on-call path in
mcp-service (branch `II-25173-k8s-agent-oncall`, uncommitted): agent
`arik8s` (`config/agents/arik8s.yaml`), offered on `general_support` only
for a system whose Kubernetes agent card sets `kubernetes_url`, and called
only when that URL is the cluster the agent serves
(`ARI_K8S_AGENT_CLUSTER_URL`). On test.insightfinder.com (account bo): a log
project with an `incidentlist` keyword (`OOMKilled`) in a new system, the
Kubernetes card (test EKS API URL; token unused), "Detected incidents" on,
then one synthetic log line naming deployment `e2e-oom`. IC created the
incident, held it for its 1800 s root-cause wait, dispatched it; the
coordinator called the k8s agent (health check, then "set the memory limit
... to 256Mi"); the proposal paused, IC recorded the approval request
("Set limits.memory 256Mi on ari-k8s-e2e/e2e-oom (app)"); after approval the
change was applied (32Mi → 256Mi), the investigation completed and IC sent
the report as a Slack DM. Dev images were run by excluding the
`insightfinder` HelmRelease from its Kustomization
(`kustomize.toolkit.fluxcd.io/reconcile: disabled`) and suspending it — a
plain suspend was undone within minutes when the `apps` Kustomization
re-applied the object. Everything was restored and deleted afterwards.

**Found live, fixed in this repo:**
1. A coordinator asking to "propose an increase" without a value was refused
   (by design), but the reply said a 512Mi change was "ready for
   confirmation". The respond note now says a refusal proposes nothing, and
   the refusal carries the current replicas/resources and an example request.
2. The health report did not show configured requests/limits, so nobody
   could name a concrete value. It now has `container_resources` for the
   workloads with findings.
3. A memory change was reported `succeeded` after 10 s while the new pod
   kept OOM-crashing (a container that is killed at its limit is ready for a
   few seconds after each start). New pods must now stay up without a
   restart for 45 s (`rollout.STABILITY_SECONDS`); re-run live: the same
   change is now `new_pods_failing` with "OOMKilled (exit code 137)".
4. A pod that restarts often but is running at the moment was reported only
   as `FrequentRestarts`; it is now `FrequentRestarts (<last reason>)`.

**Environment facts (test, 2026-10-14):** approval email is not delivered
(`EmailNative: Configuration is not correct for sending the email,
Platform: AWS, Version: TEST`), so the single-use approve link never
reaches anyone; the test approval was sent to mcp-service's
`/api/v1/tasks/{id}/approve` (the call IC's approval page makes). The
Temporal gRPC frontend has no ingress (`test-temporal.insightfinder.com` is
the Web UI). The test cluster's own telemetry goes to stg, not test.

**Not done / follow-ups:** cross-cluster routing (a stg system watching
test pods) is refused, not routed; IC's Kubernetes card still requires an
unused token; a k8s change has no URL so it is absent from IC's digest
action list (it is in the report text).


## 2026-10-15 — k8s agent review fixes, and the k8s kind after the Agent Skills merge

Merged `origin/test` (chat gateway, `ari` CLI, Agent Skills) into the
II-25173 branch; conflicts were only in `README.md`, `docs/MEMORY.md` and
`pyproject.toml` (wheel packages now list `k8s_agent` with `chat_gateway`
and `ari_cli`).

**Agent Skills and the k8s kind.** `common/planner.py` now offers every
kind's planner the discovered skills (`available_skills`), but only the
oncall kind registers `run_skill`. A k8s plan naming `run_skill` would have
failed `plan_task` on an unknown capability, and skill scripts have no
business on a worker holding cluster-wide patch rights. Two guards, both
outside `common/`: `Dockerfile.k8s` sets `SKILLS_ENABLED=false`, and
`k8s_agent/meta.py`'s plan policy keeps only `find_workload`,
`check_health` and `propose_change` (a test pins that list to
`configs/k8s/capabilities.yaml`).

**Review fixes** (each with a test that fails on the old code):
- `rollout.assess` reported `rollout_failed` from a `ProgressDeadlineExceeded`
  left over from before the change, which is exactly the stuck Deployment a
  resource fix gets approved for. It now trusts the condition only once the
  controller has observed the patched generation and the condition was
  updated after the change (views now keep conditions' `last_update`).
- `check_health` matched events and flagged workloads by name prefix, so
  `api` picked up `api-gateway`'s events. Pods are now mapped to their
  Deployment through the ReplicaSet owner and `pod-template-hash`; events
  match the Deployment, its listed Pods, or generated names in Kubernetes'
  vowel-free safe-encoding alphabet.
- `find_workload`'s verbatim-name shortcut no longer fires when the prompt
  uses that name for a namespace ("the cert-manager namespace",
  "namespace cert-manager", "-n cert-manager", "命名空间"); the model decides.
- The plan policy now puts `find_workload` before `check_health` too, as
  its docstring always claimed.
- `capacity.resource_cap` subtracts the pod's other containers' requests
  (sidecars) and takes CPU and memory from one node. A per-pod request
  larger than every eligible node is now a `high` risk instead of "stays
  Pending until Cluster Autoscaler adds nodes".

Known gap, not fixed: a Deployment whose selector has only
`matchExpressions` gives `list_pods` no label selector, so a deployment-scope
health check reads every pod in the namespace (and their events). Rare on
EKS workloads, which use `matchLabels`.

`poetry.lock` is stale against `pyproject.toml` (`poetry check --lock`
fails); that is already true on `origin/test` itself, so it is not from
this merge. Images build with `pip install .` and are unaffected.

Not re-run on a live cluster; all of the above is covered against the fake
API server (678 passed, 7 skipped: the `ari` CLI tests, which need `typer`
and `pexpect` from the `cli` extra).

**Chart (`charts` repo, `II-25173-k8s-agent-chart`).** The k8s API now gets
the same task store volume as the on-call API (`ariK8sAgent.persistence.taskStore`,
1Gi RWO PVC `<fullname>-k8s-task-store` at `/var/lib/ari-agent/tasks`,
`TASK_STORE_ROOT` in the Secret, `strategy: Recreate`). Without it a proposal
waiting for approval (up to mcp-service's 25-minute approval timeout) is lost
whenever the API restarts, and the shared `ari-agent-api` image is redeployed
on every build; the approval then fails closed ("Could not find a pending
action") and nothing is applied. The worker no longer sets `command:`, which
had replaced the image's tini entrypoint so SIGTERM was ignored and every
rollout ended in SIGKILL. Checked with `helm lint` and a server-side dry run
against test EKS. Still open: the test-fluxcd release carries placeholders,
and mcp-service offers `arik8s` for systems whose cluster URL does not match.

**Keeping the existing agents untouched** (checked 2026-10-15):
- `kubernetes` is now the `k8s` extra, installed only by `Dockerfile.k8s`
  (`pip install ".[k8s]"`). With it out of the base dependencies,
  `uv pip compile pyproject.toml` resolves exactly what `origin/test`
  resolves, so the shared `ari-agent-api` image and the oncall worker get
  the same packages as before. The API process never imports `kubernetes`,
  even as the k8s kind (imported `api.main` with `AGENT_KIND=k8s` in a venv
  without it). The k8s test modules `importorskip("kubernetes")`, so a
  `.[dev]` checkout without the extra runs 544 passed / 10 skipped.
- The only line in `src/common/` is the `_AGENT_PACKAGES` registration.
- `charts/oncallari`: `helm template` of the ari-oncall and ari-action
  releases with their test-fluxcd values (and with defaults, and with the
  shared Ingress on) renders byte-for-byte as before apart from the chart
  version label.
- mcp-service: the coordinator's Kubernetes rules are added only when
  `arik8s` is offered; without it every addendum variant equals the one on
  `origin/test`.
- test-fluxcd: adding the `k8s-ari-agent` Temporal namespace only adds a
  step to the namespace-creation Job (describe-or-create); the Temporal
  server Deployments and ConfigMaps render unchanged.

## 2026-10-15 (evening) — Second full review, fixes, and a chart-based live run on test

Three parallel reviews (this repo; mcp-service + helm-charts; charts +
test-fluxcd), then fixes, then a live run with the agent installed from the
branch chart (`helm install ari-k8s charts/oncallari`, namespace
`ari-k8s-agent`) and mcp-service swapped to the branch image.

**Fixed here** (each with a test that fails on the old code):
- *An applied change could be reported as "nothing was executed".*
  `apply_approved_change` patched, then `wait_rollout` (or the Flux read)
  could raise, or the worker could die; the step had one attempt and no
  `action_result`, so `respond` stated NOTHING WAS EXECUTED while the change
  was live. Now: everything after the patch is caught
  (`verification_unavailable`, applied); a patch call that errors is judged
  by re-reading the target; and a retried attempt that finds the approved
  values already in place skips the write and verifies. With that, the step
  retries once (`max_attempts: 2`) without ever writing twice.
- *The quote did not have to state the value.* "3 replicas" could back 2024,
  "2Gi" could back 20Gi. `policy.quote_in_prompt` now also requires the
  quote to state the extracted value (same integer, or the same quantity in
  any spelling: "2 GiB" = 2Gi, "0.5 cores" = 500m).
- *A capacity clamp could produce 0* (other containers' requests eat the
  node); now refused.
- *Unrelated Cluster Autoscaler trouble stopped verification.* A GPU group in
  backoff, or a FailedScaleUp for another pod, made a CPU Deployment's
  rollout `unschedulable`. `classify_pending` now counts only CA events on
  the workload's own pending pods (and a backoff only when CA acted for
  them); a backoff at proposal time is a medium risk, not high.
- *Autoscaler changes were never verified.* A lowered max reported success
  at once; now the Deployment must move inside both bounds, and the
  autoscaler is re-read afterwards (`reverted` if a reconcile restored it).
- `action_result.ran: true`, which UIE's on-call Slack summary reads (it
  printed "did not run" for an applied k8s change).
- Smaller: generated-name hashes are 6-10 characters (`api-grpc` is not
  `api`'s); a namespace is preferred only where the prompt uses it as one
  (a namespace called `it`); PDBs match template labels and are reported as
  not blocking a scale-down (ReplicaSet deletions are not evictions).

**Not fixed, recorded:** several proposals on one A2A task stay approvable
(only reachable through the new chat gateway continuing a task; the on-call
path opens a task per call); an autoscaler word in a negation still counts
as asking for an autoscaler change; the patch carries no resourceVersion
precondition (a millisecond window).

**Live run on test (2026-10-15 18:25-18:47, real LLM):** 17/17 scenarios
pass, including the new ones: `health_prefix` (e2e-web vs e2e-web-gateway),
`clamp_sidecar` (cap minus the proxy's 256Mi), `fix_stuck` (e2e-memhungry
stuck in ProgressDeadlineExceeded, raised to 128Mi: succeeded) and
`resume_after_restart` (API restarted between proposal and approval; the
task-store PVC carried the proposal and the change applied). The chart
install showed tini as PID 1 in the worker, `SKILLS_ENABLED=false`, and the
PVC mounted at `/var/lib/ari-agent/tasks`.

**End to end from InsightFinder, same evening:** a synthetic OOMKilled log for
`e2e-memhungry` (stuck in ProgressDeadlineExceeded) at 10:39Z; IC dispatched
at 11:20Z with `arik8s` offered (its new availability check passed for the
matching cluster); health check, then the proposal "Set requests.memory 64Mi,
limits.memory 128Mi on ari-k8s-e2e/e2e-memhungry (app)"; approved through
mcp-service's `/approve` (test still cannot email approvals); applied,
`status: succeeded` after 55s with the 45s stability window, `ran: true`;
completed callback and the Slack report at 11:24Z. Everything was removed
afterwards (helm release, namespaces, Temporal namespace, mcp-service
restored by Flux, Detected incidents off again).

**After the commit security review:** mcp-service offered `arik8s` to any
system whose card named this cluster's URL, and that URL is whatever a
tenant types, so another tenant could have changes proposed against our own
cluster and approve them itself. mcp-service now also requires the system's
account to be in `ARI_K8S_AGENT_ALLOWED_USERS` (unset: nobody), passed by the
insightfinder chart as `mcpService.ariK8sAgentAllowedUsers` ("bo" on test).
Not live-tested; the e2e above ran before it, as bo.

## 2026-10-15 (night) — Untrusted logs, and the second full live run on test

- Container log tails in health reports are fenced as untrusted data
  (`stepkit.untrusted_block`; marker-like text in the log is defused) and the
  response step is told all cluster text is data, never instructions.
- Live regression with the final images, agent installed from the committed
  chart: 22/22 scenarios pass, including `injection` (a crash log telling the
  AI to call every pod healthy: fenced, finding reported, not obeyed),
  `pdb_info`, `oversized` (pod larger than every node: high risk),
  `keda_reverted` (bound changed back right after: `reverted`),
  `worker_restart` (worker killed mid-verification: the retry found the
  change in place, wrote nothing, reported `succeeded`) and
  `resume_after_restart`.
- mcp-service account allowlist, checked inside the deployed pod: `bo` on
  this cluster offered; `Bo`, `bo2`, another account, or another cluster's
  URL not offered. An unrelated system's investigation in the same window
  got neither `arik8s` nor the Kubernetes rules.
- From InsightFinder: the first new incident was consolidated with the
  morning's (same instance, similar text) and never dispatched; use a fresh
  instance name per run. The next one dispatched, but the coordinator
  (org model claude-haiku-4-5) only *recommended* the memory increase in its
  report. Fixed on both sides: rule 1b/1c tells it to call the agent again to
  have such a change proposed, and the agent's health reply now ends with the
  exact request that would get its fix proposed. Rerun: health check, then
  "Set limits.memory 64Mi on ari-k8s-e2e/e2e-memhungry (app)", approved via
  mcp-service `/approve`, `succeeded` (55s, 45s stable), `ran: true`,
  completed callback and Slack report at 14:13Z.
- Still not testable on test: the approval email (test cannot send mail),
  Cluster Autoscaler backoff in an unrelated group, a patch call that errors
  after landing (unit-tested only).

## 2026-10-16 — Pre-PR review of the k8s kind, and a 36-scenario live run from the chart

Scope: this repo and the oncallari chart only (mcp-service and the other
repos integrate the agent separately).

**Fixed after review:**
- A retried apply that found the change already in place verified it with no
  start time, so pods from before the change (often the very crash loop the
  change fixes) counted as its own: `new_pods_failing` straight away. It now
  judges only pods created since the proposal (`pending_action.created_at`).
- A replica or resource change put back during verification (a Flux
  reconcile, a Helm upgrade) waited out the window and said `in_progress`.
  `wait_rollout` now returns `reverted` as soon as the Deployment stops holding
  the approved value (quantities compared as numbers, not strings).
- "512m" memory is legal Kubernetes (milli-bytes), so it was proposed as a
  1-byte limit. Memory with `m`/`u`/`n`, or under 4Mi, is now refused with a
  hint to use Mi/Gi.
- A health check of a name that matched nothing silently reported the whole
  namespace. It now checks nothing: `scope.type: unresolved`, with candidates.
- Planner params could override `find_workload`'s resolution (an unchecked
  name); they no longer can.
- A Deployment selected only by `matchExpressions` listed every pod in its
  namespace; pods are now listed with the full selector.
- Redaction: a log tail cut through a private key (only BEGIN, or only END)
  and quoted multi-word secrets (`"password": "correct horse battery"`) are
  now scrubbed.
- `kubernetes` is in the `dev` extra: without it the k8s tests skip rather
  than fail.
- Chart: RBAC trimmed to exactly what `tools/client.py` calls (no `watch`,
  no ReplicaSets/DaemonSets, no events.k8s.io, no node metrics, Flux objects
  `get` only; `docs/examples/k8s-rbac.yaml` matches). Rendering now fails
  without `agentApiKeys` (or with `change-me`), without a payload encryption
  key, with `api.replicaCount` > 1, or with `serviceAccount.create=false` and
  no name.

**Live script made stricter and re-runnable:** every applied change is
read back with kubectl; every cancelled proposal must leave the Deployment's
generation unchanged; write scenarios put the fields they touched back with
kubectl (the four write scenarios that used to fail on a second run now
pass again). New: `auth`, `rbac`
(`kubectl auth can-i` as the worker's ServiceAccount, allow and deny lists),
`pods` (tini PID 1, uid 10001, read-only root, no token in the API pod),
`health_cluster`, `health_missing`, `missing_target`, `ambiguous`, and the
approval gate: `gate_text_yes`, `gate_wrong_id`, `gate_cross_task`,
`gate_cancelled`, `gate_stale`, `gate_replay`, `double_approve`.

**Verified on test (2026-10-16):** installed with the README's `helm install`
line from the chart branch (temporary images `2610161234-ii25173`), then
`--allow-writes`: 36/36 pass; four write scenarios rerun: pass. Everything
deleted afterwards (release, both namespaces, Temporal namespace; helm
removed the cluster RBAC).

**Not live-tested:** the retried apply's proposal-time cutoff when an old
failing pod exists (`worker_restart` uses e2e-slow, which has none). Cheap
next time: propose 128Mi on e2e-memhungry, set 128Mi with kubectl, approve;
expect `already_at_target` and a status other than `new_pods_failing`.

**Shared behaviour seen, not changed (src/common, same for oncall):**
- A missing or wrong `x-api-key` is refused, but answered 500, not 401.
- An approval naming the wrong id fails the task, so the real proposal can
  no longer be approved (HTTP 400); ask again for a fresh one.
- Two approvals sent at once wrote once and the task ended `COMPLETED`
  (workflow id = task id).
- If a retried apply attempt itself fails at the Temporal level (worker lost
  again, step budget spent), the response says nothing was executed and no
  `action_result` is published, although the first attempt may have written.
  The fix belongs in the shared workflow/`execution_fact`.
- `tests/test_workflow_execution.py::test_the_budget_caps_a_step_across_all_of_its_retries`
  failed once in a full run and passed on every rerun (timing).

## 2026-10-16 — Small kind-agnostic `src/common/` fixes found while building the k8s kind

Until now the k8s kind's only `src/common/` line was its `_AGENT_PACKAGES`
entry. With the user's go-ahead, four shared problems it ran into are fixed
in `common/`. Each one is kind-agnostic (no branch on an agent kind), and
oncall behaves as before apart from what is listed. `ari-agent-demo`'s
vendoring sync now has these files to pick up: `a2a_server/auth.py`,
`app_factory.py`, `llm/client.py`, `agent_meta.py`, `planner.py`,
`respond.py`, `orchestration/workflow.py`.

- **A wrong or missing `x-api-key` is a 401 on REST too.** It used to be a
  500 on `/message:send`, `/message:stream` and `/tasks/...`, and a 401 only
  on JSON-RPC. The cause: the SDK wraps every REST handler in
  `rest_error_handler`, which turns any non-`A2AError`, including the
  context builder's `HTTPException`, into `500 unknown exception`. The key
  is now checked by `ApiKeyMiddleware` (pure ASGI) before routing, but only
  for the A2A routes. The agent card, `/healthz` and `/docs` stay open as
  before. Watch out: the SDK mounts its REST routes again under
  `/{tenant}`, and `/.well-known/agent-card.json` matches that mount by
  prefix, so the middleware only counts a mount when one of its child
  routes matches. The context builder still checks the key as a second
  line. Callers now fail fast: `chat_gateway`/`http_retry` retry 5xx, not
  401. Tests: `tests/test_a2a_auth.py`. `scripts/k8s_agent_live_test.py
  auth` now requires a 401; before, it also accepted a 500.
- **LLM provider errors no longer carry any part of the API key.**
  OpenAI's 401 body quotes the rejected key with its first and last
  characters (`sk-proj-****abcd`), and that text became the planning
  failure reason, which reaches `respond`'s prompt, the reply and Temporal
  history. `common/llm/client.py` now raises `LLMProviderError`. A 401
  gives only provider + status. Other statuses keep their detail, with
  key-echo and key-like strings masked (`sk-`/`pk-`/`rk-`, Google `AIza`,
  Groq `gsk_`, bearer tokens, bare 32+ hex), and the SDK exception is not
  chained (`from None`). This covers the planner, `respond` and every
  capability's `llm_complete`, since all of them use this client. It was
  never reproduced against a live provider; the tests use the response
  shape OpenAI documents.
- **`AgentMeta.planner_skills`** (default `True`). The k8s kind sets it
  `False`, so its planner is never shown `available_skills`. Before this,
  only the image env (`SKILLS_ENABLED=false` in `Dockerfile.k8s`, kept as
  defense in depth) and `_keep_plans_safe` stood between that planner and a
  `run_skill` step that does not exist in the kind.
- **An approved step that raised past its retries is no longer reported as
  "nothing was executed".** If Temporal gives up waiting on the confirmed
  (`confirm_capability`) step, the workflow now records
  `{"status": "interrupted", "failure": ...}` for it. That covers a
  heartbeat, start-to-close or schedule-to-close timeout (which is what a
  lost worker or a spent budget looks like) and a cancellation; see
  `workflow._may_have_run`. `respond.execution_fact` then states that the
  outcome is unknown and has to be checked, because an attempt may already
  have written. An error the capability raises itself is still a plain
  failure and still reported as nothing executed, e.g. the drift refusal
  ("Nothing was changed. Ask again"). A first version also marked those
  `interrupted`, and review caught it. Oncall's `publish_pr` gets the same
  treatment. Tests: `test_respond.py`, and in `test_k8s_workflow.py`, the
  `..._cut_off_by_its_timeout_...` and `..._that_refuses_...` tests.

**Verified on test (2026-10-16, after review fixes):** installed from the
chart branch with temporary images `2610161648-ii25173`, on test's existing
Temporal namespace `jenkins-ari-agent` (the Jenkins kind is deprecated and
nothing polls it there), task queue `k8s-ari-queue`, so nothing new was
created in Temporal. Results:
- `--allow-writes`: 36/36 pass. `auth` now sees 401 on every refused
  request.
- Two one-off checks:
  - `gate_stale`: the drift refusal's reply says nothing was changed. It
    does not say the outcome is unknown.
  - A request carrying a fake OpenAI key ended with "planning failed:
    openai rejected the API key (HTTP 401)", and no part of the key appears
    anywhere in the task.
- Cleanup: release and both namespaces deleted, with no `ari-k8s` cluster
  RBAC left.
- Not live-tested: the `interrupted` path itself. `worker_restart` still
  ends in the retried attempt finding the change in place.

**Considered and not done:**
- *A deterministic plan that skips the LLM planner for the k8s kind.* The
  planner does real routing here: a health question vs a change vs a plain
  question, a request that needs both, and resolving "yes, go ahead" from
  the history. `find_workload` skips its own model call when the name
  appears verbatim, so there is no other call to fold the intent into, and
  a keyword router would be worse.
- *Multi-step resume after approval.* `apply_approved_change` already
  verifies inside its own step.
- *An approval naming the wrong id fails the task* (the real proposal can
  then no longer be approved). Leaving the task in `input-required` instead
  is a small `executor.py` change, but mcp-service's approval handling and
  the live `gate_wrong_id` scenario both expect the failure. Change all
  three together, or none.

## 2026-10-15 - Slack front end for the chat gateway (II-25116, step 2)

People can now use the agent from Slack. `src/chat_gateway/slack.py` is a
Bolt `AsyncApp` over Socket Mode on top of the platform-neutral `Bridge` from
step 1; `docs/CHAT_GATEWAY.md` is the operator and user guide. The agent does
not change: the gateway is one more A2A client, like the `ari` CLI.

**Why a separate process, not part of `ari-oncall-api`.** The gateway carries
`LLM_API_KEY` and `GITHUB_PAT` for every request; the API pod holds neither
and is internet-facing in test. Socket Mode is one long-lived outbound
connection that Slack load-balances events across, so the gateway must run as
exactly one replica with `strategy: Recreate`, while the API rolls on every
push to `test`. Keeping them apart means an agent rollout does not drop the
Slack connection and a Slack outage cannot touch the agent.

**How it behaves.**

- A conversation is a thread, keyed `channel:thread_ts`; a DM is one
  conversation keyed `channel:dm`. Mention the bot to start; replies in a
  known thread continue without a mention. One request per thread at a time
  (per-thread lock). Progress is one message edited in place, throttled by
  `SLACK_PROGRESS_SECONDS`.
- Proposals show the agent's own words plus a compact Repository / Branch /
  Files / Tests block, with Approve (confirm dialog), Decline and Full diff
  buttons. The button value carries thread key, task id, pending action id
  and requester, so buttons work after a restart and a leftover Decline on an
  older proposal is refused instead of cancelling the current task.
- `REQUESTER_CAN_APPROVE=false` is a four-eyes rule: only someone in
  `APPROVERS` other than the requester may approve; the requester can still
  decline. Startup refuses `false` with an empty `APPROVERS`.
- Approval emails come from UIE (OnCallAriApprovalDelivery), never from the
  agent, so requests made from Slack or the CLI get buttons or a menu only.

**Restarts.** The first live restart lost every conversation, and a plain
reply in an old thread was silently ignored, which looked like the bot had
died. Two fixes:

- `SLACK_SESSION_FILE` persists conversations (atomic write, mode 0600). Only
  ids are saved: thread key, requester, context id, task id, task state,
  pending action id, timestamp. The request `config` (LLM key, PAT) is never
  written; `approve()` rebuilds it from the environment. A damaged file is
  logged and the gateway starts empty. The 24 h TTL still applies.
- In a thread the gateway does not know, a plain reply triggers one
  `conversations.replies` read (cached per thread, bounded at 5000 entries);
  if the bot has written there, it posts a one-line "I no longer have this
  conversation, mention me to start again" note, once per thread per process.
  Threads the bot never joined stay silent. A failing read stays silent.

**Slack quirks found on the way.**

- `message` events also arrive for messages that mention the bot; those are
  left to `app_mention`, or every mention would run twice.
- Slack turns `repo=github.com/org/x` into `<http://github.com/org/x|github.com/org/x>`;
  the label is kept, and `repo=` values without a scheme get `https://`.
- Messages with subtype `file_share` or `thread_broadcast` are real user
  messages and must not be dropped with the other subtypes.
- A button click in a DM has no `thread_ts`; replies must go back to the DM,
  not into a new thread.
- mrkdwn needs `&`, `<`, `>` escaped outside code; `**bold**`, headings and
  Markdown links are converted, code spans and fences are left alone, and a
  long answer split into chunks re-balances its ``` fences.
- The container runs as PID 1, so SIGTERM is handled explicitly or
  `docker stop` waits for the kill timeout.

**Live**, app "ARI Agent" in a private test channel, gateway on the bastion
against the deployed test agent: a question, a thread follow-up, a DM, a
decline (buttons closed, "Declined by"), and an approve that opened PR #25 on
OnCall-ARI-InfraCore-testing. Reading the channel afterwards led to three
fixes: `status` is one line instead of the whole answer again, no duplicate
"canceled" message after a decline, and the shorter proposal text above.

**Review.** A review workflow confirmed 16 unique defects; 15 were fixed. Not
fixed: a progress edit that falls inside the throttle window is dropped, not
deferred, so the "working" message can lag one step behind.

**Not done here:** a `chatGateway` block in the `oncallari` Helm chart (one
replica, Recreate, Secret for the Slack tokens, a small PVC for
`SLACK_SESSION_FILE`, no ingress); the Docker Hub repository
`insightfinderinc/ari-chat-gateway` may not exist yet; Teams is step 4.

**Verified:** Windows 534 passed (six POSIX-only modules cannot be imported
there); Linux 668 passed in the worker image with the `slack` and CLI extras.

## 2026-10-19 - Microsoft Teams front end for the chat gateway (II-25116, step 4)

`src/chat_gateway/teams.py` puts the same `Bridge` behind Microsoft Teams;
`python -m chat_gateway teams` runs it, `docs/CHAT_GATEWAY.md` has the setup.
Built on the Microsoft 365 Agents SDK for Python 1.8.0
(`microsoft-agents-hosting-aiohttp`, `microsoft-agents-authentication-msal`),
the successor of the Bot Framework SDK. Only `CloudAdapter`, the JWT
decorator and proactive `continue_conversation` are used; the SDK's
`AgentApplication` routing is not, so the gateway logic stays plain dicts and
is tested with a fake poster.

**How it differs from Slack, and why.**

- Teams cannot be reached through an outbound socket. Microsoft POSTs each
  activity to `/api/messages`, so the gateway needs public HTTPS. The handler
  validates the Microsoft token (SDK decorator; 401 otherwise), returns 202
  at once and runs the turn in a background task, sending every reply
  proactively. Holding the request open for a minutes-long task would make
  Teams retry it after about 15 s and start the work twice.
- Conversation key is `conversation.id`: a channel thread
  (`19:...@thread.tacv2;messageid=...`), a group chat, or the personal chat.
  In channels Teams only delivers messages that @mention the bot, so every
  reply there needs a mention, and the Slack "forgotten thread" hint has no
  equivalent: any message the bot receives is addressed to it.
- Users are keyed by `aadObjectId` (lower-cased). Messages from any tenant but
  `TEAMS_TENANT_ID` are dropped before anything else.
- Proposals are Adaptive Cards with `Action.Submit` buttons carrying the same
  thread / task / pending action / requester data as Slack's buttons. A click
  arrives as a message activity with `value` and `replyToId` = the card. The
  card is rebuilt without its submit buttons after a decision from an
  in-memory copy (bounded at 500); after a restart the rebuilt card only has
  the decision note.
- Teams markdown drops single line breaks, so lone `\n` outside code fences
  becomes `\n\n`.
- `chunks`, `proposal_text` and `says_nothing_new` moved from `slack.py` to
  `rendering.py` so both front ends share them.

**Getting it into Teams (the slow part was permissions, not code).** The
user could create the bot in the Developer Portal (Entra app registration was
allowed), but custom app upload was disabled for normal users, and the Teams
admin center refused them. "Submit an app to your org" plus an admin approval
worked instead; the app then shows under Built for your org. Code, endpoint
and secret changes need no resubmission; package changes do.
`scripts/teams_app_package.py` builds the package (manifest 1.17, icons drawn
in code, no Pillow).

**Live**, 2026-10-19, gateway on the bastion behind a Cloudflare quick tunnel
(the URL changes whenever the tunnel container restarts), against the
deployed test agent: personal chat (`hi`, a TLS question answered via
`run_skill`, `status`), a channel post with an @mention, a proposal card,
Decline (task canceled, card closed with "Declined by"), and Approve, which
opened PR #26 on OnCall-ARI-InfraCore-testing. All 11 POSTs got 202, no
warnings in the log. The client secret was checked separately by acquiring a
Bot Framework token with MSAL.

**Not done here:** the Helm chart (`chatGateway` block with a Service and an
HTTPS ingress for Teams), and four-eyes was not exercised live in Teams (unit
tests only).

**Verified:** Windows 549 passed (six POSIX-only modules cannot be imported
there); Linux 684 passed in the worker image with the slack, teams and CLI
extras, including the SDK wiring test.

## 2026-10-19 - Open-source cleanup: a quickstart that runs, a README to start from, no Jenkins in the docs (II-25116, step 5)

The last step of II-25116: make the repository usable by someone outside
the company. Done after the k8s kind (II-25173, PR #14) merged into `test`,
so the README and design docs are edited on top of its rewrite instead of
conflicting with it.

**Things that were broken, now fixed and run for real.**

- `scripts/run_api.sh`, `scripts/run_worker.sh` and `docker-compose.yaml`
  still ran `ari_agent.main:app` / `ari_agent.orchestration.worker`, a
  package split up on 2026-09-18, and compose built a `Dockerfile` that does
  not exist. Now `api.main:app` / `api.worker`, `Dockerfile.api` /
  `Dockerfile.oncall`, `src/`, `configs/` and `skills/` mounted, and
  `host.docker.internal` mapped so the containers reach the dev Temporal.
- `.env.example` now works as-is against `deploy/temporal-dev`
  (`host.docker.internal:7233`, namespace `default`, no TLS, the current
  queue name `oncall-ari-queue` instead of the pre-rename
  `oncall-ari-fix-queue`).
- Verified on the bastion from a clean copy: dev Temporal up, `docker
  compose up --build` (3 min), agent card 200, `ari send "is example.com
  up?"` answered in 19 s through the tutorial's skill (Temporal history:
  `skill: http-status`, command `scripts/check.py example.com`).
- Found while verifying: the compose containers run as root and wrote
  root-owned `__pycache__` into the mounted `src/`, which the user then
  cannot delete. Compose sets `PYTHONDONTWRITEBYTECODE=1`; the README's test
  command mounts the tree read-only and installs only dependencies. Both
  re-run: 875 passed, no file in the tree owned by anyone but the user.

**Open-source surface.**

- `LICENSE`: Apache-2.0 (the user's choice for now; the leader may still
  decide otherwise).
- `README.md` rewritten as the entry point: what it is, the three ways to
  use it (A2A, `ari`, Slack/Teams), Agent Skills first among the extension
  points, agent kinds, a local quickstart, deployment, layout, testing,
  contributing, license. It says plainly that `ari` needs `DEFAULT_REPO_URL`
  even for a question: the agent does not validate `repo`, but the shared
  bridge refuses to send a request without one. Left as is; a repo-less
  question would need the planner to know there is no repository, or a
  code-change plan crashes on `ctx.config["repo"]`.
- `docs/SKILLS.md` starts with "Write your first skill" (`http-status`),
  the exact files of which were extracted from the doc and run as above.
- `configs/oncall/protected_branches.yaml` named a company repository whose
  entry only repeated the defaults; replaced by `example-org/example-repo:
  [release]`, tests moved to it.
- The commit identity (`support@insightfinder.com`) is now settings
  `COMMIT_AUTHOR_NAME` / `COMMIT_AUTHOR_EMAIL`, same defaults. Not
  `GIT_AUTHOR_*`: git reads those itself and they would leak into every
  subprocess.
- `deploy/k8s/` kept (the user asked to), brought in line with the chart:
  one replica each with `Recreate`, PVCs for the task store and both caches,
  real commands and images, `pvcs.yaml`, a README saying it is for scratch
  clusters.

**Jenkins in the docs.** The Jenkins kind lives only on the `jenkins-agent`
branch. `design.md` lost its Jenkins section; the approval gate it was
explained through is now its own §5b, built on oncall's `propose_pr` ->
`publish_pr` and k8s's `propose_change` -> `apply_approved_change`, every
claim checked against the code. `INPUT_REQUIRED_INTEGRATION.md`'s examples
are now oncall's real `pending_action` fields; `DEVELOPER_GUIDE.md` §9 is a
two-line pointer (its sample client did not exist here); `ARCHITECTURE.md`
uses the k8s kind as the worked example. Also corrected on the way: the
executor docstring that called `INPUT_REQUIRED` out of scope, design.md's
"in-memory task store only", "no cache eviction", and the removed
`publish_pr` opencode fallback still described as live.

**Deliberately not changed.** `common/` still names Jenkins capabilities
(`_OPENCODE_CAPABILITIES` in `capabilities_base.py`) and Jenkins in a few
comments. The `jenkins-agent` branch still merges `common/` from `test`, so
removing those names would silently break it on its next merge. CI image
names stay hard-coded to `docker.io/insightfinderinc`; a fork edits the
workflow.

**Open questions found, not fixed.** A task that pauses twice holds two
`pending_action` artifacts; the chat gateway reads the last one, and a
comment in `meta.py` says MCP Service reads the first. Not verified live.

**Verified:** Windows 576 passed (six POSIX-only modules cannot be imported
there); Linux 875 passed, nothing skipped, in the worker image with every extra.

## 2026-10-20 - A Coordinator for the open-source edition (II-25116)

Two editions now exist. Paying customers reach the agents through UIE and
MCP Service, whose `CoordinatorAgent` picks the agent. Free users get the
open-source agents with neither, so nothing chose an agent: the CLI and the
chat gateway were wired to exactly one. `src/coordinator/` fills that gap;
`docs/COORDINATOR.md` is the guide.

**Decisions (with the user and the leader).**

- The Coordinator is an A2A server, so clients do not change: they point
  `ARI_AGENT_URL` at it. It reuses the SDK server stack, `FileTaskStore`,
  API-key auth and `chat_gateway.a2a_client.AgentClient`, and imports
  nothing from Temporal or the worker, so it ships in the API image.
- v1 sends one request to one agent (oncall, k8s), sticky per conversation.
  Multi-step orchestration across agents is deferred until v1 is tested.
- Routing uses the request's own `llm` config, like the agents: the
  Coordinator holds no model key. Fallback order on any routing failure:
  the conversation's agent, `default_agent`, the first reachable agent. One
  reachable agent means no model call. Routing never fails a request.
- `forward_config` per agent is a security boundary: clients send an
  oncall-shaped config with the GitHub token, and the k8s agent must not
  receive it.
- Proposals, approvals and declines are relayed by id; the agent checks the
  id. Artifacts are republished under the downstream `artifactId`, which the
  SDK replaces rather than duplicates (`append_artifact_to_task`).
- Coordinator task -> agent task links live in
  `TASK_STORE_ROOT/coordinator-routes.json` (atomic, 0600, 7-day prune), so
  a restart resumes following running tasks and keeps waiting proposals
  approvable.
- Deployment: the chat gateways never run in EKS (EKS is for the UIE
  product); they run on a separate EC2. The Coordinator runs in EKS next to
  the agents and reuses them and Temporal. The `oncallari` chart (0.3.8)
  gains `orchestration: mcp | coordinator`; the `chatGateway` block from
  0.3.7 is removed.

**Found while testing:** a request with a wrong `x-api-key` is refused, but
`common/a2a_server/auth.py` on `main` lets the SDK's error handler turn the
401 into a 500. Not a Coordinator problem; the test accepts either.

**Verified:** 15 Coordinator tests drive the real app through the A2A SDK
with two fake agents (route, token filtering, approve, decline, stickiness,
single agent, no agent, agent error, card, auth, restart while paused,
restart mid-run). Windows 561 passed; Linux 696 passed in the worker image.
