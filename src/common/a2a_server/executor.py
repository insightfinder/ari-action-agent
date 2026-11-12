"""AriAgentExecutor — bridges the A2A protocol's Task lifecycle to our
existing Temporal workflow (`AriAgentWorkflow`), which does all the
actual work (planning, opencode, GitHub MCP, ...) completely unchanged.
Every request becomes exactly one Temporal workflow execution and exactly
one A2A Task: `context.task_id` == the Temporal workflow ID, one
identifier end to end, same convention as before this integration.

This is also what retires the old `/v1/chat` vs. `/v1/tasks` split — both
collapse onto this one `execute()`, with no branching in it at all:
- A caller that blocks on `message/send` (the default) gets this Task's
  final result inline once it reaches a terminal state — this is what the
  old synchronous "chat" endpoint was.
- A caller that sets `configuration.blocking: false` gets a response as
  soon as the first status update below is published (`submit()`), then
  polls `tasks/get` and/or receives a webhook if it registered a
  `pushNotificationConfig` on the request — this is what the old
  `/v1/tasks` + polling/callback was. The SDK's own
  `BasePushNotificationSender` (wired in `main.py`) delivers that webhook
  automatically whenever a status update is published for a task with a
  registered config; this module never POSTs a callback itself.

A run that stops at an approval gate (a step returning
`status: "needs_approval"`, e.g. oncall's `propose_pr`) ends in
`TASK_STATE_INPUT_REQUIRED` with the proposal as a `pending_action`
artifact. The caller resumes it on the same task id with a
`confirm_pending_action` DataPart, or cancels it with `tasks/{id}:cancel`;
see `_find_pending_action` and docs/DEVELOPER_GUIDE.md.

Deliberately out of scope (documented, not an oversight): no fine-grained
per-capability progress (e.g. "now running verify") as separate status
updates — Temporal's own workflow history already has that detail; this
executor reports SUBMITTED → WORKING → one terminal state.
"""

from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Role, Task, TaskState, TaskStatus

from common import tracing
from common.agent_meta import get_agent_meta
from common.orchestration.temporal_client import get_temporal_client
from common.orchestration.workflow import AriAgentWorkflow
from common.settings import get_settings

_TERMINAL_STATE_BY_STATUS = {
    "completed": TaskState.TASK_STATE_COMPLETED,
    "no_action": TaskState.TASK_STATE_COMPLETED,
    "failed": TaskState.TASK_STATE_FAILED,
    # Produced when a step pauses for a person, e.g. propose_pr.
    "needs_approval": TaskState.TASK_STATE_INPUT_REQUIRED,
}


def _extract_config(context: RequestContext) -> dict:
    """The prompt travels as a plain TextPart (`context.get_user_input()`
    already joins those); this agent's repo/LLM/options config travels
    alongside it as a DataPart on the same Message — see
    docs/DEVELOPER_GUIDE.md for the exact shape a caller sends."""
    message = context.message
    if message is None:
        raise ValueError("no message on this request")
    for part in message.parts:
        if part.HasField("data"):
            return json_format.MessageToDict(part.data)
    raise ValueError(
        "no config DataPart found on the message — this agent needs a "
        "DataPart carrying repo/llm config alongside the prompt's TextPart"
    )


def _extract_history(context: RequestContext) -> list[dict]:
    """Reconstructs prior turns from the Task's own accumulated history,
    when this message continues an existing task/context. This agent
    still stores nothing itself (no database) — whatever history exists
    here is only what the SDK's own TaskStore accumulated for this task's
    lifetime (in-memory by default; see main.py), not a durable record."""
    task = context.current_task
    if not task:
        return []
    history = []
    for message in task.history:
        text = "\n".join(part.text for part in message.parts if part.text)
        if text:
            history.append({"role": "agent" if message.role == Role.ROLE_AGENT else "user", "content": text})
    return history


def _dict_to_part(data: dict) -> Part:
    value = Value()
    json_format.ParseDict(data, value)
    return Part(data=value)


def _find_pending_action(task: Task | None, action_id: str) -> dict | None:
    """Deterministic, non-LLM lookup for resuming a paused
    TASK_STATE_INPUT_REQUIRED task: scans this Task's own previously
    published artifacts (via `_publish_result`'s `updater.add_artifact`
    below) for one whose data carries this exact id. Real and persisted —
    verified against the installed a2a-sdk (1.1.2): a second message/send
    for the same non-terminal task_id re-enters `execute()` with
    `context.current_task.artifacts` reflecting everything an earlier
    call on this task published, via `InMemoryTaskStore`. No inference:
    either the id matches something this agent itself proposed, or it
    doesn't."""
    if task is None:
        return None
    for artifact in task.artifacts:
        for part in artifact.parts:
            if part.HasField("data"):
                data = json_format.MessageToDict(part.data)
                if data.get("id") == action_id:
                    return data
    return None


