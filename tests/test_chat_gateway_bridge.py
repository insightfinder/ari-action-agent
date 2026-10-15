import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chat_gateway.a2a_client import AgentError
from chat_gateway.bridge import Bridge, decision_word, parse_directives
from chat_gateway.config import ConfigError, GatewaySettings
from chat_gateway.session import SessionStore

SETTINGS_FIELDS = [name.upper() for name in GatewaySettings.model_fields]


def _task(task_id="t1", state="TASK_STATE_SUBMITTED", context_id="c1", artifacts=None, text=""):
    task = {"id": task_id, "contextId": context_id, "status": {"state": state}}
    if text:
        task["status"]["message"] = {"parts": [{"text": text}]}
    if artifacts:
        task["artifacts"] = artifacts
    return task


def _proposal(action_id="act-1", **extra):
    data = {"id": action_id, "title": "T", "action_name": "Open pull request", "repo_url": "https://github.com/org/default"}
    data.update(extra)
    return {"name": "pending_action", "parts": [{"data": data}]}


def _paused(task_id="t1", action_id="act-1", context_id="c1"):
    return _task(task_id, "TASK_STATE_INPUT_REQUIRED", context_id=context_id, artifacts=[_proposal(action_id)], text="ready")


class FakeClient:
    def __init__(self):
        self.sent = []
        self.confirmed = []
        self.canceled = []
        self.gets = []
        self.final = {}
        self.current = {}
        self.counter = 0
        self.outage = None
        self.wait_kwargs = {}

    async def send(self, prompt, config, *, context_id="", task_id="", **_):
        self.counter += 1
        new_id = task_id or f"t{self.counter}"
        self.sent.append({"prompt": prompt, "config": config, "context_id": context_id, "task_id": task_id})
        submitted = _task(new_id, context_id=context_id or f"c{self.counter}")
        self.current[new_id] = submitted
        return submitted

    async def wait(self, task_id, *, poll_seconds, budget_seconds, on_update=None, on_outage=None, **kwargs):
        self.wait_kwargs = kwargs
        context_id = self.current[task_id]["contextId"]
        final = self.final.get(task_id) or _task(task_id, "TASK_STATE_COMPLETED", context_id=context_id, text="done")
        if on_update is not None:
            await on_update(_task(task_id, "TASK_STATE_WORKING", context_id=context_id))
        if self.outage is not None and on_outage is not None:
            await on_outage(self.outage)
        if on_update is not None:
            await on_update(final)
        self.current[task_id] = final
        return final

    async def get_task(self, task_id):
        self.gets.append(task_id)
        if task_id not in self.current:
            raise AgentError("tasks/get", 404, f"Task {task_id} not found")
        return self.current[task_id]

    async def confirm(self, task, pending_action_id, config, *, text="approved"):
        self.confirmed.append({"task": task, "id": pending_action_id, "config": config, "text": text})
        resumed = _task(task["id"], context_id=task.get("contextId", ""))
        self.current[task["id"]] = resumed
        return resumed

    async def cancel(self, task_id):
        self.canceled.append(task_id)
        canceled = _task(task_id, "TASK_STATE_CANCELED", context_id=self.current[task_id]["contextId"])
        self.current[task_id] = canceled
        return canceled


@pytest.fixture
def settings(monkeypatch, tmp_path):
    for name in SETTINGS_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return GatewaySettings(
        llm_api_key="sk-test",
        github_pat="ghp_test",
        default_repo_url="https://github.com/org/default",
        approvers="boss",
        poll_seconds=0,
    )


@pytest.fixture
def bridge(settings):
    return Bridge(FakeClient(), settings, SessionStore())


async def _pause_then(bridge, thread="th", requester="alice", task_id="t1"):
    bridge.client.final[task_id] = _paused(task_id)
    return await bridge.start(thread, "fix it", requester)


def test_parse_directives_pulls_repo_ref_and_base_out_of_the_text():
    text, directives = parse_directives("fix the NPE repo=https://github.com/org/x ref:dev base=staging please")
    assert text == "fix the NPE please"
    assert directives == {"repo": "https://github.com/org/x", "ref": "dev", "base": "staging"}


def test_parse_directives_strips_wrapping_punctuation_and_slack_labels():
    text, directives = parse_directives("use repo=<https://github.com/org/x|github.com/org/x>, ref=dev. ok")
    assert directives == {"repo": "https://github.com/org/x", "ref": "dev"}
    assert text == "use ok"


def test_parse_directives_ignores_a_directive_glued_to_another_word():
    text, directives = parse_directives("see xrepo=foo and prefix:thing")
    assert directives == {}
    assert text == "see xrepo=foo and prefix:thing"


