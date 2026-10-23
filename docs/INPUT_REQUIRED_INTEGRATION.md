# Integrating with `TASK_STATE_INPUT_REQUIRED`

Audience: whatever system calls this agent over A2A (a chat UI, an
orchestrator, ARI/MCP Service, etc.) and needs to actually implement the
human-in-the-loop pause described in `docs/design.md` §5b. That doc
explains *why* this exists and how it's built; this doc is the
integration contract — what you will see on the wire, and exactly what
to send back.

**Scope today: every action the Jenkins agent proposes — `trigger_build`
and `stop_build` alike — and every on-call run that is about to open a
pull request.** On-call pauses at `propose_pr`, after the change is made
and its branch is pushed, and opens the pull request only once you
confirm. Everything else (an on-call request that asked for no PR, a
plain question) completes without ever producing
`TASK_STATE_INPUT_REQUIRED` — see the
[comparison table](#what-pauses-and-what-does-not) at the end if you're
integrating against both agents.

The wire contract below is identical for both: same task state, same
`pending_action` artifact, same structured confirmation. Only the
contents of the artifact differ, and you do not need to understand them
— you echo the `id` back. Read `docs/DEVELOPER_GUIDE.md` first if
you haven't already — this doc assumes you know `message/send`, the
three response patterns, and how multi-turn `context_id`/`task_id`
continuation works.

## 1. The sequence, end to end

```
1. You send a message/send asking for a change
   ("deploy checkout-service to staging")
        │
        ▼
2. Task status becomes TASK_STATE_INPUT_REQUIRED
   (not COMPLETED) — a "pending_action" artifact is attached
        │
        ▼
3. You (or a human behind you) decide: confirm or don't
        │
        ▼
4a. Confirm: send ANOTHER message/send, same task_id,          4b. Decline: POST /tasks/{id}:cancel,
    with a structured confirm_pending_action field                  same task_id — see §6.
        │                                                            │
        ▼                                                            ▼
5. Task status becomes a normal terminal state again          Task status becomes
   (COMPLETED or FAILED), carrying an "action_result" artifact TASK_STATE_CANCELED
```

Nothing about steps 1–2 or 4–5 depends on which of the three response
patterns from `docs/DEVELOPER_GUIDE.md` §3 you're using (blocking,
poll, or webhook) — `TASK_STATE_INPUT_REQUIRED` shows up identically in
whichever one you observe status through. See §3 below for the
per-pattern specifics.

## 2. What the agent actually sends you (step 2)

A real captured shape (illustrative — this exact sequence has been
verified against the SDK's source and by unit tests, not yet against a
live deployed run — see `docs/MEMORY.md`, 2026-09-18):

```json
{
  "task": {
    "id": "9f2e...",
    "contextId": "4c8d...",
    "status": {
      "state": "TASK_STATE_INPUT_REQUIRED",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{ "text": "I'm ready to trigger deploy/checkout-service with ENV=staging (defaults to production; you asked for staging explicitly). Confirm to proceed." }]
      }
    },
    "artifacts": [
      {
        "name": "pending_action",
        "parts": [{ "data": {
          "id": "b2c1e4a0-...",
          "action": "trigger_build",
          "job": "deploy/checkout-service",
          "parameters": { "ENV": "staging" },
          "build_number": null,
          "reasoning": "ENV defaults to \"production\"; user asked for staging explicitly",
          "jenkins_url": "https://jenkins.example.com",
          "created_at": "2026-09-18T22:00:00+00:00"
        }}]
      }
    ]
  }
}
```

**What to actually check, in order of reliability:**

1. `task.status.state == "TASK_STATE_INPUT_REQUIRED"` — this is the
   authoritative signal. Check this, not the message text.
2. Find the artifact named `"pending_action"` in `task.artifacts` and
   read its `data` — this is what you need for step 4, verbatim.
3. `task.status.message`'s text is for a human to read (show it in your
   UI/chat), not for your code to parse. Don't try to detect "is this
   asking for confirmation" from the message string — the state field
   already tells you that unambiguously.

**Do not construct or guess the `pending_action.id` yourself.** It's a
server-generated UUID; the only valid one is whatever this exact
`pending_action` artifact says. A human approving this in your UI should
be approving *this specific artifact's data* (job, parameters, reasoning)
— not just clicking a generic "yes" that your integration then sends as
free text (see §5 for why that does nothing).

