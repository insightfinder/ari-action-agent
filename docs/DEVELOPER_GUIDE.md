# Developer Guide — calling the ARI agents over A2A

There are two agent kinds on this codebase today, each its own A2A server
deployment with its own base URL and Agent Card: the **on-call agent**
(§1–§8 below — clones a repo, fixes it, opens a PR) and the **Jenkins
agent** (§9 — reads/diagnoses a Jenkins job or build, proposes and, once
confirmed, executes a change). Both are genuine
[A2A (Agent2Agent) protocol](https://a2a-protocol.org) servers, built on
the official [`a2a-sdk`](https://pypi.org/project/a2a-sdk/) (a Linux
Foundation project), serving both the **JSON-RPC** and **REST/HTTP+JSON**
transports the spec defines (gRPC is a deliberate, separate follow-up —
see `docs/design.md` §2), and everything about §1–§8 (discovery, auth,
`message/send`, the three response patterns, cancellation, errors)
applies identically to the Jenkins agent — only the Agent Card content
and the config `DataPart` shape differ, which §9 covers.

Everything below for the on-call agent is verified against a real running
instance, not just read off the spec — exact request/response bodies are
copied from real captured traffic, not hand-written. The Jenkins agent's
own MCP integration has been verified at the protocol level against a
real production Jenkins (see `docs/MEMORY.md`, 2026-09-18) but **not yet
through an actual end-to-end A2A call** — §9's examples are illustrative
of the intended shape, not captured live traffic.

If you already have an A2A client library, you can likely skip straight to
[§5 Config `DataPart` shape](#5-this-agents-config-datapart-shape) — that's
the one thing specific to *this* agent; everything else is standard A2A.

## 1. Discovering the agent

```
GET /.well-known/agent-card.json
```

No auth required (per A2A convention — this is how a caller learns *how* to
authenticate everything else). Real response:

```json
{
  "name": "ARI On-Call Agent",
  "description": "On-call agent that takes an incident/prompt plus repo + LLM config, analyzes the problem in an isolated sandbox using opencode, generates and verifies a fix, and opens a pull request. Every request is independent and self-contained.",
  "supportedInterfaces": [
    { "url": "https://ari-oncall-agent.internal.example.com", "protocolBinding": "JSONRPC", "protocolVersion": "1.0" },
    { "url": "https://ari-oncall-agent.internal.example.com", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0" }
  ],
  "capabilities": { "streaming": false, "pushNotifications": true },
  "securitySchemes": {
    "apiKeyAuth": { "apiKeySecurityScheme": { "location": "header", "name": "x-api-key" } }
  },
  "securityRequirements": [{ "schemes": { "apiKeyAuth": {} } }],
  "skills": [{ "id": "code_analysis_fix", "name": "Code Analysis & Fix", "...": "..." }]
}
```

Both transports are served at the **same base URL** — the JSON-RPC endpoint
is `POST <url>/`, the REST endpoints are `<url>/message:send`,
`<url>/tasks/{id}`, etc. (see below).

## 2. Authentication

Every call except the Agent Card and `/healthz` needs two headers:

| Header | Value |
|---|---|
| `x-api-key` | one of this deployment's `AGENT_API_KEYS` |
| `A2A-Version` | `1.0` |

**The `A2A-Version` header is not optional in practice.** If it's missing,
the SDK's own version-negotiation logic assumes you meant an older draft
(`0.3`) and rejects the request with a version-mismatch error — this tripped
us up while building this integration (see `docs/MEMORY.md`), so it's called
out here explicitly rather than left to be discovered the hard way.

Missing/invalid `x-api-key` → `401`. Missing/wrong `A2A-Version` → a `400`
with a structured A2A error body (shown in §7).

## 3. Sending a message — the one operation that does everything

There's a single operation, `message/send` (`SendMessage` in JSON-RPC/gRPC
terms), for everything this agent does — asking it a question, asking it to
fix a bug, asking it to add a feature. What differs is `configuration.
return_immediately` and, optionally, a push-notification config — not a
different endpoint.

### REST

```
POST /message:send
```

```json
{
  "message": {
    "message_id": "0e6d9c9e-2f1c-4b8b-9b8a-2f5a2d6b6b1a",
    "role": "ROLE_USER",
    "parts": [
      { "text": "What is a race condition, in one sentence?" },
      { "data": { "repo": {"url": "https://github.com/octocat/Hello-World"}, "llm": {"provider": "openai", "api_key": "sk-...", "model": "gpt-4o-mini"} } }
    ]
  }
}
```

`message_id` is required (any unique string — a UUID is the obvious
choice). The prompt is a plain `TextPart`; this agent's own config (repo,
LLM, options) rides alongside it as a `DataPart` — see §5.

