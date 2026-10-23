# Integrating with `TASK_STATE_INPUT_REQUIRED`

Audience: whatever system calls this agent over A2A (a chat UI, an
orchestrator, ARI/MCP Service, etc.) and needs to actually implement the
human-in-the-loop pause described in `docs/design.md` §5b. That doc
explains *why* this exists and how it's built; this doc is the
integration contract — what you will see on the wire, and exactly what
to send back.

**Scope today: every on-call run that is about to open a pull request,
every change the Kubernetes agent proposes, and every build the Jenkins
agent is asked to start or stop.** On-call pauses at `propose_pr`, after
the change is made and its branch is pushed, and `publish_pr` opens the
pull request only once you confirm. The `k8s` kind pauses at
`propose_change`, and `apply_approved_change` changes the cluster only
once you confirm. The `jenkins` kind pauses at `propose_action`, and
`execute_action` triggers or stops the build only once you confirm.
Everything else (an on-call request that asked for no PR, an on-call
`run_skill` run, a k8s health check or refusal, a Jenkins status
question or failure diagnosis, a plain question) completes without ever
producing
`TASK_STATE_INPUT_REQUIRED` — see the
[comparison table](#what-pauses-and-what-does-not) at the end.

The wire contract below is identical for all three kinds: same task
state, same `pending_action` artifact, same structured confirmation. Only
the contents of the artifact differ, and you do not need to understand them
— you echo the `id` back. Read `docs/DEVELOPER_GUIDE.md` first if
you haven't already — this doc assumes you know `message/send`, the
three response patterns, and how multi-turn `context_id`/`task_id`
continuation works. Each kind's own summary of the same round trip is
`docs/DEVELOPER_GUIDE.md` §9 (Jenkins) and §10 (Kubernetes). A working
caller of this contract is the chat gateway's client
(`src/chat_gateway/a2a_client.py`, `confirm()` and `cancel()`), which the
`ari` CLI and the Slack/Teams front ends use.

## 1. The sequence, end to end

```
1. You send a message/send asking for a change
   ("fix the NullPointerException in OrderService and open a PR")
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
   (COMPLETED or FAILED), carrying the result artifact:       TASK_STATE_CANCELED
   "pull_request" (on-call) or "action_result" (k8s, jenkins)
```

Nothing about steps 1–2 or 4–5 depends on which of the three response
patterns from `docs/DEVELOPER_GUIDE.md` §3 you're using (blocking,
poll, or webhook) — `TASK_STATE_INPUT_REQUIRED` shows up identically in
whichever one you observe status through. See §3 below for the
per-pattern specifics.

## 2. What the agent actually sends you (step 2)

An on-call proposal (illustrative — the field names and shapes are taken
from `propose_pr.py`, the values are made up; this is not a captured
response):

```json
{
  "task": {
    "id": "9f2e1c3a-...",
    "contextId": "4c8d...",
    "status": {
      "state": "TASK_STATE_INPUT_REQUIRED",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{ "text": "Found the cause: OrderService.total() dereferences a null discount. I fixed it and pushed branch ari/ari-fix-null-discount-in-order-total-9f2e1c3a; the pull request against main is waiting for your approval. Compare: https://github.com/org/shop/compare/main...ari/ari-fix-null-discount-in-order-total-9f2e1c3a" }]
      }
    },
    "artifacts": [
      {
        "name": "pending_action",
        "parts": [{ "data": {
          "id": "b2c1e4a0-...",
          "action": "open_pull_request",
          "action_name": "Open pull request",
          "repo": "org/shop",
          "repo_url": "https://github.com/org/shop",
          "branch": "ari/ari-fix-null-discount-in-order-total-9f2e1c3a",
          "base_ref": "main",
          "title": "ARI: Fix null discount in order total",
          "body": "...",
          "changed_files": ["src/main/java/shop/OrderService.java"],
          "root_cause": { "summary": "...", "evidence": [] },
          "key_hunks": "diff --git a/src/main/java/shop/OrderService.java b/src/main/java/shop/OrderService.java\n@@ ...",
          "verification": { "status": "not run", "detail": "" },
          "compare_url": "https://github.com/org/shop/compare/main...ari/ari-fix-null-discount-in-order-total-9f2e1c3a",
          "branch_pushed": true,
          "created_at": "2026-10-19T10:00:00+00:00"
        }}]
      },
      {
        "name": "root_cause",
        "parts": [{ "data": { "summary": "...", "evidence": [] } }]
      }
    ]
  }
}
```