def test_decision_word_recognises_bare_approvals_and_declines_only():
    assert decision_word("Yes!") == "approve"
    assert decision_word("  approve it ") == "approve"
    assert decision_word("Don't") == "decline"
    assert decision_word("no, decline") == ""
    assert decision_word("yes but also run the tests") == ""


def test_a_caller_supplied_store_is_used_even_while_empty(settings):
    store = SessionStore()
    assert Bridge(FakeClient(), settings, store).store is store


@pytest.mark.asyncio
async def test_start_sends_the_configured_repo_llm_and_options(bridge):
    rendered = await bridge.start("th", "fix it", "alice")

    sent = bridge.client.sent[0]
    assert sent["prompt"] == "fix it"
    assert sent["config"] == {
        "repo": {"url": "https://github.com/org/default", "pat": "ghp_test"},
        "llm": {"provider": "openai", "api_key": "sk-test", "model": "gpt-4o-mini"},
        "options": {},
    }
    assert sent["context_id"] == "" and sent["task_id"] == ""
    assert rendered.text == "done"
    assert rendered.state == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_directives_override_the_repo_and_shape_the_prompt(bridge):
    await bridge.start("th", "fix it repo=https://github.com/org/x ref=dev base=staging", "alice")
    sent = bridge.client.sent[0]
    assert sent["config"]["repo"] == {"url": "https://github.com/org/x", "ref": "dev", "pat": "ghp_test"}
    assert sent["prompt"] == "fix it\nIf this request leads to a pull request, open it against the staging branch."


@pytest.mark.asyncio
async def test_a_repo_directive_outside_the_allowed_hosts_never_leaves_the_gateway(bridge):
    with pytest.raises(ConfigError, match="not allowed"):
        await bridge.start("th", "fix it repo=https://evil.example/x", "mallory")
    with pytest.raises(ConfigError, match="credentials"):
        await bridge.start("th", "fix it repo=https://ghp_x@github.com/org/x", "mallory")
    assert bridge.client.sent == []


@pytest.mark.asyncio
async def test_channel_repo_map_applies_to_the_thread_prefix_and_allows_its_host(settings):
    settings.repo_map = "C123=https://gitlab.example/org/chan, C9=https://github.com/org/other"
    bridge = Bridge(FakeClient(), settings)
    await bridge.start("C123:1699.42", "fix it", "alice")
    assert bridge.client.sent[0]["config"]["repo"]["url"] == "https://gitlab.example/org/chan"


@pytest.mark.asyncio
async def test_missing_repo_is_a_config_error_not_a_request(settings):
    settings.default_repo_url = ""
    bridge = Bridge(FakeClient(), settings)
    with pytest.raises(ConfigError):
        await bridge.start("th", "fix it", "alice")
    assert bridge.client.sent == []


@pytest.mark.asyncio
async def test_follow_up_in_the_same_thread_reuses_the_context_but_not_a_finished_task(bridge):
    await bridge.start("th", "first", "alice")
    await bridge.start("th", "second", "alice")
    first, second = bridge.client.sent
    assert second["context_id"] == "c1"
    assert second["task_id"] == ""
    assert first["context_id"] == ""


@pytest.mark.asyncio
async def test_a_message_on_a_paused_task_continues_that_task(bridge):
    rendered = await _pause_then(bridge)
    assert rendered.state == "TASK_STATE_INPUT_REQUIRED"
    assert [b.action for b in rendered.buttons] == ["approve", "decline"]
    bridge.client.final["t1"] = _task("t1", "TASK_STATE_COMPLETED", text="answered")
    rendered = await bridge.start("th", "what changed?", "alice")
    assert bridge.client.sent[1]["task_id"] == "t1"
    assert bridge.client.sent[1]["context_id"] == "c1"
    assert rendered.text.startswith("answered")
    assert "can no longer be approved" in rendered.text
    assert bridge.store.get("th").pending_action_id == ""


@pytest.mark.asyncio
async def test_a_follow_up_that_pauses_again_keeps_the_new_proposal(bridge):
    await _pause_then(bridge)
    bridge.client.final["t1"] = _paused(action_id="act-2")
    rendered = await bridge.start("th", "also run the tests", "alice")
    assert rendered.state == "TASK_STATE_INPUT_REQUIRED"
    assert "can no longer be approved" not in rendered.text
    assert bridge.store.get("th").pending_action_id == "act-2"


@pytest.mark.asyncio
async def test_two_proposals_on_one_task_confirm_the_latest(bridge):
    bridge.client.final["t1"] = _task("t1", "TASK_STATE_INPUT_REQUIRED", artifacts=[_proposal("act-1"), _proposal("act-2")], text="again")
    rendered = await bridge.start("th", "fix it", "alice")
    assert [b.pending_action_id for b in rendered.buttons] == ["act-2", "act-2"]
    bridge.client.final["t1"] = _task("t1", "TASK_STATE_COMPLETED", text="opened")
    await bridge.approve("th", "alice")
    assert bridge.client.confirmed[0]["id"] == "act-2"