async def _publish_result(updater: TaskUpdater, result: dict) -> None:
    # Every artifact this agent kind declares (agent_meta.artifacts) that
    # `workflow.py` found present on this run — published under that same
    # name, so oncall keeps publishing "root_cause"/"pull_request" exactly
    # as documented (docs/DEVELOPER_GUIDE.md) and a different agent kind
    # publishes whatever it declared, with zero agent-specific code here.
    for name in get_agent_meta().artifacts:
        value = result.get(name)
        if value:
            await updater.add_artifact([_dict_to_part(value)], name=name)

    final_message = updater.new_agent_message([Part(text=result["message"])])
    state = _TERMINAL_STATE_BY_STATUS.get(result["status"], TaskState.TASK_STATE_COMPLETED)
    await updater.update_status(state, message=final_message)


def _open_task_span(target, agent_kind: str, context: RequestContext, prompt: str,
                    confirmed_action: dict | None) -> str | None:
    """Records this task's root span under the caller's and returns its
    traceparent.

    Ended at once, the way mcp-service's own durable tasks do it: the
    workflow outlives any one API pod, so a span held open across
    `handle.result()` would be lost — orphaning every activity span under
    it — whenever this pod restarts mid-run. The outcome is recorded by the
    worker, from the `respond` step (orchestration/activities.py), so it
    survives that restart too."""
    try:
        with tracing.span(
            f"a2a-task:{agent_kind}",
            target=target,
            traceparent=target.traceparent,
            attributes={
                "trace.entity": "agent-task",
                "agent.kind": agent_kind,
                "a2a.task_id": context.task_id,
                "a2a.context_id": context.context_id,
                "agent.query": tracing.truncate(prompt),
                "agent.confirmed_action": (confirmed_action or {}).get("id"),
            },
        ) as span:
            return tracing.traceparent_of(span)
    except Exception:  # noqa: BLE001 - tracing must never fail a request
        return None


def _record_task_error(target, config: dict, error: Exception) -> None:
    """The one outcome the worker cannot record: the workflow never produced
    a result at all. Never raises."""
    if target is None:
        return
    try:
        with tracing.span(
            "a2a-task-result",
            target=target,
            traceparent=config["trace"].get("traceparent"),
            attributes={"trace.entity": "agent-task-result", "agent.status": "failed"},
        ) as span:
            tracing.mark_failed(span, error)
    except Exception:  # noqa: BLE001 - tracing must never fail a request
        pass


class AriAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        prompt = context.get_user_input()
        config = _extract_config(context)
        history = _extract_history(context)

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # The framework requires the actual Task object as the first
        # event — a bare TaskStatusUpdateEvent (e.g. from
        # `updater.submit()`) alone isn't accepted as the first event and
        # errors with "Agent should enqueue Task before
        # TaskStatusUpdateEvent event" (found by calling the endpoint, not
        # documented). This first `enqueue_event` is also what lets a
        # non-blocking caller's message/send response return immediately,
        # well before the underlying Temporal workflow (started below)
        # finishes.
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )
        await updater.start_work()

        # A structured, non-conversational confirmation of a prior
        # pending_action — e.g. jenkins_agent's trigger_build proposal —
        # rather than the planner inferring one from a plain-text "yes".
        # See docs/design.md §5b: resolved deterministically against this
        # same task's own previously published artifacts, never an LLM.
        confirm_request = config.pop("confirm_pending_action", None)
        confirmed_action = None
        if confirm_request:
            confirmed_action = _find_pending_action(context.current_task, confirm_request.get("id", ""))
            if confirmed_action is None:
                await updater.failed(
                    message=updater.new_agent_message(
                        [
                            Part(
                                text=(
                                    f"Could not find a pending action with id "
                                    f"{confirm_request.get('id')!r} to confirm in this conversation — "
                                    "it may be stale, already handled, or this isn't the right "
                                    "conversation. Ask again and I'll propose it fresh."
                                )
                            )
                        ]
                    )
                )
                return

        settings = get_settings()
        target = tracing.target_for(config)
        if target is not None:
            # Every activity re-roots its spans on this task's own span, so
            # the traceparent the workflow carries is ours, not the caller's.
            config["trace"]["traceparent"] = _open_task_span(
                target, settings.agent_kind, context, prompt, confirmed_action
            ) or target.traceparent
        workflow_input = {
            "prompt": prompt,
            "history": history,
            "config": config,
            "confirmed_action": confirmed_action,
        }

        try:
            client = await get_temporal_client()
            handle = await client.start_workflow(
                AriAgentWorkflow.run,
                workflow_input,
                id=context.task_id,
                task_queue=settings.task_queue_for(config),
            )
            result = await handle.result()
        except Exception as exc:
            _record_task_error(target, config, exc)
            await updater.failed(message=updater.new_agent_message([Part(text=f"Internal error: {exc}")]))
            return

        await _publish_result(updater, result)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(context.task_id)
        await handle.cancel()

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