## 3. Detecting it per response pattern

- **Blocking** (`message/send` with no `return_immediately`): the HTTP
  response itself carries `status.state == "TASK_STATE_INPUT_REQUIRED"`
  directly — same as a normal blocking completion, just a different
  state value. Nothing else changes about how you make the call.
- **Polling** (`configuration.return_immediately: true` + `GET
  /tasks/{id}`): keep polling while `state` is `TASK_STATE_SUBMITTED` or
  `TASK_STATE_WORKING`, same as always — but **stop polling and treat
  `TASK_STATE_INPUT_REQUIRED` as a real poll-terminal state**, not a
  transient one. The task will sit at `INPUT_REQUIRED` indefinitely
  (until you confirm, or it is lost with the task store — persistent when
  `TASK_STORE_ROOT` is set, in memory and lost on a process restart when
  it is not) — polling further will just keep returning the same state.
- **Webhook** (`task_push_notification_config`): one of the delivered
  `statusUpdate` payloads will carry `status.state ==
  "TASK_STATE_INPUT_REQUIRED"`. Treat this delivery as "stop and get a
  decision," not as an error or a mid-pipeline progress update — it's
  the same shape as the terminal-state delivery you'd otherwise wait for
  (see `docs/DEVELOPER_GUIDE.md` §3 Pattern C's captured example), just
  with this state value instead of `COMPLETED`/`FAILED`.

## 4. Confirming it (step 4a)

Send a normal `message/send` — same operation, same endpoint, no new
API surface — with:

- **The same `task_id`** as the paused task. This is what makes it a
  *resume* of that exact task rather than a new, unrelated request — see
  `docs/DEVELOPER_GUIDE.md` §5's note on multi-turn continuation.
- **The same `context_id`.**
- **A structured field in the config `DataPart` — not free text:**
  ```json
  { "confirm_pending_action": { "id": "b2c1e4a0-..." } }
  ```
  using the exact `id` from the artifact in §2. You still need to supply
  `jenkins`/`llm` config alongside it (same as any other request to this
  agent) — the confirm field is additive, not a replacement for the rest
  of the config shape.

```python
import uuid
import httpx

resp = httpx.post(
    f"{base_url}/message:send",
    headers={"x-api-key": api_key, "A2A-Version": "1.0", "Content-Type": "application/json"},
    json={
        "message": {
            "message_id": str(uuid.uuid4()),
            "context_id": context_id,   # from the paused task
            "task_id": task_id,          # from the paused task
            "role": "ROLE_USER",
            "parts": [
                {"text": "confirming the proposed deploy"},  # for the human-readable log; not parsed
                {"data": {
                    "confirm_pending_action": {"id": pending_action_id},
                    "jenkins": jenkins_config,
                    "llm": llm_config,
                }},
            ],
        }
    },
    timeout=600,
)
```

A plain text part is still worth sending (shown above) purely for the
conversation's own readability/audit trail — the confirm logic itself
reads only the structured `confirm_pending_action` field and completely
ignores the prompt text for this turn (`plan_task`'s bypass branch never
looks at it — see `docs/design.md` §5b).

## 5. What happens after you confirm

- **Resolves correctly** → `execute_action` runs, and the completed Task
  (now back to `TASK_STATE_COMPLETED`/`TASK_STATE_FAILED`, whichever the
  trigger actually resulted in) carries an `action_result` artifact.
- **The id doesn't resolve** (typo, wrong task_id, already handled, or an
  in-memory task store lost it across a process restart) → the Task goes
  straight to `TASK_STATE_FAILED` with a message explaining why, rather
  than silently falling through to normal planning. Don't retry the same
  confirm blindly — if it didn't resolve once, it won't resolve again;
  ask the agent to propose the action fresh instead.
- **More than 30 minutes passed** since the proposal → rejected as stale
  (`execute_action`'s own check, independent of and in addition to the
  confirm mechanism itself) — same "propose it again" recovery.