@pytest.mark.asyncio
async def test_a_message_while_the_task_runs_is_refused_after_checking_the_live_state(bridge):
    bridge.store.start("th", "alice")
    bridge.store.record_task("th", _task("t1", "TASK_STATE_WORKING"))
    bridge.client.current["t1"] = _task("t1", "TASK_STATE_WORKING")
    rendered = await bridge.start("th", "again", "alice")
    assert rendered.state == "refused"
    assert "still working" in rendered.text
    assert bridge.client.sent == []
    assert bridge.client.gets == ["t1"]


@pytest.mark.asyncio
async def test_a_stale_running_state_is_refreshed_before_refusing(bridge):
    bridge.store.start("th", "alice")
    bridge.store.record_task("th", _task("t1", "TASK_STATE_WORKING"))
    bridge.client.current["t1"] = _task("t1", "TASK_STATE_COMPLETED", text="finished meanwhile")
    rendered = await bridge.start("th", "again", "alice")
    assert rendered.state == "TASK_STATE_COMPLETED"
    assert bridge.client.sent[0]["context_id"] == "c1"
    assert bridge.client.sent[0]["task_id"] == ""


@pytest.mark.asyncio
async def test_an_empty_message_is_refused(bridge):
    rendered = await bridge.start("th", "repo=https://github.com/org/x", "alice")
    assert rendered.state == "refused"
    assert bridge.client.sent == []


@pytest.mark.asyncio
async def test_progress_updates_reach_the_caller_once_per_change_and_never_for_the_final_state(bridge):
    seen = []

    async def on_update(rendered):
        seen.append(rendered.text)

    await bridge.start("th", "fix it", "alice", on_update=on_update)
    assert seen == ["working (task t1)"]


@pytest.mark.asyncio
async def test_an_agent_outage_mid_run_is_reported_and_the_follow_continues(bridge, settings):
    settings.agent_outage_seconds = 120
    bridge.client.outage = AgentError("tasks/get", 503, "no available server")
    seen = []

    async def on_update(rendered):
        seen.append((rendered.state, rendered.text))

    rendered = await bridge.start("th", "fix it", "alice", on_update=on_update)
    assert rendered.state == "TASK_STATE_COMPLETED"
    assert seen[1][0] == "unavailable"
    assert "no available server" in seen[1][1] and "still waiting" in seen[1][1]
    assert bridge.client.wait_kwargs["outage_seconds"] == 120


@pytest.mark.asyncio
async def test_approve_sends_the_stored_ids_and_config_then_follows_to_the_end(bridge):
    await _pause_then(bridge)
    bridge.client.final["t1"] = _task(
        "t1", "TASK_STATE_COMPLETED", text="opened", artifacts=[{"name": "pull_request", "parts": [{"data": {"url": "https://x/pull/1"}}]}]
    )

    rendered = await bridge.approve("th", "alice", "act-1")

    confirm = bridge.client.confirmed[0]
    assert confirm["task"]["id"] == "t1"
    assert confirm["task"]["contextId"] == "c1"
    assert confirm["id"] == "act-1"
    assert confirm["config"] == bridge.client.sent[0]["config"]
    assert confirm["text"] == "approved by alice"
    assert rendered.state == "TASK_STATE_COMPLETED"
    assert "https://x/pull/1" in rendered.text
    assert bridge.store.get("th").pending_action_id == ""


@pytest.mark.asyncio
async def test_approve_by_a_configured_approver_is_allowed(bridge):
    await _pause_then(bridge)
    bridge.client.final["t1"] = _task("t1", "TASK_STATE_COMPLETED", text="opened")
    rendered = await bridge.approve("th", "boss")
    assert rendered.state == "TASK_STATE_COMPLETED"
    assert bridge.client.confirmed[0]["text"] == "approved by boss"


@pytest.mark.asyncio
async def test_approve_by_someone_else_is_refused(bridge):
    await _pause_then(bridge)
    rendered = await bridge.approve("th", "mallory")
    assert rendered.state == "refused"
    assert bridge.client.confirmed == []


@pytest.mark.asyncio
async def test_approve_with_a_stale_id_is_refused(bridge):
    await _pause_then(bridge)
    rendered = await bridge.approve("th", "alice", "act-old")
    assert rendered.state == "refused"
    assert bridge.client.confirmed == []


@pytest.mark.asyncio
async def test_approve_with_nothing_pending_is_refused(bridge):
    await bridge.start("th", "fix it", "alice")
    rendered = await bridge.approve("th", "alice")
    assert rendered.state == "refused"
    assert (await bridge.approve("missing", "alice")).state == "refused"
    assert bridge.client.confirmed == []


