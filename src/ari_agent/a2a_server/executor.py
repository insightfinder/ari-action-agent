"""AriAgentExecutor — bridges the A2A protocol's Task lifecycle to our
existing Temporal workflow (`AriOnCallWorkflow`), which does all the
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

Deliberately out of scope for this pass (documented, not an oversight):
- No fine-grained per-capability progress (e.g. "now running verify") as
  separate status updates — Temporal's own workflow history already has
  that detail; this executor reports SUBMITTED → WORKING → one terminal
  state, not a blow-by-blow.
- No `TASK_STATE_INPUT_REQUIRED` (a genuine multi-turn "the agent is
  paused awaiting your reply" resume flow) — `capabilities/respond.py`
  always gives a definitive answer instead of leaving a task open.
"""

from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Role, Task, TaskState, TaskStatus

from ..orchestration.temporal_client import get_temporal_client
from ..orchestration.workflow import AriOnCallWorkflow
from ..settings import get_settings

_TERMINAL_STATE_BY_STATUS = {
    "completed": TaskState.TASK_STATE_COMPLETED,
    "no_action": TaskState.TASK_STATE_COMPLETED,
    "failed": TaskState.TASK_STATE_FAILED,
    # Reserved for a real policy/approval gate, not produced today — see
    # docs/design.md §5.4-equivalent notes on `needs_approval`.
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


async def _publish_result(updater: TaskUpdater, result: dict) -> None:
    if result.get("root_cause"):
        await updater.add_artifact([_dict_to_part(result["root_cause"])], name="root_cause")
    if result.get("pr"):
        await updater.add_artifact([_dict_to_part(result["pr"])], name="pull_request")

    final_message = updater.new_agent_message([Part(text=result["message"])])
    state = _TERMINAL_STATE_BY_STATUS.get(result["status"], TaskState.TASK_STATE_COMPLETED)
    await updater.update_status(state, message=final_message)


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

        settings = get_settings()
        workflow_input = {"prompt": prompt, "history": history, "config": config}

        try:
            client = await get_temporal_client()
            handle = await client.start_workflow(
                AriOnCallWorkflow.run,
                workflow_input,
                id=context.task_id,
                task_queue=settings.temporal_task_queue,
            )
            result = await handle.result()
        except Exception as exc:
            await updater.failed(message=updater.new_agent_message([Part(text=f"Internal error: {exc}")]))
            return

        await _publish_result(updater, result)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(context.task_id)
        await handle.cancel()

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