`verification.status` is one of `passed`, `skipped`, `not run`,
`failed` or `inconclusive`; a proposal can carry an unverified change,
and this field says so rather than leaving it to be inferred.

A k8s proposal has the same envelope and a different `pending_action`
body: `id`, `action` (`scale_replicas` | `set_autoscaler_bounds` |
`set_resources`) plus that action's own fields (`namespace`,
`deployment`, and `replicas`, `autoscaler`/`min`/`max`, or
`container`/`resources`), `action_name`, `before`/`after`, `risk_tier`,
`risks`, the server-side `dry_run` result, `snapshot` and `created_at`,
among others — `docs/DEVELOPER_GUIDE.md` §10 lists them. Numbers in it
come back as doubles (`3.0`), because artifacts travel as protobuf
`Value`s.

**What to actually check, in order of reliability:**

1. `task.status.state == "TASK_STATE_INPUT_REQUIRED"` — this is the
   authoritative signal. Check this, not the message text.
2. Find the artifact named `"pending_action"` in `task.artifacts` and
   read its `data` — its `id` is what you need for step 4, verbatim. A
   task that paused more than once (see §7) carries one `pending_action`
   artifact per proposal; the chat gateway takes the last one
   (`src/chat_gateway/tasks.py`, `artifacts()`).
3. `task.status.message`'s text is for a human to read (show it in your
   UI/chat), not for your code to parse. Don't try to detect "is this
   asking for confirmation" from the message string — the state field
   already tells you that unambiguously.

