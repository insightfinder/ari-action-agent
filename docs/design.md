# ARI Agents — Design

Current-state architecture reference. For *why* things ended up this way,
what's been verified against real infrastructure, and the history of bugs
found and fixed along the way, see [`docs/MEMORY.md`](MEMORY.md) — that's
the decision/verification log; this doc is just what's true today.

## 0. One framework, two agent kinds

This started as a single agent (the on-call agent, §1–§6 below) and has
since been restructured into a small shared framework that any number of
A2A-protocol agent kinds can sit on top of. Today there are two:

| Agent kind | What it does | Package |
|---|---|---|
| `oncall` | Clones a repo, diagnoses a bug/feature request, fixes it, opens a PR | `ari_oncall_agent/` |
| `jenkins` | Reads/diagnoses a Jenkins job or build, proposes and (once confirmed) executes a change | `jenkins_agent/` |

**Package layout** (`src/`):

- **`common/`** — everything agent-kind-agnostic: the A2A server
  (`a2a_server/`), the Temporal workflow/activities (`orchestration/`),
  the planner (`planner.py`), the final-answer synthesizer (`respond.py`),
  the capability/tool registries, settings, sandbox management, the LLM
  client, and `opencode_runtime.py` (the generic opencode
  subprocess/event-parsing/tracing mechanics — see §5). None of this
  imports from either agent package.
- **`ari_oncall_agent/`** / **`jenkins_agent/`** — everything specific to
  one agent kind: its capabilities, its own thin `tools/opencode_cli.py`
  wrapper (scoped opencode agent definitions + per-request config
  writing), its `schemas.py`, and its `meta.py` (below).
- **`api/`** — thin deployable entrypoints only: `api/main.py` (`uvicorn
  api.main:app`) and `api/worker.py` (`python -m api.worker`). Both just
  call into `common/`; neither contains agent-kind-specific logic.

**Which agent kind a process runs as** is one setting,
`settings.agent_kind` (env `AGENT_KIND`, default `"oncall"`). It selects:
which `configs/<agent_kind>/{capabilities,tools}.yaml` the registries load
(`common/config_paths.py`), and which agent kind's `meta.py` module
supplies this process's `AgentMeta` (`common/agent_meta.py`):

```python
@dataclass
class AgentMeta:
    description: str                    # this agent kind's Agent Card description
    skill: AgentSkill                    # this agent kind's Agent Card skill
    planner_description: str             # short phrase the planner's prompt uses to describe itself
    planner_domain_guidance: str         # the domain-specific paragraph in the planner's system prompt
    planner_fallback_steps: list[dict]   # steps used when the planner's own output is malformed
    respond_domain_note: str             # what `respond` should cover, beyond the generic framing
    artifacts: dict[str, str]            # artifact name -> "capability.result_key" to surface on the Task
```

Every place that used to hardcode on-call-specific wording or a
`results.get("publish_pr", {}).get("pr")`-style lookup now reads this
instead — `a2a_server/agent_card.py`, `planner.py`, `respond.py`, and
`orchestration/workflow.py`'s artifact extraction. Adding a third agent
kind means a new package + a new `configs/<kind>/` directory + a new
`meta.py`; nothing in `common/` changes.

**Deployment** is one shared, agent-kind-agnostic API image plus one
worker image *per agent kind* (§7) — each agent kind's API and worker
Deployments are independently toggleable in the Helm chart and can scale
separately.

The rest of this section (§1–§6) describes the on-call agent's own
pipeline in detail; §5b covers the Jenkins agent's own integration; §7–§8
cover deployment and limitations for both.

## 1. What this is

A standalone agent that takes an incident/prompt plus full repo/LLM
configuration in a single request, autonomously clones the repo,
investigates and fixes the problem in an isolated sandbox using
[opencode](https://github.com/sst/opencode), and opens a pull request —
optionally verifying the fix first when the request asks for it.

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

All three run the exact same `AriAgentWorkflow` (agent-kind-agnostic — see
§0) via the exact same `AriAgentExecutor.execute()`
(`common/a2a_server/executor.py`) — there is no second code path.
`context.task_id` (the A2A Task ID) **is** the Temporal workflow ID, one
identifier end to end; Temporal's own workflow history is the audit
trail.

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
3. Starts `AriAgentWorkflow` via the Temporal client, with the A2A task ID
   as the workflow ID.
4. Awaits the workflow's result (this can take minutes; that's fine —
   it's one of many concurrent asyncio tasks in the API process, and it's
   exactly what lets a *blocking* caller's connection stay open until
   completion).
5. Maps the result onto `Task` artifacts (`root_cause`, `pull_request`
   when present) and a final status update (§4).