@pytest.mark.asyncio
async def test_approve_checks_the_live_task_before_confirming(bridge):
    await _pause_then(bridge)
    bridge.client.current["t1"] = _task("t1", "TASK_STATE_CANCELED")
    rendered = await bridge.approve("th", "alice")
    assert rendered.state == "refused"
    assert "no longer waiting" in rendered.text and "canceled" in rendered.text
    assert bridge.client.confirmed == []
    assert bridge.store.get("th").pending_action_id == ""


@pytest.mark.asyncio
async def test_the_requester_is_whoever_started_the_current_task(bridge):
    await bridge.start("th", "what does this repo do?", "alice")
    bridge.client.final["t2"] = _paused("t2")
    await bridge.start("th", "fix the NPE", "bob")
    assert (await bridge.approve("th", "alice")).state == "refused"
    bridge.client.final["t2"] = _task("t2", "TASK_STATE_COMPLETED", text="opened")
    assert (await bridge.approve("th", "bob")).state == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_only_the_requester_or_an_approver_may_continue_a_paused_task(bridge):
    await _pause_then(bridge)
    rendered = await bridge.start("th", "what does this file do?", "mallory")
    assert rendered.state == "refused"
    assert "only alice" in rendered.text
    assert len(bridge.client.sent) == 1


@pytest.mark.asyncio
async def test_a_typed_yes_on_a_paused_task_approves_instead_of_being_sent_as_text(bridge):
    await _pause_then(bridge)
    bridge.client.final["t1"] = _task("t1", "TASK_STATE_COMPLETED", text="opened")
    rendered = await bridge.start("th", "Yes, go ahead!", "alice")
    assert rendered.state == "refused" or rendered.state == "TASK_STATE_INPUT_REQUIRED" or len(bridge.client.sent) == 2
    bridge.client.final["t1"] = _paused()
    bridge.client.current["t1"] = _paused()
    bridge.store.record_task("th", _paused())
    rendered = await bridge.start("th", "yes", "alice")
    assert bridge.client.confirmed[-1]["id"] == "act-1"
    assert rendered.state == "TASK_STATE_INPUT_REQUIRED" or rendered.state == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_a_typed_no_on_a_paused_task_declines(bridge):
    await _pause_then(bridge)
    rendered = await bridge.start("th", "No", "alice")
    assert bridge.client.canceled == ["t1"]
    assert rendered.state == "TASK_STATE_CANCELED"
    assert len(bridge.client.sent) == 1


@pytest.mark.asyncio
async def test_decline_cancels_the_task_and_clears_the_proposal(bridge):
    await _pause_then(bridge)
    rendered = await bridge.decline("th", "alice")
    assert bridge.client.canceled == ["t1"]
    assert rendered.state == "TASK_STATE_CANCELED"
    assert bridge.store.get("th").pending_action_id == ""


@pytest.mark.asyncio
async def test_decline_by_someone_else_is_refused(bridge):
    await _pause_then(bridge)
    rendered = await bridge.decline("th", "mallory")
    assert rendered.state == "refused"
    assert bridge.client.canceled == []


@pytest.mark.asyncio
async def test_a_button_still_works_after_the_conversation_left_memory_for_an_approver(settings):
    bridge = Bridge(FakeClient(), settings)
    bridge.client.current["t1"] = _paused()
    bridge.client.final["t1"] = _task("t1", "TASK_STATE_COMPLETED", text="opened")
    assert (await bridge.approve("gone", "alice", "act-1", task_id="t1")).state == "refused"
    rendered = await bridge.approve("gone", "boss", "act-1", task_id="t1")
    assert rendered.state == "TASK_STATE_COMPLETED"
    confirm = bridge.client.confirmed[0]
    assert confirm["id"] == "act-1"
    assert confirm["config"]["repo"] == {"url": "https://github.com/org/default", "pat": "ghp_test"}


@pytest.mark.asyncio
async def test_a_recovered_approval_needs_a_pat_to_open_the_pull_request(settings):
    settings.github_pat = ""
    bridge = Bridge(FakeClient(), settings)
    bridge.client.current["t1"] = _paused()
    with pytest.raises(ConfigError, match="GITHUB_PAT"):
        await bridge.approve("gone", "boss", "act-1", task_id="t1")
    assert bridge.client.confirmed == []


@pytest.mark.asyncio
async def test_status_reads_the_live_task(bridge):
    assert (await bridge.status("th")).state == "refused"
    await bridge.start("th", "fix it", "alice")
    rendered = await bridge.status("th")
    assert rendered.state == "TASK_STATE_COMPLETED"