### JSON-RPC

Same payload, wrapped:

```
POST /
```
```json
{ "jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": { "message": { "...": "as above" } } }
```

### Pattern A — blocking (what a "chat" call used to be)

Omit `configuration` (or leave `return_immediately` unset/`false`). The HTTP
response doesn't come back until the `Task` reaches a terminal state. Real
captured response for the example above:

```json
{
  "task": {
    "id": "d2902fbe-8e8c-4973-aaca-715d582843dd",
    "contextId": "4c8dcaa9-fdf2-474f-812c-4f22ea9172cd",
    "status": {
      "state": "TASK_STATE_COMPLETED",
      "message": {
        "messageId": "25dd27c0-2de6-485b-866d-8c32982ae352",
        "role": "ROLE_AGENT",
        "parts": [
          { "text": "A race condition is a situation in programming where the outcome of a process depends on the sequence or timing of uncontrollable events..." }
        ]
      },
      "timestamp": "2026-09-15T22:45:09.215415Z"
    }
  }
}
```

That took about 2–3 seconds for a plain question with no repo work. A full
analyze→fix→verify→publish run takes several minutes — for those, use
pattern B or C instead of blocking.

### Pattern B — non-blocking + polling (what a "task" call used to be)

Add `"configuration": {"return_immediately": true}`. The response comes back
in well under a second, with the task in `TASK_STATE_SUBMITTED`:

```json
{ "task": { "id": "4bffcf4a-38c9-4493-b2ad-54e2a9b1b94f", "contextId": "73b0eff4-...", "status": { "state": "TASK_STATE_SUBMITTED" } } }
```

Poll it:

```
GET /tasks/{id}
```

```json
{ "id": "4bffcf4a-...", "contextId": "73b0eff4-...", "status": { "state": "TASK_STATE_WORKING" } }
```

...and again once it's done — same shape as the blocking response above,
just fetched separately. `TASK_STATE_FAILED` and a message explaining why
show up here too if the pipeline gave up.

### Pattern C — non-blocking + push notification webhook

Same as B, but also register where to deliver updates — either inline on
the same request:

```json
{
  "message": { "...": "as above" },
  "configuration": {
    "return_immediately": true,
    "task_push_notification_config": { "url": "https://your-service.example.com/webhook" }
  }
}
```

...or separately, any time, via `POST /tasks/{id}/pushNotificationConfigs`
(useful if you only decide you want a webhook after the task's already
running). Either way, **the SDK delivers these itself** — this agent's own
code never POSTs a webhook. It's also more granular than you might expect:
real captured deliveries for one request were three separate POSTs, as the
task moved through its lifecycle:

```jsonc
// POST 1, immediately
{ "task": { "id": "02105b8f-...", "contextId": "3aa84f49-...", "status": { "state": "TASK_STATE_SUBMITTED" } } }

// POST 2, a moment later
{ "statusUpdate": { "taskId": "02105b8f-...", "contextId": "3aa84f49-...", "status": { "state": "TASK_STATE_WORKING", "timestamp": "..." } } }

// POST 3, when done
{ "statusUpdate": { "taskId": "02105b8f-...", "contextId": "3aa84f49-...", "status": { "state": "TASK_STATE_COMPLETED", "message": { "role": "ROLE_AGENT", "parts": [{ "text": "..." }] }, "timestamp": "..." } } }
```

## 4. What you get back for a real fix

For a request that actually touches a repo, the completed `Task` carries
`artifacts` in addition to the final message — the root cause (if analysis
ran) and the PR link (if one was opened), as structured `DataPart`s, not
just embedded in the text:

```json
{
  "status": { "state": "TASK_STATE_COMPLETED", "message": { "parts": [{ "text": "Fixed the WaitGroup race and opened PR #1..." }] } },
  "artifacts": [
    { "name": "root_cause", "parts": [{ "data": { "summary": "...", "evidence": [] } }] },
    { "name": "pull_request", "parts": [{ "data": { "url": "https://github.com/neyaadeez/go-script/pull/2", "branch": "ari/fix-hosts-scan-portability" } }] }
  ]
}
```

## 5. This agent's config `DataPart` shape

Everything this agent needs beyond the prompt — LLM credentials, repo
location/credentials, dynamic options — travels as a `DataPart` alongside
the prompt's `TextPart`. This is *not* part of the A2A spec itself; it's this
agent's own contract for what it expects inside one `DataPart`.

```jsonc
{
  "repo": {
    "url": "https://github.com/org/repo",
    "ref": "main",             // optional, default "main" — branch to check out
    "pat": "ghp_...",           // optional — HTTPS PAT for a private repo / opening a PR
    "ssh_key": "-----BEGIN..."  // optional, alternative to pat
  },
  "llm": {
    "provider": "openai",       // or "anthropic"
    "api_key": "sk-...",
    "model": "gpt-5-mini",
    "base_url": null            // optional override
  },
  "options": {
    "skip_tests": true          // free-form — the planner reads whatever you put here
  },
  "system_prompt": null         // optional override of the planner/executor's default system prompt
}
```

**Prompts are read dynamically, including branch names.** "Check the repo
in the `dev` branch, fix the memory leak, and raise a PR against `staging`"
correctly produces a plan that checks out `dev` and opens the PR against
`staging` — you don't need separate fields for this; just say it in the
prompt. See `docs/design.md` §7 for how that's implemented.

**Multi-turn conversations**: send follow-up messages with the same
`context_id` (and, to continue the same task rather than start a new one,
the same `task_id`) — this agent has no database, but the SDK's in-memory
task store gives you history within that task's lifetime. Don't rely on
this surviving a restart of the agent process.

## 6. Cancelling a task

```
POST /tasks/{id}:cancel
```

Cancels the underlying Temporal workflow and marks the task
`TASK_STATE_CANCELED`.

## 7. Errors

Structured, per the A2A error schema, e.g. a missing `A2A-Version` header:

```json
{
  "error": {
    "code": 400,
    "status": "FAILED_PRECONDITION",
    "message": "A2A version '0.3' is not supported by this handler. Expected version '1.0'.",
    "details": [{ "@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "VERSION_NOT_SUPPORTED", "domain": "a2a-protocol.org" }]
  }
}
```

## 8. Sample client

A complete, runnable example is at
[`docs/examples/sample_client.py`](examples/sample_client.py) — fetches the
agent card, and demonstrates all three patterns from §3 using plain `httpx`
(no A2A-specific client library required; the official `a2a-sdk` also ships
a `a2a.client` package if you'd rather use that — see the comment at the top
of the sample for the equivalent).

```bash
pip install httpx
OPENAI_API_KEY=sk-... python docs/examples/sample_client.py chat
OPENAI_API_KEY=sk-... GITHUB_PAT=ghp_... python docs/examples/sample_client.py task-poll
OPENAI_API_KEY=sk-... GITHUB_PAT=ghp_... python docs/examples/sample_client.py task-webhook
```

The Jenkins agent has its own sibling example,
[`docs/examples/jenkins_sample_client.py`](examples/jenkins_sample_client.py)
— see §9.

## 9. The Jenkins agent

A separate deployment, own base URL, own Agent Card (`skills[0].id ==
"jenkins_read_diagnose_act"`) — but the same `message/send` operation and
the same three response patterns as §3. Only the config `DataPart` shape
differs.

### Config `DataPart` shape

```jsonc
{
  "jenkins": {
    "base_url": "https://jenkins.example.com",
    "auth": {
      "type": "api_token",        // or "bearer", "basic"
      "user": "svc-ari-jenkins",  // required for api_token/basic
      "token": "..."
    },
    "verify_tls": true,
    "ca_bundle": null,             // optional — PEM content (not a path), for a private/internal CA
    "allow_mutations": true        // design intent only today — not yet enforced in code, see docs/design.md §8
  },
  "llm": {
    "provider": "anthropic",
    "api_key": "sk-ant-...",
    "model": "claude-sonnet-5"
  }
}
```