Not implemented, deliberately: fine-grained per-capability progress as
separate status updates (Temporal's own workflow history already has that
detail — this reports `SUBMITTED → WORKING → one terminal state`).
`TASK_STATE_INPUT_REQUIRED` (a genuine paused "waiting on your reply"
flow) *is* implemented, but only for one specific case today — the
Jenkins agent's `trigger_build` confirmation gate (§5b) — not as a
general mechanism `respond` (§4) can invoke; every other case still
always gets a definitive answer instead of a pause.

## 3. Core abstractions

| Abstraction | What it is | Where (on-call) | Where (Jenkins) |
|---|---|---|---|
| **Request** | Config model — this agent kind's own connection info, `llm`, options. Self-contained, immutable. | `ari_oncall_agent/schemas.py` (`RepoConfig`, `LLMConfig`, `RequestConfig`) | `jenkins_agent/schemas.py` (`JenkinsConfig`, `JenkinsAuthConfig`) |
| **Plan** | Ordered list of `Step`s (capability + params), produced per-request by the planner. | `common/models/plan.py`, `common/planner.py` (shared) | same |
| **Capability** | One pipeline step's behavior. Declared in `configs/<agent_kind>/capabilities.yaml` (description, tools, retry, on_failure); implemented as `run(ctx) -> dict`. | `clone_repo`, `generate_change`, `verify`, `publish_pr` in `ari_oncall_agent/capabilities/` | `resolve_target`, `inspect`, `diagnose`, `propose_action`, `execute_action` in `jenkins_agent/capabilities/` |
| **Tool** | A concrete callable a capability may use. Declared in `configs/<agent_kind>/tools.yaml`. | `git_clone`, `opencode`, `repo_cache`, ... in `ari_oncall_agent/tools/` | `opencode`, `jenkins_stop_build`, ... in `jenkins_agent/tools/` |

Both agent kinds share the same `respond` capability (`common/respond.py`,
`internal: true` in both YAMLs) — it's 100% mechanically generic once its
system prompt is filled from `AgentMeta.description`/`respond_domain_note`
(§0).

Adding a capability or tool = new module + new YAML entry under that
agent kind's `configs/<agent_kind>/`. No changes to
`common/orchestration/workflow.py`, `common/orchestration/activities.py`,
or the API layer — this is the extensibility seam: new purposes later are
new capabilities/tools/catalog entries, not a rewrite. Adding a whole new
*agent kind* is the same shape one level up (§0): a new package, a new
`configs/<kind>/`, a new `meta.py` — `common/` itself never changes.

## 4. Planning, execution, and the response

1. The `plan_task` activity calls `common/planner.py`, which sends the
   prompt + `capabilities.registry.catalog()` (names, descriptions, tools)
   to the request's own LLM and gets back an ordered step list. It honors
   `config.options` (e.g. `skip_tests`) and dynamic branch names in the
   prompt (§6) as explicit directives; it also decides per request whether
   `verify` is worth including at all — opt-in, not part of the default
   pipeline, only included when the request explicitly asks for tests/
   verification (building/testing an arbitrary target repo is this
   pipeline's slowest, least predictable step) — and can tailor a step's
   `instruction` when the generic default wording (bug-fix vs. feature vs.
   edit/refactor) would fit poorly. Malformed planner output falls back to
   the standard clone→change→publish pipeline (no `verify`, matching the
   opt-in default) rather than failing the request.
2. `plan_task` then applies this agent kind's own `default_step_policy`
   (`common/agent_meta.py`, oncall's own in `ari_oncall_agent/meta.py`) to
   the raw plan, if it has one — a deterministic, code-level correction
   for a "by default" behavior that prompt guidance alone can't reliably
   guarantee. Oncall's: `publish_pr` is opt-*out* (the reverse of
   `verify`) — always appended when `generate_change` ran and the
   planner's own step list omitted it, unless `options.skip_pr` or the
   planner's own `skip_publish_pr: true` (set only when the request
   itself said not to open a PR) says otherwise. Confirmed live this
   split was necessary, not just theoretical: a smaller/cheaper planner
   model inconsistently included `publish_pr` for the exact same kind of
   request across separate runs, purely from wording in its own system
   prompt — the same reliability gap `clone_repo` had (§6's
   `require_cloned_repo`), just for a default-behavior policy instead of
   a hard technical prerequisite.
