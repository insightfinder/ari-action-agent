# ARI On-Call Agent — Design (v2: standalone, Temporal-only)

This supersedes `multi-agent-ai/docs/ari-oncall-agent-design.md` for this
service. Differences from that earlier draft, per updated requirements:

- **Temporal only** — no `inline` execution engine. Temporal is hosted
  externally; this service is purely a client + worker against it.
- **Standalone A2A agent** — its own folder (`on-call-ari-agent/`), its own
  deploy (EC2/K8s), no dependency on the rest of the `multi-agent-ai`
  monorepo (`shared/`, other services, their DB). Auth is a static API key
  per request, not session/OAuth. **Genuinely spec-compliant A2A** as of
  2026-09-15 (JSON-RPC + REST, built on the official `a2a-sdk`) — see §6;
  an earlier version only borrowed A2A's vocabulary without being
  wire-compatible, see `docs/MEMORY.md` for why that changed.
- **Every config lives in the request payload** — LLM provider/key/model,
  repo URL + credentials, an optional system-prompt override, and free-form
  `options` (e.g. `skip_tests`) — as a `DataPart` alongside the prompt's
  `TextPart` (see §6). No env-var indirection for secrets, no server-side
  config store — the service is stateless and every request is independent.
  There's no `callback` field in this config anymore — delivering a result
  asynchronously is the A2A protocol's own concern
  (`pushNotificationConfig`), not this agent's.
- **opencode does the coding work** — analyze/fix/verify are wrappers
  around opencode running in a sandboxed workspace, not bespoke
  LLM-tool-calling loops.
- **Dynamic planning, not a static YAML pipeline** — prompts vary too much
  ("also skip tests", "just open the PR", a bare question) to hardcode a
  step sequence. A planner (using the request's own LLM) reads the
  capability catalog and prompt and emits the step list for *this* request.
- **No persistence** — no chat history store, no request database. Chat
  callers resend history each turn; task callers correlate on `request_id`.

## 1. One A2A operation, three ways to get the result

There is exactly one operation, A2A's `message/send` (`SendMessage` in
JSON-RPC/gRPC terms) — no separate "chat" vs. "task" endpoint. What differs
is how the *caller* chooses to receive the result, entirely via standard A2A
mechanics (see §6 for the full protocol detail):

| | Blocking | Non-blocking + polling | Non-blocking + webhook |
|---|---|---|---|
| Caller gets | Final `Task`, inline | `Task` (submitted) immediately | `Task` (submitted) immediately |
| Completion | Same call blocks until terminal state | Caller polls `tasks/get` | SDK POSTs progressive updates to a registered `pushNotificationConfig` |
| Was previously | `POST /v1/chat` | `POST /v1/tasks` + `GET /v1/tasks/{id}` | `POST /v1/tasks` + `config.callback` |

All three run the exact same `AriOnCallWorkflow` via the exact same
`AriAgentExecutor.execute()` (§6) — there is no second code path to keep in
sync, and this workflow itself doesn't know or care which of the three a
given caller is using.

`context.task_id` (the A2A Task ID) **is** the Temporal workflow ID — one
identifier end to end, matching how Temporal's own history becomes the
audit trail (no separate step-log table needed).

## 2. Core abstractions (unchanged in spirit from v1, now Temporal-native)

| Abstraction | What it is | Where |
|---|---|---|
| **Request** | `RequestConfig` — repo, llm, system_prompt override, options. Self-contained, immutable, no callback field (that's the A2A protocol's own concern now, see §6). | `api/schemas.py` |
| **Plan** | Ordered list of `Step`s (capability + params), produced per-request by the planner. | `models/plan.py`, `planning/planner.py` |
| **Capability** | One pipeline step's behavior (`clone_repo`, `analyze`, `generate_fix`, `verify`, `publish_pr`). Declared in `configs/capabilities.yaml` (description, tools, retry, on_failure); implemented as `run(ctx) -> dict` in `capabilities/`. | `capabilities/` |
| **Tool** | A concrete callable a capability may use (`git_clone`, `opencode`, `repo_cache`, ...). Declared in `configs/tools.yaml`; implemented in `tools/`. | `tools/` |

Adding a capability or tool = new module + new YAML entry. No changes to
`orchestration/workflow.py`, `orchestration/activities.py`, or the API
layer. This is the extensibility seam requested — new purposes later are
new capabilities/tools/catalog entries, not a rewrite.

## 3. Planning, then execution

