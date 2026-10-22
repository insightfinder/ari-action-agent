import asyncio
from typing import Any, Awaitable, Callable

import structlog
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from chat_gateway.a2a_client import AgentError
from chat_gateway.tasks import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, context_id_of, status_text, task_state

from .registry import AgentEntry, Registry
from .router import Complete, NoAgentAvailable, choose
from .settings import CoordinatorSettings
from .store import RouteStore, TaskLink

logger = structlog.get_logger()

STATES = {
    COMPLETED: TaskState.TASK_STATE_COMPLETED,
    FAILED: TaskState.TASK_STATE_FAILED,
    CANCELED: TaskState.TASK_STATE_CANCELED,
    INPUT_REQUIRED: TaskState.TASK_STATE_INPUT_REQUIRED,
    "TASK_STATE_REJECTED": TaskState.TASK_STATE_REJECTED,
}

ROUTER_ANSWER_CHARS = 300


def with_conversation(prompt: str, turns: list[dict[str, Any]]) -> str:
    earlier = [t for t in turns if t.get("request")]
    if not earlier:
        return prompt
    lines = ["Earlier in this conversation (for context; answer only the current request):"]
    for turn in earlier:
        lines.append(f"User: {turn['request']}")
        if turn.get("answer"):
            lines.append(f"{turn.get('agent') or 'Agent'} agent answered: {turn['answer']}")
    lines += ["", f"Current request: {prompt}"]
    return "\n".join(lines)


def router_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = []
    for turn in turns:
        item = {"agent": turn.get("agent", ""), "request": turn.get("request", "")}
        if turn.get("answer"):
            item["answer"] = str(turn["answer"])[:ROUTER_ANSWER_CHARS]
        trimmed.append(item)
    return trimmed


DEFAULT_TEXT = {
    COMPLETED: "Done.",
    FAILED: "The agent could not finish this request.",
    CANCELED: "Canceled.",
    INPUT_REQUIRED: "Waiting for your approval.",
}


def data_part(data: dict[str, Any]) -> Part:
    value = Value()
    json_format.ParseDict(data, value)
    return Part(data=value)


def to_part(part: Any) -> Part | None:
    if not isinstance(part, dict):
        return None
    if isinstance(part.get("data"), dict):
        return data_part(part["data"])
    if isinstance(part.get("text"), str) and part["text"]:
        return Part(text=part["text"])
    return None


def request_config(context: RequestContext) -> dict[str, Any] | None:
    message = context.message
    if message is None:
        return None
    for part in message.parts:
        if part.HasField("data"):
            data = json_format.MessageToDict(part.data)
            return data if isinstance(data, dict) else None
    return None


async def publish(updater: TaskUpdater, downstream: dict[str, Any]) -> None:
    for index, artifact in enumerate(downstream.get("artifacts") or []):
        if not isinstance(artifact, dict):
            continue
        parts = [p for p in (to_part(x) for x in artifact.get("parts") or []) if p is not None]
        if not parts:
            continue
        name = str(artifact.get("name") or "artifact")
        artifact_id = str(artifact.get("artifactId") or artifact.get("artifact_id") or f"{name}-{index}")
        await updater.add_artifact(parts, artifact_id=artifact_id, name=name)
    state = task_state(downstream)
    text = status_text(downstream) or DEFAULT_TEXT.get(state, "The agent returned an unexpected state.")
    await updater.update_status(STATES.get(state, TaskState.TASK_STATE_FAILED), message=updater.new_agent_message([Part(text=text)]))