3. `plan_task` enriches each step with its capability's `retry` and
   `on_failure` policy from `configs/capabilities.yaml`, so
   `orchestration/workflow.py` itself never has to read the registry
   (keeps it free of filesystem/import side effects, required for
   Temporal's workflow sandbox).
4. `workflow.py` walks the steps via the generic `execute_step` activity.
   `verify` failing loops back to `generate_change` up to `max_attempts`,
   then the run ends in `fail_needs_human` (`status: "failed"`) rather
   than spinning forever. A step's raised exception (as opposed to a soft
   `{"status": "failed"}` return) is caught the same way, so any hard
   capability failure still reaches `cleanup_workspace` and `respond`
   instead of aborting the workflow with no explanation.
5. Every step happens against one ephemeral sandbox workspace
   (`tools/sandbox.py`, keyed by `request_id`), cleaned up when the
   workflow ends — one request's bad patch/hung test can't affect another.
5. A final `respond` capability **always** runs after the loop above,
   regardless of what the planner chose or whether the pipeline gave up.

**Response shape.** `AriAgentWorkflow.run()` returns one plain dict. Which
top-level keys appear besides `request_id`/`status`/`message`/`data` is
driven entirely by that agent kind's `AgentMeta.artifacts` mapping (§0) —
resolved via the `resolve_artifacts` **activity**
(`orchestration/activities.py`), not a plain function called from
`workflow.py` itself. This split matters, not just style: `AgentMeta`
transitively imports `a2a.types` (for `AgentSkill`), and pulling that
into `workflow.py`'s own module chain breaks Temporal's default
`SandboxedWorkflowRunner` outright — confirmed live, the worker fails to
even start (`RuntimeError: Failed validating workflow AriAgentWorkflow`)
if `workflow.py` imports `agent_meta` at module scope. Same reasoning as
`plan_task` being the one that calls `get_capability()`, never
`workflow.py` — anything touching the capability/agent-meta registries
belongs in an activity. For the on-call agent:

```json
{
  "request_id": "...",
  "status": "completed",
  "message": "Fixed the WaitGroup Add/Wait race in discoverScan.go and added CLI flags for subnet/port range; verify passed; opened PR #1.",
  "root_cause": { "summary": "...", "evidence": [...] },
  "pull_request": { "url": "...", "branch": "ari/change-..." },
  "data": { "...": "raw per-capability results, for programmatic callers" }
}
```

(`pull_request` — not `pr` — is the actual key; an earlier version of
this doc and the workflow disagreed on this, one real inconsistency this
restructuring unified, see `docs/MEMORY.md`.) The Jenkins agent instead
surfaces `pending_action` (from `propose_action`) and `action_result`
(from `execute_action`) when present.

`status`: `completed | failed | no_action | needs_approval` (the last one
reserved — no policy/approval gate exists yet). `common/a2a_server/
executor.py` maps this onto the real A2A wire shape: `message` becomes
the completed `Task`'s final `Message`, every `AgentMeta.artifacts` key
present in the result becomes a named `Artifact`, and `status` maps onto
`TaskState` (`completed`/`no_action` → `TASK_STATE_COMPLETED`, `failed` →
`TASK_STATE_FAILED`, `needs_approval` → `TASK_STATE_INPUT_REQUIRED`,
unused today).

`message` is never a template. It comes from `respond` (`common/
respond.py`, shared by every agent kind — §3) — an `internal: true`
capability (excluded from the planner's catalog, so the planner never
picks it itself) that `workflow.py` calls directly and unconditionally
after the planner-chosen steps finish or give up. It uses the request's
own LLM over the prompt,
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

## 5. opencode + GitHub MCP integration (on-call agent)

opencode does the analysis/editing/test-running for `analyze`/
`generate_change`/`verify`. `publish_pr` is different: its *primary* path
is plain deterministic Python (git subprocess + a direct GitHub REST API
call, `tools/git_ops.py`/`tools/github_rest.py`) — no opencode, no LLM,
no MCP round trip — because everything that step needs (title, body,
branch name, file list) is already known by the time it runs. Confirmed
live that routing it through opencode + the GitHub MCP server (the
original implementation) was the dominant cost of the step: an opencode
subprocess plus several LLM-mediated tool-call round trips (git add, git
commit, git push, then the MCP call) for zero actual reasoning.
`publish_pr` falls back to that original opencode + GitHub MCP flow only
if the primary path raises — kept deliberately, not removed, as a safety
net for whatever an unusual repo/auth state might need that a fixed
git/REST sequence can't adapt to. The subprocess/event-parsing/tracing
mechanics described below (process invocation, `--format json` NDJSON
parsing, `_build_trace`) live in `common/opencode_runtime.py` and are
shared verbatim with the Jenkins agent (§5b) — only the scoped agent
definitions and per-request config (`write_config`) are agent-kind-
specific, in each agent's own thin `tools/opencode_cli.py`.