1. `plan_task` activity calls `planning/planner.py`, which sends the
   prompt + `capabilities.registry.catalog()` (names, descriptions, tools)
   to the request's own LLM and gets back an ordered step list. It also
   honors `config.options` (e.g. `skip_tests`) as explicit directives.
   Malformed planner output falls back to the standard
   clone→analyze→fix→verify→publish pipeline rather than failing the
   request.
2. `plan_task` enriches each step with its capability's `retry` and
   `on_failure` policy from `configs/capabilities.yaml`, so the workflow
   itself never has to read the registry (keeps `workflow.py` free of
   filesystem/import side effects — required for Temporal's workflow
   sandbox).
3. `workflow.py` walks the steps, calling the generic `execute_step`
   activity for each. `verify` failing loops back to `generate_fix` up to
   `max_attempts`, then the run ends in `fail_needs_human` (`status:
   "failed"`) rather than spinning forever.
4. Every step happens against one ephemeral sandbox workspace
   (`tools/sandbox.py`, keyed by `request_id`), cleaned up when the
   workflow ends — one request's bad patch/hung test can't affect another.
5. A final `respond` capability **always** runs after the loop above,
   regardless of what the planner chose or whether the pipeline gave up —
   see §4.

## 4. Response shape

Internally, `AriOnCallWorkflow.run()` still returns one plain dict, the same
shape regardless of how the caller is receiving it:

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
reserved, unused today — no policy/approval gate exists yet, matching v1's
"placeholder only" stance). `a2a_server/executor.py:_publish_result` maps
this onto the real A2A wire shape: `message` becomes the completed `Task`'s
final `Message`, `root_cause`/`pr` (when present) become named `Artifact`s,
and `status` maps onto `TaskState` (`completed`/`no_action` →
`TASK_STATE_COMPLETED`, `failed` → `TASK_STATE_FAILED`, `needs_approval` →
`TASK_STATE_INPUT_REQUIRED`, though nothing produces that last one yet) —
see §6 for the full mapping and `docs/DEVELOPER_GUIDE.md` for what a caller
actually receives.

**`message` is never a template.** It comes from `respond`
(`capabilities/respond.py`) — an `internal: true` capability (excluded
from `capabilities.registry.catalog()`, so the planner never picks it
itself) that `orchestration/workflow.py` calls directly and
unconditionally after the planner-chosen steps finish or give up. It uses
the request's own LLM (`llm_complete` tool → `llm/client.py:complete`)
over the prompt, conversation `history`, and whatever step results exist:

- **No steps ran** (a plain question, or a chat turn already answered by
  history) → `respond` just answers the question directly. This is the
  fix for a real bug: chat prompts used to get back a canned
  `"No repository action was needed for this request."` even when they
  were genuine questions expecting a real answer.
- **Steps ran** → `respond` synthesizes root cause, what changed and why,
  verify status, and the PR link into one coherent summary — not
  `f"Opened PR: {url}"`.
- **Pipeline gave up** (`fail_needs_human`) → `respond` explains what's
  blocking it, using `failure_reason`.

If `respond` itself fails after retries (an `ActivityError` reaching
`workflow.py`), a templated one-liner is used as a last resort
(`_fallback_message`) — the request must never come back with no message
at all, but synthesis quality is allowed to degrade before availability is.

`history` (prior turns) is threaded from the A2A layer
(`a2a_server/executor.py:_extract_history`, reconstructed from the current
`Task`'s own accumulated `history` — see §6) through `plan_task` (so the
planner can resolve "yes, go fix it" against earlier turns) and into every
`StepContext` (a `history` field, `[]` for any capability that doesn't need
it).

**`message` always states things definitively, never a dangling question.**
Every request is delivered the same way from `respond`'s point of view — a
caller may block, poll, or register a webhook, and this capability has no
idea which — so its system prompt forbids ending with a question or an
offer conditioned on a reply ("which should I do next?"). (Found live: a
response once ended with exactly that kind of dangling question — no
guaranteed channel for an answer, since this agent doesn't implement A2A's
`TASK_STATE_INPUT_REQUIRED` pause/resume flow in this pass. Fixed by adding
this rule; verified against a real run that the final message now ends with
a plain recommendation instead. This used to be conditioned on a `chat` vs.
`task` "mode" — retired along with the mode split itself, see §6.)

## 5. opencode + GitHub MCP integration (decided, implemented)

