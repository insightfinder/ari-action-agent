# Developer Guide — calling the ARI On-Call Agent over A2A

This agent is a genuine [A2A (Agent2Agent) protocol](https://a2a-protocol.org)
server, built on the official [`a2a-sdk`](https://pypi.org/project/a2a-sdk/)
(a Linux Foundation project). It serves both the **JSON-RPC** and
**REST/HTTP+JSON** transports the spec defines. (gRPC is a deliberate,
separate follow-up — see `docs/design.md` §6.) Everything below is verified
against a real running instance, not just read off the spec — exact request/
response bodies are copied from real captured traffic, not hand-written.

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