Jenkins connection details are per-request, exactly like the on-call
agent's `repo` block — never deployment-level config. This is deliberate:
this agent stores no credentials between requests.

### Reading vs. diagnosing vs. changing something

A plain status/history question ("how's the checkout-service deploy
job doing?") runs `resolve_target` → `inspect` and answers directly — no
confirmation needed, nothing mutates. A failure question ("why has the
nightly build been red since Tuesday?") runs `resolve_target` →
`diagnose` instead, and the final message includes the diagnosed cause
with evidence (failing step, first real error line), not just a
pass/fail restatement.

A requested **change** ("deploy checkout-service to staging", "stop that
build") never executes on the first turn. It runs `resolve_target` →
`propose_action`, which resolves the job and every parameter but calls
nothing mutating (`propose_action`'s opencode agent has every mutating
Jenkins tool denied outright — see `docs/design.md` §5b), and the Task
carries a `pending_action` artifact:

```json
{
  "artifacts": [
    {
      "name": "pending_action",
      "parts": [{ "data": {
        "id": "b2c1...",
        "action": "trigger_build",
        "job": "deploy/checkout-service",
        "parameters": { "ENV": "staging" },
        "reasoning": "ENV defaults to \"production\"; user asked for staging explicitly",
        "jenkins_url": "https://jenkins.example.com",
        "created_at": "2026-09-18T22:00:00+00:00"
      }}]
    }
  ]
}
```

**How you confirm it differs by action** (see `docs/design.md` §5b for
the full three-layer gate):

- **`trigger_build`** — a hard gate, not a chat reply. The Task's status
  is genuinely `TASK_STATE_INPUT_REQUIRED` (not `COMPLETED`) — this agent
  *does* implement that state for this one action, unlike the on-call
  agent (§3). See
  [`docs/INPUT_REQUIRED_INTEGRATION.md`](INPUT_REQUIRED_INTEGRATION.md)
  for the full integration contract (detecting it across all three
  response patterns, edge cases, a runnable snippet); summary: send a
  follow-up `message/send` on the **same `task_id`** whose config
  `DataPart` carries a structured field instead of free text:
  ```json
  { "confirm_pending_action": { "id": "b2c1..." } }
  ```
  This id must match a `pending_action` artifact already on that exact
  task — the server resolves it deterministically from the task's own
  history, never from an LLM's reading of a "yes." A wrong/stale/unknown
  id fails clearly rather than silently doing nothing. There is no
  free-text way to confirm a `trigger_build` — a plain "yes, go ahead" on
  its own does not execute it.
- **`stop_build`** — unchanged, conversational. Send a follow-up
  `message/send` in the same `context_id` with a plain "yes, go ahead"
  reply; the planner reads history and, recognizing a confirmation of a
  specific prior proposal, schedules `execute_action` with those exact
  fields itself. A bare "yes" with no matching proposal in history does
  nothing, and a confirmation more than 30 minutes after the proposal is
  rejected as stale either way.

Once genuinely confirmed, `execute_action` runs and the Task's status
returns to a normal terminal state, carrying an `action_result` artifact
instead. Stopping a build goes through a direct REST call under the hood,
not opencode — see `docs/design.md` §5b for why (no stop/cancel tool
exists in the real plugin, confirmed live).

### Sample client

[`docs/examples/jenkins_sample_client.py`](examples/jenkins_sample_client.py)
demonstrates the read/diagnose/propose flow (agent-card, a status
question, a failure diagnosis, and a change proposal) — deliberately does
**not** include a mode that confirms and executes an action, consistent
with this codebase's general posture of never wiring an automatic
confirm-and-mutate path in example/test code.

```bash
pip install httpx
JENKINS_BASE_URL=https://jenkins.example.com JENKINS_USER=... JENKINS_TOKEN=... \
  ANTHROPIC_API_KEY=sk-ant-... python docs/examples/jenkins_sample_client.py propose
```