opencode does all the actual analysis/editing/test-running *and* the
GitHub PR operations — the latter via opencode's own built-in MCP client
pointed at the public GitHub MCP server, not a custom Python MCP client.
Concretely (see `tools/opencode_cli.py`):

- **Per-request `opencode.json`**, written into the cloned repo dir right
  after clone (`clone_repo` capability) and torn down with the rest of the
  sandbox: `provider.<llm.provider>.options.apiKey` (that request's own
  key — never a shared/global one), and, since GitHub credentials are also
  per-request, `mcp.github` pointed at
  `https://api.githubcopilot.com/mcp/` with `Authorization: Bearer
  <repo.pat>` and `oauth: false` (forces PAT auth, skips opencode's OAuth
  discovery attempt). Kept out of the repo's own git history via
  `.git/info/exclude`, not `.gitignore` (which opencode itself could edit).
- **Four scoped opencode agents** — `ari-analyze` (read-only, `edit:
  deny`), `ari-fix` (`edit: allow`, no build tooling), `ari-verify` (`edit:
  deny`, broad `bash: allow` since test/build commands are unpredictable
  per-repo), `ari-publish` (`edit: deny`, and the *only* agent with
  `github_*` MCP tools enabled — every other agent has them off by
  default). `analyze`/`generate_fix`/`verify`/`publish_pr` each call
  `opencode run --agent <name> ...` with only the matching agent.
- **One ephemeral `opencode run` process per capability step**, not a
  long-lived `opencode serve` — a Temporal activity is supposed to be a
  self-contained unit of work, and Temporal doesn't guarantee two activity
  invocations for the same workflow land on the same worker, so a
  subprocess/session kept alive across activity boundaries would be
  exactly the kind of hidden cross-activity state the model is meant to
  avoid. Continuity across steps instead comes from opencode's own
  `--continue` flag (resume the most recent session for that `--dir` from
  disk), gated on `ctx.opencode_session_started` — no session ID needs to
  be threaded through Temporal payloads.
- **`--auto`** (auto-approve anything not explicitly denied) is a backstop,
  not the primary control — agent permission blocks only ever use
  `allow`/`deny`, never `ask`, since nothing is present to answer a prompt.
- **Output parsing** (`_parse_events` in `tools/opencode_cli.py`) reads
  `--format json`'s event stream defensively (opencode's docs don't
  publish the exact wire schema; this was reverse-engineered from
  `session/message-v2.ts` in the `dev` branch — Part shapes `text` /
  `tool` / `reasoning` / `step-start`, tool parts carrying
  `tool`/`callID`/`state.{status,input,output,error}`). `publish_pr` reads
  the PR URL out of the `github_*_pull_request` tool call's result rather
  than trusting free text, with a regex-over-response-text fallback.
  `generate_fix` captures the diff via plain `git diff HEAD` rather than
  trusting opencode's own account of what it changed.

### 5.1 Verified live against a real opencode binary (2026-09-15)

Installed `opencode-ai` (npm, v1.18.31) locally and ran it against a
scratch git repo with a deliberately invalid OpenAI key — no live model
call, but this exercises real CLI parsing, config validation, and error
plumbing, not just documentation. Two things the docs didn't cover turned
out to matter:

- **`agent.<name>.mode` must be `"primary"`, not `"subagent"`.** The first
  version of `_AGENT_DEFS` used `"subagent"` (following the MCP docs'
  own example agent), and `opencode run --agent ari-analyze ...` **silently
  fell back to the built-in `build` agent** — no error, just a one-line
  warning (`agent "ari-analyze" is a subagent, not a primary agent. Falling
  back to default agent`) visible only with `--print-logs --log-level
  DEBUG`. That would have made every permission scope in this design a
  no-op — `ari-fix` would have gotten `build`'s full default permissions
  regardless of what we wrote for it. Fixed: all four agents now declare
  `"mode": "primary"`. Confirmed the fix by checking the debug log showed
  `agent=ari-analyze` / `ari-fix` / `ari-verify` / `ari-publish` for all
  four, and re-validated the full `write_config()` output (agents + the
  `mcp.github` block together) the same way — config loads and every agent
  is selected correctly; the `mcp.github` entry with a bogus PAT logs
  `"server unavailable" key=github status=failed` (a warning, not fatal)
  for **every** agent's run, not just `ari-publish` — opencode attempts the
  configured MCP connection at startup regardless of which agent's `tools`
  block actually exposes it. Harmless (one extra failed/slow connection
  attempt per step for analyze/fix/verify), just worth knowing.