- **Per-request `opencode.json`**, written into the cloned repo right
  after clone and torn down with the rest of the sandbox:
  `provider.<llm.provider>.options.apiKey` (that request's own key, never
  a shared one), and — since GitHub credentials are also per-request —
  `mcp.github` pointed at `https://api.githubcopilot.com/mcp/` with
  `Authorization: Bearer <repo.pat>` and `oauth: false` (forces PAT auth),
  used only by `publish_pr`'s fallback (see above). Kept out of the repo's
  own git history via `.git/info/exclude`, not `.gitignore` (which
  opencode itself could edit).
- **Four scoped opencode agents**, each `mode: "primary"` (required for
  `--agent <name>` selection to actually take effect): `ari-analyze`
  (read-only, `edit: deny`), `ari-change` (`edit: allow`, no build tooling),
  `ari-verify` (`edit: deny`, broad `bash: allow` since test/build
  commands are unpredictable per-repo), `ari-publish` (`edit: deny`, the
  *only* agent with `github_*` MCP tools enabled — reached only as
  `publish_pr`'s fallback). `analyze`/`generate_change`/`verify` each call
  `opencode run --agent <name> ...` with only the matching agent on every
  run; `publish_pr` only calls it (as `ari-publish`) when its primary path
  fails.
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
- **Output parsing** (`common/opencode_runtime.py:_parse_events`) reads
  `--format json`'s NDJSON event stream: each line's `part.type` is
  `"text"` or `"tool"` (tool parts carry `tool`/`callID`/
  `state.{status,input,output,error}`); a fatal error arrives as a
  top-level `{"type": "error", ...}` event on stdout, not stderr.
  `publish_pr`'s fallback reads the PR URL out of the matching GitHub MCP
  tool call's result, with a regex-over-response-text fallback, same as
  the original implementation. `generate_change` captures the diff via
  plain `git diff HEAD` rather than trusting opencode's own account of
  what it changed.

**LLM client** (`llm/client.py`) uses the official SDKs — `openai>=3.14`
(`AsyncOpenAI`, the Responses API) and `anthropic>=1.6` (`AsyncAnthropic`,
the Messages API) — used only by the planner and `respond`; a fresh client
is constructed per call since the key is per-request, never global.
Unrelated to opencode's own LLM calls, which it manages itself via
`provider.apiKey` in the per-request `opencode.json`.

## 5b. opencode + Jenkins MCP integration (Jenkins agent)

The Jenkins agent has no repo of its own and clones nothing — every real
action against a target Jenkins goes through opencode's built-in MCP
client pointed at *that Jenkins's own* [MCP Server
plugin](https://plugins.jenkins.io/mcp-server/) (`jenkins_agent/
tools/opencode_cli.py:write_config`, mirroring the on-call agent's GitHub
MCP wiring in §5). Jenkins connection details (base URL, auth, CA bundle)
are **not** deployment config — they travel per-request in the `jenkins`
`DataPart`, exactly like the on-call agent's `repo` block (see
`docs/DEVELOPER_GUIDE.md`).

**Capabilities**: `resolve_target` (turns the prompt into a concrete
job/build) → `inspect` (status/history/queue questions) or `diagnose`
(failure analysis, same `SUMMARY:`-marker + enforcement-retry pattern as
the on-call agent's `generate_change`) → `propose_action` (resolves a
requested change into a `pending_action`, never executes) → `execute_action`
(the only capability allowed to actually trigger or stop a build).

**The confirm-before-execute gate is enforced at least twice,
independently, for every action — and a third time, specifically for
`trigger_build`:**

1. **Structurally, in opencode's own permission scoping.** Every
   capability except `execute_action` runs as the `ari-jenkins-read`
   opencode agent, which has every `jenkins_*` MCP tool denied outright —
   a mutating call is refused by opencode itself regardless of what the
   model attempts. Only `execute_action` runs as `ari-jenkins-act`, the
   one agent with `jenkins_*` allowed.
2. **Structurally, in `execute_action.py`'s own code.** It refuses to run
   at all unless `ctx.params` already carries a concrete, non-stale
   (< 30 minutes old) confirmed `action`/`job`/`parameters` — never
   inferred from a bare "yes". For `stop_build`, carrying a prior
   `propose_action`'s exact `pending_action` fields into a later
   confirmation turn's params is the planner's job (its domain guidance
   says so explicitly) — a conversational "yes, go ahead" is enough, same
   as it always was; if there's nothing concrete to confirm,
   `execute_action` should never have been scheduled, and it fails loudly
   rather than guessing.
3. **For `trigger_build` only — a hard, protocol-level gate, not an LLM's
   inference.** `propose_action` flags a `trigger_build` proposal with
   `requires_approval: true`; `orchestration/workflow.py`'s step loop
   turns that into `status: "needs_approval"`, which
   `a2a_server/executor.py` maps onto the real A2A `TASK_STATE_
   INPUT_REQUIRED` (this is what the caller sees — a genuine paused
   "waiting for your input" task, not a completed one). Confirming
   requires a follow-up `message/send` on the *same* `task_id` carrying a
   structured `confirm_pending_action: {"id": "..."}` field — not a plain
   "yes". `executor.py` resolves that id deterministically against this
   task's own previously published `pending_action` artifact (real,
   persisted across turns in `a2a-sdk`'s `InMemoryTaskStore` — verified
   directly against the installed SDK, not assumed from the spec — so no
   LLM ever reconstructs the action/job/parameters from chat text for
   this one action). This resolved `confirmed_action` bypasses the LLM
   planner entirely (`orchestration/activities.py`'s `plan_task` builds a
   one-step plan directly) and sets a `StepContext.confirmed` flag that
   `execute_action.py` checks for `trigger_build` specifically — a flag
   the planner's own JSON output structurally cannot set, since it only
   ever populates a step's `params` (a plain `dict`), never a sibling
   field on the step itself (`common/models/plan.py`'s `Step` schema has
   no such field, and pydantic drops anything extra). Without this,
   layers 1–2 alone would not have stopped the *existing* planner from
   still independently scheduling an unconfirmed `trigger_build` from a
   misread "yes" — layer 3 is what actually makes this a hard gate rather
   than a second path alongside the soft one. See
   [`docs/INPUT_REQUIRED_INTEGRATION.md`](INPUT_REQUIRED_INTEGRATION.md)
   for the caller-side contract — what's actually sent over the wire and
   exactly what a caller needs to send back.

Stopping a build goes through a **direct REST call**
(`jenkins_agent/tools/jenkins_rest.py`), not opencode+MCP — confirmed live
against a real Jenkins (below) that the plugin's tool list has no
stop/cancel tool at all, so this isn't a workaround for an untested edge
case, it's the only way to stop a build. Triggering goes through
opencode+MCP. Mutations are never auto-retried by the engine (`configs/
jenkins/capabilities.yaml`'s `retry.max_attempts: 1` for `execute_action`)
— a timed-out trigger may have already succeeded, and a duplicate build
is worse than surfacing the timeout.

**Verified live** against a real Jenkins instance (2026-09-18, Jenkins
2.568.3, MCP Server plugin 0.203.v12a_6f2a_01d72 — see `docs/MEMORY.md`
for the full session): the plugin's real MCP endpoint
(`{base_url}/mcp-server/mcp`) is a streamable-HTTP JSON-RPC transport
(`initialize` → `Mcp-Session-Id` response header → `notifications/
initialized` → `tools/list`, the latter returned as a single SSE `data:`
event, not plain JSON). It exposes exactly 19 tools, each carrying the
plugin's own `readOnlyHint`/`destructiveHint` annotations: 15 read-only
(`getJob`, `getJobs`, `getBuild`, `getBuildLog`, `searchBuildLog`,
`getQueueItem`, `getTestResults`, `whoAmI`, `getStatus`, `getJobScm`,
`getBuildScm`, `getBuildChangeSets`, `findJobsWithScmUrl`,
`getFlakyFailures`, `getReplayScripts`) and exactly 4 destructive
(`triggerBuild`, `updateBuild`, `rebuildBuild`, `replayBuild`) — confirming
there is genuinely no stop/cancel tool. Separately, opencode's own docs
were checked directly and confirm its MCP tool-naming rule verbatim:
*"MCP server tools are registered with server name as prefix... use
`mymcpservername_*`"* — so this codebase's `"jenkins_*": false` deny
pattern for `ari-jenkins-read` matches real behavior, not just an analogy
to GitHub's `github_*` (the previous state of this doc's caveat).

**Not yet verified live**, because it requires an actual `opencode run`
against a real Jenkins with real credentials, which the same session
could not complete (Claude Code's own safety classifier blocks writing
real secrets into `opencode.json`, and running it manually was deferred
by the operator): that the `jenkins_*` deny pattern is actually *applied*
at runtime by this specific opencode version against this specific MCP
server (docs confirm the rule; never observed in a live log), and
correctness of any capability's actual output (`resolve_target`/
`inspect`/`diagnose`/`propose_action`) against a real job. `execute_action`
has had zero real runs of any kind. See `scripts/jenkins_agent_local_test.py`
for the harness built to close this gap — it deliberately only wires the
four read-only capabilities.

Also not yet verified live: the `trigger_build` confirm gate's actual
pause → follow-up `message/send` → resume round-trip through a real
deployed API process and Temporal worker. Every piece of it was verified
directly against source (the installed `a2a-sdk`'s artifact-continuation
behavior, `Step`'s pydantic schema dropping unknown fields) rather than
assumed, and the full unit-test suite covers every new code path in
isolation — but no real HTTP round-trip against a live instance has
exercised it end to end yet.

## 5c. The same gate, for the on-call agent's pull requests

The on-call agent reuses §5b's three layers verbatim for the one thing it
does that reaches outside the sandbox: opening a pull request. Nothing in
`common/` changed to allow it — the mechanism was already generic, and
turning it on for this agent kind is four lines of `ari_oncall_agent/meta.py`
plus a capability.

- **Pausing.** `propose_pr` returns `requires_approval`, which the step
  loop turns into `status: "needs_approval"` → `TASK_STATE_INPUT_REQUIRED`.
- **Resuming.** `AgentMeta.confirm_capability` is `"publish_pr"`, so
  `plan_task`'s bypass runs that one step with the approved proposal as its
  params and `confirmed: True`, skipping the planner entirely.
- **The gate.** `publish_pr` refuses any step where `ctx.confirmed` is
  false, and refuses a confirmed proposal that is not an
  `open_pull_request` one with every field a pull request needs.

Two things differ from the Jenkins case, both consequences of this agent
owning a working tree:

**The branch is pushed before the pause, not after the approval.** The
approval is human-speed, and the resume is a *separate* Temporal workflow
execution: `workflow.py` runs `cleanup_workspace` on the way out of the
pausing run, and the sandbox root is on no persistent volume, so a worker
restart during the approval window would take the change with it. Pushing
first leaves the approval as one idempotent REST call that needs no
working tree. The accepted cost is a branch visible on the target
repository before anyone approved it — quieter than a pull request, but
not nothing on a repository that builds every pushed branch.

**`publish_pr` is `internal: true`.** The planner must not be able to
schedule it at all; §5b's Jenkins gate relies on `execute_action` being
plannable but refusing unconfirmed work, which leaves prompt guidance as
the only thing discouraging a plan from trying. Here the capability is
simply absent from `catalog()`, so there is nothing to discourage.
`get_capability()` — which the confirmation bypass uses — does not filter
internal capabilities, so the resume still resolves it.

## 6. Repo caching & dynamic branches (on-call agent)

On-call-agent-specific — the Jenkins agent has no repo of its own, so
none of this applies to it (§5b). Two related requirements: many requests
target the same repo over time
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
replica (the Helm chart's PVC is `ReadWriteOnce` today, matching
`worker.replicaCount: 1`). LRU eviction (`common/cache_gc.py`) kicks in
when the backing PVC is low on free space — see the agent's own
docs/MEMORY.md for why both `repoCacheRoot` and `buildCacheRoot` are
deliberately small (5Gi) rather than sized to never fill up.

**Dynamic branches**: `clone_repo` accepts an optional `ref` step param
(which branch to check out — falls back to `repo.ref`, then to the
remote's own detected default branch when neither is set), and
`publish_pr` accepts an optional `base_ref` step param (which branch the
PR targets — falls back to the same default, independently of whatever
`ref` was). The planner's system prompt reads the prompt for named
branches and sets these params.

`ensure_cache_and_clone` (`tools/repo_cache.py`) never hardcodes a branch
name, and never just trusts a given `ref` either: `git_branch_exists`
checks it against the mirror *before* attempting the clone, and falls
back to `git_default_branch` (reads the mirror's real `HEAD` symref —
`main`/`master`/`dev`/anything) whenever `ref` is `None` or doesn't
actually exist. Confirmed live this second case matters, not just the
first: the planner guessed `ref="main"` for a repo whose real default
branch is `"master"`, despite the prompt never naming a branch at all —
prompt guidance alone isn't reliable enough to prevent that (the same
lesson as `clone_repo`/`publish_pr` elsewhere in this doc). Checking
existence up front means exactly one clone attempt either way — no
failed-then-retried clone, no matching on git's own error text.

`repo.url` can also be a local filesystem path, not just a network
remote — git supports this natively.

**`StepContext.require_cloned_repo`** (`common/capabilities_base.py`):
`generate_change`/`verify`/`publish_pr` call this instead of reading
`repo_dir` directly — it raises immediately and clearly if `clone_repo`
never actually ran, rather than silently falling back to the empty
sandbox root (`repo_dir`'s own fallback, correct for a repo-less chat
prompt or an agent kind with no `clone_repo` capability at all, e.g.
Jenkins) and only failing three layers down inside opencode with an
opaque `Unexpected server error`. Confirmed live: a planner using a
smaller/cheaper model produced a step list that included `generate_change`
but dropped `clone_repo` — exactly this failure, caught and fixed by
adding the guard plus sharpening `planner_domain_guidance` to state the
ordering requirement explicitly.

**Protected branches** (`ari_oncall_agent/protected_branches.py`): a
configurable blacklist of branches this agent must never push to directly
(`configs/oncall/protected_branches.yaml` — a per-repo `protected_branches`
map keyed by repo URL, matched by owner/repo slug so scheme/`.git`-suffix
differences don't cause a miss; read-only access — clone/fetch/pull, and a
PR opened *against* one of these branches via `base_ref` — is always
unaffected, only a direct push to the branch itself is blocked).
`main`/`master` are protected for *every* repo by default
(`default_protected_branches` in the same file, itself configurable — an
explicit `[]` is the only way to turn it off, omitting the key keeps the
default), added on top of whatever a repo's own entry lists, not a
replacement for it. Two different enforcement points, because there are
two different ways `publish_pr` can end up pushing:
- Its deterministic primary path (`tools/git_ops.py:push`) always targets
  a freshly generated `ari/<slug>-<request-id>` branch, never an existing
  one, so a hard veto there (`publish_pr.run`, checked before anything
  touches git) is unreachable in normal operation — kept as defense-in-
  depth against a future change letting a caller-supplied branch name
  reach that point.
- Its opencode + GitHub MCP **fallback** (`ari-publish`, real `git push`
  bash access, only reached if the primary path fails) is the one path
  that actually needs protecting: `tools/opencode_cli.py:write_config`
  computes explicit bash-permission `deny` entries for the common `git
  push` forms against each of this repo's protected branches, and the
  fallback's own instruction names them in plain language too.
  **Best-effort, not a hard guarantee** — opencode's permission matching
  is prefix + trailing-wildcard only, so it can't semantically parse
  every possible git push invocation, and it has no visibility at all
  into the GitHub MCP server's own tools. Real branch protection
  configured on the GitHub repo itself (restrict who/what can push) is
  the only guarantee, and covers the MCP path too — see
  `protected_branches.py`'s own docstring.

`ari-verify`'s bash permission denies `git push*` unconditionally
(regardless of branch, not just protected ones) for the same reason —
verify has no legitimate reason to push anything, ever, and its bash is
otherwise fully open for arbitrary build tooling.

## 7. Deployment images

Three dedicated Dockerfiles at the repo root, not one dual-entrypoint
image — chosen so each process only carries the toolchain it actually
needs, and so the two agent kinds' worker images can differ completely
without bloating either:

- **`Dockerfile.api`** — the FastAPI/A2A process (`uvicorn api.main:app`).
  Agent-kind-agnostic and shared by both agent kinds' API Deployments
  (which agent kind's card/skill it serves is pure runtime config —
  `AGENT_KIND`/`AGENT_NAME`/`AGENT_PUBLIC_URL`, verified live: the same
  built image returns a different `description` depending only on
  `AGENT_KIND`). Just `python:3.12-slim` + `pip install .` — this process
  never shells out to git/opencode/a build toolchain itself, only the
  worker does.
- **`Dockerfile.oncall`** — the on-call agent's worker (`python -m
  api.worker`, `AGENT_KIND=oncall`). Carries everything §5's opencode
  integration and `verify` need:
  - **`git`, `curl`, `ca-certificates`** — `clone_repo` and friends shell
    out to `git` directly. `git config --system --add safe.directory '*'`
    is also set at build time, since cloning a directory owned by a
    different user than the container process (e.g. a bind-mounted host
    path) otherwise hits git's "detected dubious ownership" refusal —
    this must be real git config, not a per-command `-c` override.
  - **Node.js 22 + `opencode-ai`** pinned to an exact version — floating
    to `latest` would silently invalidate the behavior this agent's
    opencode integration (§5) was built and verified against; bump the
    pin deliberately and re-verify.
  - **`golang-go` + `openjdk-21-jdk-headless` + `maven`** — needed for
    `verify` to build/test *target* repos written in Go or Java, not for
    ARI itself. A deliberate, bounded bet: Go/Java/Node/Python are the
    languages this agent has actually been exercised against. Not full
    multi-language coverage — a target repo needing Rust, .NET, etc. will
    clone/analyze/fix fine (opencode doesn't need the target's own
    toolchain for that) but `verify` will fail with "command not found,"
    correctly surfaced as a real failure rather than silently succeeding.
    Extend this list as real requests need more languages, or revisit
    with a per-request build sandbox (Docker-outside-of-Docker or an
    external build service) if the language spread outgrows "just add it
    to the image."
- **`Dockerfile.jenkins`** — the Jenkins agent's worker (`python -m
  api.worker`, `AGENT_KIND=jenkins`). Deliberately much smaller: only
  Node.js 22 + the same pinned `opencode-ai` version, no git and none of
  the build toolchain above — this agent never clones or builds anything
  (§5b), so none of that is needed.

**Helm chart** (`insightfinder/charts`, `charts/oncallari`): each agent
kind is an independently toggleable block (`ariOncallAgent.enabled`,
`ariJenkinsAgent.enabled`) with its own api+worker Deployment pair, own
Secret, Service, and Ingress, and its own Temporal namespace/task queue
(`oncall-ari-agent`/`oncall-ari-queue` and `jenkins-ari-agent`/
`jenkins-ari-queue` respectively) against one shared Temporal dev server.
Both agent kinds' API Deployments reference the same `Dockerfile.api`
image (different `.api.image` value in the chart only because the chart
allows pinning them independently, not because the image differs); each
agent kind's worker Deployment references its own dedicated worker image.
This chart currently only supports "Mode A": a fully separate API+worker
Deployment pair per agent kind. A single shared API process serving
multiple agent kinds behind one Deployment ("Mode B") would need
`api/main.py` itself to mount multiple agent kinds' A2A apps under path
prefixes in one process — not implemented, so there's no chart toggle for
it yet.

## 8. Known limitations

**Resolved since earlier versions of this doc** (kept here only as a
pointer — see `docs/MEMORY.md` for what was actually wrong and how each
was fixed): secrets in Temporal's event history are no longer plaintext
(field-level `EncryptionCodec` masks `api_key`/`pat`/`ssh_key` values
only, leaving the rest of the payload readable in the Temporal UI by
design); a failed capability no longer skips `cleanup_workspace`/
`respond`; `publish_pr`'s PR title/body quality bug (twice — the second
fix detects-and-retries instead of falling back to raw narration);
`publish_pr`'s real ~200s redundant-re-read bottleneck (found via the
tracing below, not a guess).

**Still open, on-call agent:**

- **gRPC transport** not implemented — needs its own server process/port
  and `grpcio`/`grpcio-tools` dependencies. Deferred, not abandoned.
- **`TASK_STATE_INPUT_REQUIRED`** not implemented — no genuine paused
  "waiting for your reply" flow; a caller follows up by sending a new
  message in the same context instead.
- **Push-notification delivery retry semantics** are the SDK's
  `BasePushNotificationSender`'s concern, not audited here.
- **No repo-cache eviction/GC** — fine for a bounded set of well-known
  repos.
- Concurrency/volume expectations are not yet established — informs
  worker replica count and Temporal task-queue sizing.
- Git providers beyond GitHub/GitLab are untested.

**Still open, Jenkins agent** (see §5b for what *is* verified):

- **No jenkins_agent capability has completed a real end-to-end run** —
  everything verified so far is at the raw MCP-protocol/plugin level, not
  through an actual `opencode run`. `execute_action` in particular has
  never run at all, against anything.
- **`JenkinsConfig.allow_mutations`** (`jenkins_agent/schemas.py`) is
  design intent only — nothing in code enforces it today. The only real
  enforcement is the two-layer gate in §5b (opencode's own tool
  permissions, plus `execute_action`'s own confirmed-action check); a
  request-level "force read-only regardless of what the token could do"
  switch doesn't exist yet.
- **No Jenkins-side RBAC recommendation is enforced by this codebase** —
  the design assumes a caller *may* supply a restricted, read-only
  service-account token, but nothing here requires or checks that. A
  token belonging to a full-access account gets exactly the same
  treatment; Jenkins's own permissions are the only backstop in that
  case, not anything in this agent.
- **TLS/CA-bundle env var handling for opencode's Node MCP client**
  (`NODE_EXTRA_CA_CERTS`/`NODE_TLS_REJECT_UNAUTHORIZED` in `tls_env()`) is
  Node's own documented mechanism but has never been exercised against a
  real self-signed/private-CA Jenkins.
- **A single Jenkins request travels over plain HTTP** if `jenkins.
  base_url` is `http://` (as the one real instance checked so far is) —
  the Basic-auth header goes over the wire unencrypted in that case. Not
  a code gap to fix here; a deployment/network concern for whoever
  configures `base_url`.