- **Sending a plain "yes" instead of the structured field does
  nothing** — there is no code path that turns free text into a
  `trigger_build` confirmation. This is deliberate (`docs/design.md`
  §5b's layer 3) — if your integration only sends text, `trigger_build`
  will simply never execute, and the task will just stay at
  `INPUT_REQUIRED`.

## 6. Declining

Use the standard A2A operation: `POST /tasks/{id}:cancel` (already
documented in `docs/DEVELOPER_GUIDE.md` §6) — this *is* the decline
signal, no new field or endpoint needed. Verified live against a real
Temporal dev server, using the actual `AriAgentExecutor.cancel()` code
(not just the raw Temporal client): calling it against a task_id whose
run already completed with `needs_approval` produces a clean
`TASK_STATE_CANCELED` status update, with no exception — even though the
underlying Temporal workflow execution itself finished normally rather
than being "still running and blocked" (an earlier version of this
section assumed cancelling that would fail; it doesn't, and the earlier
claim that there's no decline signal at all was simply wrong — worth
saying plainly rather than quietly editing away).

```
POST /tasks/{task_id}:cancel
```

using the same `task_id` from the paused task. No body needed beyond
what that operation already requires. This marks the A2A **Task**
canceled — it does not (and cannot) retroactively "un-complete" the
underlying Temporal workflow execution, which Temporal's own history will
still show as `Completed` (it finished normally when it produced the
`needs_approval` pause; there was never anything still running to
cancel). These are two independent state machines by design — the A2A
Task's status is what any caller actually observes, and that's the one
`:cancel` updates.

**One real edge case, also verified live:** cancelling a `task_id` that
was **never started at all** (a typo, or the wrong id entirely) behaves
differently — it raises rather than silently succeeding
(`temporalio.service.RPCError: workflow not found for ID: ...`). Since
`AriAgentExecutor.cancel()` has no try/except around this, a bogus
`task_id` will surface as an error rather than a clean cancellation.
This is pre-existing behavior of the general `:cancel` operation (not
specific to the confirm-gate work), and only matters if your integration
might call cancel against an id it never actually got back from a real
`message/send` — the ids you'd use for a real decline always came from
a real paused task, so this shouldn't come up in normal use.

## 7. Edge cases worth knowing about

- **A second, unrelated message on the same paused `task_id`** (not a
  confirm — some other question or request) is accepted normally and
  runs an ordinary new planning turn; it doesn't automatically continue
  or cancel the pending proposal, which just keeps waiting.
- **Sending the same `confirm_pending_action` twice**, after the first
  one already resolved the task to a terminal state, will very likely
  be refused outright at the SDK level (a terminal-state task refuses a
  further `message/send` for that `task_id`) — verified this refusal
  exists for the general terminal-state case, but the exact behavior for
  two *concurrent* confirms racing each other before either completes
  has not been specifically verified. Don't rely on a specific outcome
  here: send one confirm and wait for its result before considering a
  retry.
- **This only works within one running server process.** `InMemoryTaskStore`
  doesn't persist across a restart — a paused task lost to a restart
  can't be resumed; you'd get the "could not find a pending action"
  failure from §5 if you tried.
- **Once declined (cancelled), it's final** — `TASK_STATE_CANCELED` is a
  terminal state, so a later confirm attempt against that same `task_id`
  will be refused, not silently re-open the proposal. Propose it again
  fresh if you change your mind, same as any other stale/resolved id.

## What pauses, and what does not

| | `trigger_build` and `stop_build` (jenkins), `propose_pr` (on-call) | Anything with nothing to approve |
|---|---|---|
| Pause mechanism | Hard: real `TASK_STATE_INPUT_REQUIRED` | None: the task completes |
| How you confirm | Structured `confirm_pending_action: {"id": ...}` field, same `task_id` | Nothing to confirm |
| Who decides it's a confirmation | Deterministic server-side artifact lookup, no LLM | — |
| Free-text "yes" alone | Does nothing | — |

See `docs/design.md` §5b for the design behind the hard gate.

**One difference worth knowing about on-call**, even though it changes
nothing you send back: by the time you are asked, the branch already
exists on the target repository. `propose_pr` pushes it before pausing,
because the approval is human-speed and the resume runs as a separate
workflow execution with no sandbox left — a change that still needed
committing afterwards would be gone. The proposal carries a
`compare_url` for exactly this reason: it is the whole diff, where the
`key_hunks` field is only an excerpt sized for an email. Declining
(§6) leaves that branch behind; nothing deletes it.