- **A fatal error is `{"type": "error", "error": {"data": {"message":
  ...}}}` on stdout, not stderr.** stderr was completely empty even on a
  hard 401. The original `run_opencode` raised `RuntimeError` using
  `stderr.decode()`, which would have produced a useless blank error
  message in production. Fixed: `_parse_events` now has an
  `_extract_error()` pass that finds this event shape first, and
  `run_opencode` prefers it over stderr when the process exits non-zero.
- `--format json` is confirmed NDJSON — one JSON object per stdout line —
  and `--dir`/`--agent`/`--model`/`--auto`/`--continue`/`-c` all match
  what's implemented.

### 5.1.1 Full live run with real OpenAI/Anthropic keys (2026-09-15)

Re-ran the same opencode binary against a scratch repo seeded with a real
bug (`cart.items` iterated without a `None` check), this time with real
API keys and `--model openai/gpt-4o-mini`, through `ari-analyze` →
`ari-fix` → `ari-verify` in one continued session. Confirmed end to end,
not just in principle:

- **`_parse_events`'s success-path handling was correct as originally
  written**, no changes needed. The wire format: every stdout line is
  `{"type": "<event-kind>", "timestamp", "sessionID", "part": {...}}` where
  `event-kind` is an envelope type (`step_start`/`tool_use`/`step_finish`/
  `text`, etc.) that we ignore — what matters is `part.type`, which *is*
  exactly `"text"` (with `part.text`) or `"tool"` (with `part.tool`,
  `part.callID`, `part.state.{status,input,output}`), matching the
  `message-v2.ts`-derived assumption exactly.
- `ari-analyze` correctly stayed read-only and used `glob`/`read` tools to
  find and inspect the buggy file; its `root_cause.summary` was accurate.
- `--continue` correctly resumed context into `ari-fix`, which produced a
  minimal, correct one-line fix (`cart.items` → `cart.items or []`) —
  `edit: allow` worked, and `tools/git_ops.py:diff` captured the real
  patch.
- `ari-verify` (given no test framework existed) wrote its own throwaway
  test, ran it, and correctly ended its response with `RESULT: PASS` — the
  free-text convention `capabilities/verify.py` relies on holds up against
  a real model.
- **Found a real gap this way**: `ari-verify` left its scratch test file
  and `__pycache__/` sitting untracked in the working tree despite being
  asked to clean up after itself. Since `ari-publish` only had `bash: {"git
  *": allow}`, a naive `git add -A` there could have swept those into the
  PR. Fixed by having `generate_fix` also capture
  `git diff --name-only HEAD` (`tools/git_ops.py:changed_files`) and
  telling `ari-publish` explicitly which files to stage (never `git add
  -A`/`git add .`) — verified against the same scratch repo that
  `changed_files`/`diff` correctly include only `cart_service.py`, not the
  leftover test file.
- `publish_pr` itself (opening a real PR via the GitHub MCP server) was
  **not** exercised — that needs a real GitHub PAT and a disposable repo,
  which is a different, more consequential ask than an LLM smoke test.

Real API keys used for this were never written to any file in this repo,
never logged, and not retained in memory — only used transiently in a
throwaway venv/scratch dir, deleted immediately after.

### 5.1.2 Full pipeline against a real repo, including publish_pr (2026-09-15)

Ran `clone_repo → analyze → generate_fix → verify → (retry) generate_fix →
verify → publish_pr` against a real public repo
(`github.com/neyaadeez/go-script`, a small Go TCP port scanner) with a real
GitHub fine-grained PAT and a real OpenAI key (`gpt-5-mini`), asked to
"introduce some new feature" — deliberately open-ended, to see what the
planner/analyze step would do with it unassisted:

- `ari-analyze` correctly identified a real, sensible improvement (the
  scanner spawned one goroutine + `net.DialTimeout` per port with no
  concurrency bound) and proposed bounding it with a buffered-channel
  semaphore — good engineering judgment, not a throwaway feature.
- `ari-fix` implemented exactly that, a genuinely minimal one-file diff.
- `ari-verify` (`go build`, `go vet`, `gofmt -l`, `go test`) **correctly
  caught a real gofmt violation** or in the fix and reported `RESULT:
  FAIL` — the retry loop design (verify → generate_fix → verify) was
  exercised for real, not simulated: feeding the failure back to `ari-fix`
  produced a `gofmt`-clean fix on the second attempt, verify then passed.
  `tools/git_ops.py:changed_files`/`diff` correctly tracked only the one
  real file across both attempts.