class CoordinatorExecutor(AgentExecutor):
    def __init__(
        self,
        registry: Registry,
        store: RouteStore,
        settings: CoordinatorSettings,
        *,
        complete: Complete | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.registry = registry
        self.store = store
        self.settings = settings
        self._complete = complete
        self._sleep = sleep

    async def follow(self, agent: AgentEntry, task_id: str) -> dict[str, Any]:
        return await self.registry.client(agent).wait(
            task_id,
            poll_seconds=self.settings.coordinator_poll_seconds,
            budget_seconds=self.settings.coordinator_follow_budget_seconds,
            outage_seconds=self.settings.coordinator_agent_outage_seconds,
            sleep=self._sleep,
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await event_queue.enqueue_event(
            Task(id=context.task_id, context_id=context.context_id, status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED))
        )
        await updater.start_work()

        prompt = context.get_user_input()
        config = request_config(context)
        if config is None:
            await self._fail(updater, "no config DataPart found on the message; send the llm (and repo) config alongside the prompt")
            return

        agent: AgentEntry | None = None
        try:
            link = self.store.task(context.task_id)
            if link is not None:
                agent = self.registry.get(link.agent)
                if agent is None:
                    await self._fail(updater, f"the agent that handled this task, {link.agent}, is no longer configured")
                    return
                await self.registry.client(agent).send(
                    prompt, self.registry.forward(agent, config), context_id=link.context_id, task_id=link.task_id
                )
                downstream_id = link.task_id
            else:
                memory = self.store.context(context.context_id)
                route = await choose(
                    self.registry,
                    prompt,
                    llm=config.get("llm"),
                    turns=router_turns(memory["turns"]),
                    current=memory["agent"],
                    complete=self._complete,
                )
                agent = self.registry.get(route.agent)
                await updater.add_artifact(
                    [data_part({"agent": route.agent, "reason": route.reason, "decided_by": route.decided_by})],
                    artifact_id="route",
                    name="route",
                )
                context_turns = memory["turns"][-self.settings.coordinator_context_turns:] if self.settings.coordinator_context_turns > 0 else []
                task = await self.registry.client(agent).send(
                    with_conversation(prompt, context_turns),
                    self.registry.forward(agent, config),
                    context_id=memory["downstream"].get(route.agent, ""),
                )
                downstream_id = str(task.get("id") or "")
                if not downstream_id:
                    await self._fail(updater, f"the {route.agent} agent accepted the request but returned no task id")
                    return
                self.store.link_task(context.task_id, TaskLink(route.agent, downstream_id, context_id_of(task)))
                self.store.remember(
                    context.context_id,
                    route.agent,
                    context_id_of(task),
                    prompt,
                    max_turns=self.settings.coordinator_route_turns,
                    task_id=context.task_id,
                )
                logger.info(
                    "coordinator_routed",
                    task_id=context.task_id,
                    agent=route.agent,
                    downstream_task_id=downstream_id,
                    decided_by=route.decided_by,
                    reason=route.reason,
                )
            downstream = await self.follow(agent, downstream_id)
        except NoAgentAvailable as exc:
            await self._fail(updater, str(exc))
            return
        except AgentError as exc:
            await self._fail(updater, f"the {agent.name if agent else 'chosen'} agent answered with an error: {exc}")
            return
        except TimeoutError as exc:
            await self._fail(updater, f"{exc}; ask for the status again later")
            return
        except RuntimeError as exc:
            await self._fail(updater, f"could not reach the {agent.name if agent else 'chosen'} agent: {exc}")
            return

        answer = status_text(downstream)
        if answer:
            self.store.record_answer(context.context_id, context.task_id, answer)
        try:
            await publish(updater, downstream)
        except Exception as exc:
            logger.warning("coordinator_publish_failed", task_id=context.task_id, error=str(exc)[:300])

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        link = self.store.task(context.task_id)
        agent = self.registry.get(link.agent) if link else None
        if link is not None and agent is not None:
            try:
                await self.registry.client(agent).cancel(link.task_id)
            except AgentError as exc:
                if exc.status_code >= 500:
                    raise
                logger.info("coordinator_downstream_cancel_refused", task_id=context.task_id, agent=agent.name, status=exc.status_code)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    async def _fail(self, updater: TaskUpdater, text: str) -> None:
        try:
            await updater.failed(message=updater.new_agent_message([Part(text=text)]))
        except Exception as exc:
            logger.warning("coordinator_fail_not_recorded", error=str(exc)[:300])
