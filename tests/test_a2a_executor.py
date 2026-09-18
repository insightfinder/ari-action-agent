import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus  # noqa: E402

from ari_agent.a2a_server.executor import (  # noqa: E402
    _dict_to_part,
    _extract_config,
    _extract_history,
    _publish_result,
)


def _data_part(data: dict) -> Part:
    value = Value()
    json_format.ParseDict(data, value)
    return Part(data=value)


def _context_with_message(parts, current_task=None):
    ctx = MagicMock()
    ctx.message = Message(message_id="m1", role=Role.ROLE_USER, parts=parts)
    ctx.current_task = current_task
    return ctx


def test_extract_config_finds_the_data_part_regardless_of_order():
    config = {"repo": {"url": "https://github.com/x/y"}, "llm": {"provider": "openai"}}
    ctx = _context_with_message([Part(text="fix the bug"), _data_part(config)])

    extracted = _extract_config(ctx)

    assert extracted == config


def test_extract_config_raises_clear_error_when_no_data_part():
    ctx = _context_with_message([Part(text="fix the bug")])

    with pytest.raises(ValueError, match="no config DataPart"):
        _extract_config(ctx)


def test_extract_config_raises_when_message_missing():
    ctx = MagicMock()
    ctx.message = None

    with pytest.raises(ValueError, match="no message"):
        _extract_config(ctx)


def test_extract_history_empty_when_no_current_task():
    ctx = _context_with_message([Part(text="hi")], current_task=None)
    assert _extract_history(ctx) == []


def test_extract_history_reconstructs_roles_and_text_from_task_history():
    task = Task(
        id="t1",
        context_id="c1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        history=[
            Message(message_id="m1", role=Role.ROLE_USER, parts=[Part(text="please fix the flaky test")]),
            Message(message_id="m2", role=Role.ROLE_AGENT, parts=[Part(text="working on it")]),
        ],
    )
    ctx = _context_with_message([Part(text="any update?")], current_task=task)

    history = _extract_history(ctx)

    assert history == [
        {"role": "user", "content": "please fix the flaky test"},
        {"role": "agent", "content": "working on it"},
    ]


def test_dict_to_part_round_trips():
    part = _dict_to_part({"url": "https://github.com/x/y/pull/1", "branch": "b"})
    assert json_format.MessageToDict(part.data) == {"url": "https://github.com/x/y/pull/1", "branch": "b"}


@pytest.mark.asyncio
async def test_publish_result_completed_with_pr_and_root_cause():
    updater = MagicMock()
    updater.add_artifact = AsyncMock()
    updater.update_status = AsyncMock()
    updater.new_agent_message = MagicMock(side_effect=lambda parts: Message(message_id="m", role=Role.ROLE_AGENT, parts=parts))

    result = {
        "status": "completed",
        "message": "Opened PR #1",
        "root_cause": {"summary": "npe", "evidence": []},
        "pr": {"url": "https://github.com/x/y/pull/1", "branch": "ari/fix-1"},
    }

    await _publish_result(updater, result)

    assert updater.add_artifact.await_count == 2
    updater.update_status.assert_awaited_once()
    state_arg = updater.update_status.await_args.args[0]
    assert state_arg == TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_publish_result_maps_failed_status_to_failed_state():
    updater = MagicMock()
    updater.add_artifact = AsyncMock()
    updater.update_status = AsyncMock()
    updater.new_agent_message = MagicMock(side_effect=lambda parts: Message(message_id="m", role=Role.ROLE_AGENT, parts=parts))

    await _publish_result(updater, {"status": "failed", "message": "gave up"})

    updater.add_artifact.assert_not_awaited()
    state_arg = updater.update_status.await_args.args[0]
    assert state_arg == TaskState.TASK_STATE_FAILED