- `ari-publish` — first attempt **failed in a way that revealed a real
  gap**: the instruction never stated which GitHub repo to target, so
  opencode asked for clarification (`owner/repo`?) instead of inspecting
  the working tree's git remote itself. Fixed `capabilities/publish_pr.py`
  to state `Repository: <owner>/<repo> (<url>)` explicitly (new
  `_repo_slug()` helper) — confirmed this resolved it, `ari-publish` then
  correctly called the `github_create_branch` MCP tool with the right repo.
- That MCP call then hit a **real `403 Resource not accessible by personal
  access token`** from GitHub's own API — confirmed independently (outside
  opencode entirely) by POSTing the same `git/refs` endpoint directly with
  the same PAT and getting the identical 403. This is a token
  permission-scope issue (this fine-grained PAT wasn't granted "Contents:
  Read and write" for that repo), not a bug anywhere in ARI — the
  integration made the right call, got a real API response, and
  `opencode`/`ari-publish` correctly surfaced the failure with actionable
  next steps rather than silently failing. **Still not exercised**: an
  actual successful branch-push + PR creation — needs a token with
  `contents:write` + `pull_requests:write` on the target repo (fine-grained)
  or classic `repo` scope.

Same handling as before: real keys used transiently, never written to any
tracked file or memory, deleted immediately after (including confirming no
leaked key strings anywhere in the working tree).

### 5.2 LLM client (`llm/client.py`)

Uses the official SDKs — `openai>=3.14` (`AsyncOpenAI`, the **Responses
API**: `client.responses.create(model=, instructions=, input=)` →
`response.output_text`) and `anthropic>=1.6` (`AsyncAnthropic`,
`client.messages.create(model=, max_tokens=, system=, messages=)` →
concatenate `content` blocks where `block.type == "text"`) — rather than
raw HTTP, verified by inspecting both packages' actual source (`base_url`
constructor param, `responses.create`/`messages.create` signatures,
`Response.output_text` property, `Message.content` shape) since their
published docs were thin on the async path. A fresh client is constructed
per call (no caching) since the key is per-request, never global. This is
used only by the planner to pick a step plan — unrelated to opencode's own
LLM calls, which it manages itself via `provider.apiKey` in the per-request
`opencode.json` (§5).

Both paths (and the planner end to end, including honoring a `skip_tests`
directive by omitting the `verify` step) were confirmed 2026-09-15 against
real OpenAI and Anthropic API calls, not just mocked — see §5.1.1 for the
opencode side of the same verification pass.

`capabilities/`, `orchestration/`, and `api/` are complete and smoke-tested
(FastAPI app boots, all routes resolve, workflow/activities import and
register with a `Worker` without error, schema round-trips the sample
payload from the requirements). Unit tests: `tests/test_opencode_cli.py`
(agent-mode regression test, config shape, event parsing against the
actual captured error JSON) and `tests/test_llm_client.py` (SDK call shape,
mocked).

## 6. A2A protocol integration (decided, implemented, live-verified)

Built on the official [`a2a-sdk`](https://pypi.org/project/a2a-sdk/) (a
Linux Foundation project), not a hand-rolled REST API imitating A2A's
vocabulary. **JSON-RPC + REST now, gRPC deliberately deferred** — a scoping
decision made explicitly: gRPC needs a genuinely separate server process
(HTTP/2 + protobuf framing, not ASGI), its own port, and `grpcio`/
`grpcio-tools` codegen dependencies, vs. JSON-RPC and REST which mount
straight into the existing FastAPI app via one SDK call. The alternative
considered — hand-implementing all three transports ourselves — was
rejected once the SDK was found to already do exactly this (see
`docs/MEMORY.md` for the research that established this).

### 6.1 What we implement vs. what the SDK owns

We implement exactly one interface, `AgentExecutor`
(`a2a_server/executor.py:AriAgentExecutor`) — `execute(context,
event_queue)` and `cancel(...)`. The SDK owns everything else: Task/Message/
Part protobuf modeling, Agent Card serving, JSON-RPC and REST framing,
`tasks/get` polling, and push-notification delivery.

`main.py` wires it together:

```python
request_handler = DefaultRequestHandler(
    agent_executor=AriAgentExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=build_agent_card(settings),
    push_config_store=InMemoryPushNotificationConfigStore(),
    push_sender=BasePushNotificationSender(httpx_client=..., config_store=...),
)
add_a2a_routes_to_fastapi(
    app,
    agent_card_routes=create_agent_card_routes(agent_card),
    jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url="/", context_builder=ApiKeyCallContextBuilder()),
    rest_routes=create_rest_routes(request_handler, context_builder=ApiKeyCallContextBuilder()),
)
```

`InMemoryTaskStore`/`InMemoryPushNotificationConfigStore` are the correct
choice here, not a shortcut: this agent stores no request state of its own
by design (§ intro), so these only give `tasks/get` and best-effort
per-task `history` something to read within this process's lifetime — a
restart means callers re-register, same as re-submitting a request against
any stateless service.

### 6.2 The executor — bridging A2A's Task lifecycle to our Temporal workflow

`AriAgentExecutor.execute()`:

1. Extracts the prompt (`context.get_user_input()` — joins `TextPart`s) and
   this agent's own config (a `DataPart` alongside it — see §6.4) and prior
   `history` (reconstructed from `context.current_task.history`, when this
   message continues an existing task/context).
2. Enqueues the initial `Task` object (state `SUBMITTED`) — this is what
   lets a non-blocking caller's `message/send` response return immediately.
3. `client.start_workflow(AriOnCallWorkflow.run, ..., id=context.task_id, ...)`
   — `context.task_id` is the Temporal workflow ID, one identifier end to
   end, unchanged from before this integration.
4. `await handle.result()` — waits for the whole workflow (this can be
   minutes; `execute()` running that long is fine, it's just one of many
   concurrent asyncio tasks in the API process, and this is exactly what
   lets a *blocking* caller's response stay open until completion).
5. Maps the result dict onto `Task` artifacts + a final status update (§4).

Deliberately out of scope for this pass: no fine-grained per-capability
progress as separate status updates (Temporal's own workflow history
already has that detail — this reports `SUBMITTED → WORKING → one terminal
state`, not a blow-by-blow), and no `TASK_STATE_INPUT_REQUIRED` (a genuine
paused "waiting on your reply" flow) — `respond` always gives a definitive
answer instead (§4).

### 6.3 Real bugs found by actually calling the endpoints, not by reading the SDK

Three, in order of discovery, none documented anywhere in the SDK's own
docs/source comments:

1. **Missing `A2A-Version` header silently downgrades the request to spec
   `0.3`**, which this agent's `1.0` handler then rejects. Traced to
   `a2a.utils.version_validator`, which reads the header out of
   `ServerCallContext.state['headers']` — populated by
   `DefaultServerCallContextBuilder`, but **not** by a naive custom
   `ServerCallContextBuilder` (our own `ApiKeyCallContextBuilder`, before
   the fix). Fixed by delegating to `DefaultServerCallContextBuilder.build()`
   for everything except the actual key check, so this can't silently
   regress if the SDK's context ever grows another such field.
2. **A bare `TaskStatusUpdateEvent` (e.g. from `TaskUpdater.submit()`) is
   not accepted as the first event** — the framework requires the actual
   `Task` object enqueued first, erroring with "Agent should enqueue Task
   before TaskStatusUpdateEvent event" otherwise. Fixed by manually
   constructing and enqueueing a `Task(id=, context_id=, status=TaskStatus(
   state=TASK_STATE_SUBMITTED))` before calling `updater.start_work()`.
3. **`plan_task`'s Temporal retry policy had no attempt cap** (a
   pre-existing gap, surfaced by this work, not caused by it) — a bad LLM
   key made a *blocking* `message/send` call hang far past any reasonable
   timeout instead of failing fast, since Temporal's default retry policy
   has no maximum-attempts ceiling. Fixed with an explicit
   `RetryPolicy(maximum_attempts=3, ...)`.

### 6.4 Config `DataPart` and the retired chat/task split

The prompt is a plain `TextPart`; this agent's own `RequestConfig` (repo,
llm, options — §2) rides alongside it as a `DataPart` on the same
`Message`, extracted via `google.protobuf.json_format.MessageToDict` on
whichever `Part` has its `data` field set (`Part.HasField('data')` — `data`
is message-typed, `google.protobuf.Value`, so presence-tracking works;
plain scalar fields like `text` don't support `HasField` in proto3 unless
declared `optional`).

The old `/v1/chat` vs. `/v1/tasks` split is gone — both were always the same
Temporal workflow underneath, just invoked differently at the API layer;
now that "differently" is a standard, caller-controlled
`configuration.return_immediately` flag on the same operation, so there's
no API-layer branch left to keep in sync (§1). `workflow.py` no longer
takes a `mode` field at all.