**Do not construct or guess the `pending_action.id` yourself.** It's a
server-generated UUID; the only valid one is whatever this exact
`pending_action` artifact says. A human approving this in your UI should
be approving *this specific artifact's data* (repository, branch, title,
changed files, key hunks — or, for k8s, the object and its before/after)
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
  (until you confirm, decline, or the task store loses it — see §7) —
  polling further will just keep returning the same state.
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
  the rest of the config alongside it (same as any other request to this
  agent) — the confirm field is additive, not a replacement for the rest
  of the config shape. For on-call that means `repo` (with a `pat`:
  `publish_pr` opens the pull request through the GitHub REST API with
  it) and `llm`; for k8s, `llm`; for jenkins, the `jenkins` block (with
  its credentials: `execute_action` calls the server with them) and
  `llm`.

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
                {"text": "approved"},  # for the human-readable log; not parsed
                {"data": {
                    "confirm_pending_action": {"id": pending_action_id},
                    "repo": repo_config,
                    "llm": llm_config,
                    "options": {},
                }},
            ],
        }
    },
    timeout=600,
)
```

This is the same message `AgentClient.confirm()` in
`src/chat_gateway/a2a_client.py` builds: the conversation's own config
with `confirm_pending_action` added, sent on the paused task's
`task_id` and `context_id`.

A plain text part is still worth sending (shown above) purely for the
conversation's own readability/audit trail — the confirm logic itself
reads only the structured `confirm_pending_action` field and completely
ignores the prompt text for this turn (`plan_task`'s bypass branch never
looks at it — see `docs/design.md` §5b).

## 5. What happens after you confirm

- **Resolves correctly** → the kind's confirm capability runs as the
  only step of a new run (`publish_pr` for on-call,
  `apply_approved_change` for k8s, `execute_action` for jenkins), and the
  Task goes back to `TASK_STATE_COMPLETED`/`TASK_STATE_FAILED`, whichever
  that step actually resulted in. On-call adds a `pull_request` artifact
  (`{"url": ..., "branch": ...}`); k8s and jenkins add an `action_result`
  artifact.
  The proposal's own artifacts stay on the task.
- **The id doesn't resolve** (typo, wrong task_id, already handled) →
  the Task goes straight to `TASK_STATE_FAILED` with a message
  explaining why, rather than silently falling through to normal
  planning. That failure is terminal, so the real proposal on the same
  task can no longer be confirmed either. Don't retry the same confirm
  blindly — ask the agent to propose the change fresh instead.
- **The proposal no longer holds** → the confirm capability refuses and
  the Task ends `TASK_STATE_FAILED`, with the same "ask again" recovery.
  `publish_pr` refuses a proposal that is not an `open_pull_request`
  proposal or lacks `branch`, `base_ref`, `title` or `repo`.
  `apply_approved_change` re-reads the target and refuses if anything
  the change depends on moved since the proposal's `snapshot` (unless
  the target already holds the approved values). Neither of those two
  checks the proposal's age; the approval window is the caller's to
  enforce. `execute_action` is the exception — it refuses a proposal more
  than 30 minutes old, so a Jenkins confirmation that arrives after a
  longer approval window has to be proposed again.
- **Sending a plain "yes" instead of the structured field does
  nothing** — there is no code path that turns free text into a
  confirmation. This is deliberate (`docs/design.md` §5b's layer 3):
  the planner is told never to schedule `publish_pr`,
  `apply_approved_change` or `execute_action`, the k8s kind drops them
  from any plan, and all three refuse to run unless the step came from
  the confirmation bypass.
  A free-text reply on the paused task just runs an ordinary new turn
  (§7).

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
  runs an ordinary new planning turn on that task. If that turn ends
  without a new proposal, the task is in a terminal state and the
  earlier proposal can no longer be confirmed; if it proposes again, the
  task pauses with a new `pending_action`. The chat gateway tells the
  user so (`STALE_PROPOSAL_NOTE` in `src/chat_gateway/bridge.py`).
- **Sending the same `confirm_pending_action` twice**, after the first
  one already resolved the task to a terminal state, is refused at the
  SDK level (a terminal-state task refuses a further `message/send` for
  that `task_id`). Two confirms sent at once were tried live against the
  k8s kind (`docs/MEMORY.md`, 2026-10-16): the change was written once
  and the task ended `COMPLETED`. On-call's `publish_pr` looks up an
  existing open pull request for the branch before creating one. Still,
  send one confirm and wait for its result before considering a retry.
- **Whether a paused task survives an API restart depends on the
  deployment.** With `TASK_STORE_ROOT` unset the API keeps tasks in
  `InMemoryTaskStore`, and a restart loses the paused task: a confirm
  afterwards is refused with HTTP 404 for the task. With
  `TASK_STORE_ROOT` set (the Helm chart mounts a volume for it), tasks
  are also written to disk and survive a restart; they are pruned after
  `TASK_STORE_MAX_AGE_SECONDS` (default 7 days)
  (`src/common/a2a_server/task_store.py`).
- **Once declined (cancelled), it's final** — `TASK_STATE_CANCELED` is a
  terminal state, so a later confirm attempt against that same `task_id`
  will be refused, not silently re-open the proposal. Propose it again
  fresh if you change your mind, same as any other stale/resolved id.

## What pauses, and what does not

| | `propose_pr` (on-call), `propose_change` (k8s) and `propose_action` (jenkins) | Anything with nothing to approve |
|---|---|---|
| Pause mechanism | Hard: real `TASK_STATE_INPUT_REQUIRED` | None: the task completes normally |
| How you confirm | Structured `confirm_pending_action: {"id": ...}` field, same `task_id` | Nothing to confirm |
| Who decides it's a confirmation | Deterministic server-side artifact lookup, no LLM | — |
| Free-text "yes" alone | Does nothing to the proposal; runs a new turn (§7) | An ordinary follow-up message |

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

The other two kinds have no such side effect: `propose_change` changes
nothing in the cluster (it runs only a server-side dry run) and
`propose_action` calls nothing mutating on Jenkins at all, so declining
leaves nothing behind.