### 6.5 Verified live

Every pattern in the table in §1, through the real running server, not
mocked: a blocking `message/send` returning a real LLM answer in ~2s; a
non-blocking `message/send` (`return_immediately: true`) returning in
0.03s with `TASK_STATE_SUBMITTED`, polled via `tasks/get` through
`WORKING` to a real `TASK_STATE_COMPLETED` with `root_cause`/
`pull_request` artifacts; a registered `pushNotificationConfig` receiving
**progressive** webhook deliveries from the SDK itself (submitted →
working → per-artifact updates → completed — more granular than this
agent's old single final callback, and this agent's own code never called
an HTTP client to send any of them); both the REST (`POST /message:send`)
and JSON-RPC (`POST /` with method `SendMessage`/`GetTask`) transports:
same behavior, same underlying executor. Two of these real runs opened
real PRs against `neyaadeez/go-script` (independently confirmed via the
GitHub API), one of them via the non-blocking + webhook pattern
specifically.

## 7. Deployment image

One `Dockerfile`, two entrypoints (`uvicorn ari_agent.main:app` for the
API, `python -m ari_agent.orchestration.worker` for the worker) — same
image, same dependencies, since capabilities/tools/planner are shared code.

Built on `python:3.12-slim`, which ships with **neither `git` nor
Node.js/npm** (confirmed directly: `which git`/`which node` against the
bare base image are both empty) — both are hard requirements, not
optional, so the image also installs:

- **`git`** — `clone_repo`, `git_diff`, `git_changed_files` all shell out
  to it directly.
- **Node.js 22 + `opencode-ai` pinned to `1.18.31`** — the exact version
  everything in `docs/MEMORY.md` was actually verified against (the
  `mode: "primary"` requirement, the stdout-only error-JSON shape, the
  `--format json` event schema). Floating to `latest` would silently
  invalidate all of that live testing; bump the pin deliberately, and
  re-verify §5.1's findings against the new version when you do.
- **`golang-go` + `openjdk-21-jdk-headless` + `maven`** (Debian gives Go
  1.24 / Java 21.0.12 / Maven 3.9.9 on this base image) — needed for
  `verify` to build/test *target* repos written in Go or Java, not for ARI
  itself. Java 21 specifically because a real target repo,
  `insightfinder/InfrastructureCore`, requires it via
  `maven-compiler-plugin`'s `<source>`/`<target>`. This is a deliberate,
  bounded bet: Go + Java + Node/TS + Python are the languages this agent
  has actually been exercised against. It is **not** full multi-language
  coverage — a target repo needing Rust, .NET, etc. will clone/analyze/fix
  fine (opencode doesn't need the target's own toolchain for that) but
  `verify` will fail with "command not found," correctly surfaced as a
  real failure (`fail_needs_human` after exhausting retries), just not
  automatically fixable without extending this list. Options considered
  when this came up: bundle more toolchains here as needed (current
  choice — simplest, but image keeps growing and a Python target repo
  still shares ARI's own Python environment rather than being isolated),
  document the limitation and stop there, or build a per-request build
  sandbox (the "correct" answer for a truly multi-language agent, but a
  real feature — Docker-outside-of-Docker or an external build service,
  with the security tradeoffs that implies for a worker already holding
  live credentials). Revisit if the language spread outgrows "just add it
  to the image."

**`repo.url` can be a local filesystem path**, not just a network remote —
git supports this natively, and `clone_repo` needed no code change for
it. What did need a fix: cloning a directory owned by a different user
than the container process (e.g. a bind-mounted host path) hits git's
"detected dubious ownership" refusal. Confirmed live that `-c
safe.directory=*` on the clone command itself does **not** suppress this
for a local clone's source — only real git config does — so the Dockerfile
sets `git config --system --add safe.directory '*'` once at build time
(system-level, not `--global`, so it applies regardless of which uid the
container runs as at deploy time).

Verified by actually building and running the image (not just reading the
Dockerfile): `git`/`node`/`opencode`/`go`/`java`/`mvn` all present and
independently functional inside a running container, the app boots and
serves `/healthz` + the agent card over real HTTP, the real `go-script`
test repo was cloned *inside the container* and `go build ./...` /
`gofmt -l .` / `go vet ./...` ran clean against it, and a real private
repo provided via a bind-mounted local path (not a URL) cloned
successfully through the actual `git_ops.clone()` function, confirming
both the Java toolchain and local-path support work end to end — see
`docs/MEMORY.md` for the `InfrastructureCore` build-environment finding
this also surfaced (a real, pre-existing gap in that repo's own
reproducibility, not this agent's).

## 8. Repo caching & dynamic branches

Two related, real requirements: many requests target the *same* repo over
time (repeat incidents against the same service), so re-cloning from the
remote every time wastes time/bandwidth/disk; and a single prompt can name
*different* branches for "where to work from" and "what to open the PR
against" ("check the repo in the dev branch... raise a PR against
staging"), so neither can be a single static field.

**Caching** (`tools/repo_cache.py`): one `git clone --mirror` per unique
repo URL, kept in `REPO_CACHE_ROOT` (persists across requests — deliberately
not `SANDBOX_ROOT`, which `cleanup_workspace` wipes per request). Before
every use, `ensure_cache()` runs a `git fetch` against it — "latest every
time," not just on first use, so a branch created five minutes ago is
already visible. `clone_repo` then does a fast **local** clone from that
mirror into the request's own sandbox (`git_ops.clone()` needed no change
for this — it already worked against filesystem paths; see §6) — this is
what gives isolation: a `--mirror` cache has no working tree to edit in
the first place, and every request's own clone is a fully independent
copy, so concurrent requests against the same repo can never see or
disturb each other's in-progress patch. Locking is two-layered (an
in-process `asyncio.Lock` plus a real `fcntl.flock`) so concurrent
requests against the *same* repo serialize on updating that one mirror
without blocking requests against other repos. A credential is only ever
used transiently for that one fetch/clone call — never persisted into the
mirror's own git config (scrubbed back to the plain URL immediately after
the initial clone) — so a later request's *read* of the cache (the local
clone into its sandbox) needs no credential, and `clone_repo` re-points
that clone's `origin` back at the real authenticated upstream afterward
(§ below), since `git remote -v` would otherwise show the cache path, not
the real repo, which could confuse a tool — or opencode itself — inspecting it.

Documented limits, not silently assumed: this cache lives on whichever
worker host handles the request — multiple worker replicas each keep an
independent cache unless `REPO_CACHE_ROOT` is a shared volume mounted at
the same path on every replica; there's no eviction/GC (fine for a bounded
set of well-known repos, revisit if that stops being true).

**Dynamic branches**: `clone_repo` accepts an optional `ref` step param
(which branch to check out — falls back to `repo.ref`/`"main"`), and
`publish_pr` accepts an optional `base_ref` step param (which branch the
PR targets — falls back to the same default, independently of whatever
`ref` was). The planner's system prompt explicitly instructs it to read
the prompt for named branches and set these params — verified live against
a real LLM: "check the repo in the dev branch... raise a PR against
staging" correctly produced `clone_repo.params.ref: "dev"` and
`publish_pr.params.base_ref: "staging"` in one plan, with `verify`
correctly omitted since testing wasn't mentioned.

## 9. Open questions carried over from v1

- Concurrency/volume expectations — informs worker replica count and
  Temporal task-queue sizing.
- Git providers beyond GitHub/GitLab at launch.
- **gRPC transport** — explicitly deferred (§6), not abandoned: needs its
  own server process/port and `grpcio`/`grpcio-tools` dependencies. Revisit
  if a real caller needs it.
- **`TASK_STATE_INPUT_REQUIRED`** — not implemented. A genuine multi-turn
  "the agent is paused, waiting for your reply" flow (as opposed to a
  caller just sending a fresh message in the same context) would need this;
  `respond` deliberately never produces a state that implies it (§4).
- Webhook delivery retry semantics are now the SDK's `BasePushNotificationSender`'s
  concern, not this agent's own code (§6) — haven't yet inspected what its
  retry/backoff behavior actually is on a non-2xx from the callback URL.
- **Secrets in Temporal history (found 2026-09-15, real risk, not yet
  fixed):** a full local-Temporal end-to-end run (see `docs/MEMORY.md`)
  confirmed Temporal persists activity/workflow *inputs* in its event
  history as plain base64 — decodable via the Temporal UI's own REST API.
  Since `RequestConfig` (LLM key, repo PAT, callback key) is passed
  directly as workflow input, every secret in a request is currently
  readable by anyone with Temporal UI/API access to the namespace. Needs a
  Temporal **Payload Codec** (encrypting data converter) before any
  non-local deployment — this is a real gap, not a hypothetical one.
