import json
import os
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ari_cli.main as cli
from chat_gateway.a2a_client import AgentClient
from chat_gateway.config import GatewaySettings
from tests.ari_cli_fakes import PENDING, PR, _task

runner = CliRunner()

def test_card_prints_the_agent_card(agent):
    result = runner.invoke(cli.app, ["card"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["skills"][0]["id"] == "code_analysis_fix"


def test_send_follows_the_task_and_prints_the_pull_request(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_WORKING"), _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened it."))
    result = runner.invoke(cli.app, ["send", "fix the bug", "--base", "staging"])
    assert result.exit_code == 0, result.output
    assert "working (task task-1)" in result.output
    assert result.output.count("Opened it.") == 1
    assert "https://github.com/org/repo/pull/9" in result.output
    message = agent.sends[0]["message"]
    assert message["parts"][0]["text"] == "fix the bug\nIf this request leads to a pull request, open it against the staging branch."
    assert message["parts"][1]["data"]["repo"] == {"url": "https://github.com/org/repo", "pat": "ghp_test"}
    assert message["parts"][1]["data"]["llm"]["api_key"] == "sk-test"


def test_send_exits_non_zero_when_the_task_fails(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_FAILED", text="could not clone"))
    result = runner.invoke(cli.app, ["send", "fix"])
    assert result.exit_code == 1
    assert "could not clone" in result.output


def test_send_no_wait_prints_only_the_task_id(agent):
    result = runner.invoke(cli.app, ["send", "fix", "--no-wait", "--base", "dev"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "task-1"
    assert len(agent.sends) == 1
    assert agent.sends[0]["message"]["parts"][0]["text"].endswith("against the dev branch.")


def test_send_tells_how_to_approve_a_paused_task(agent):
    agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    result = runner.invoke(cli.app, ["send", "fix"])
    assert result.exit_code == 0, result.output
    assert "Proposed: Open pull request" in result.output
    assert "approve with: ari approve task-1" in result.output


def test_approve_command_confirms_with_the_proposals_own_repository(agent):
    agent.script(
        "task-7",
        _task("task-7", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-7", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    result = runner.invoke(cli.app, ["approve", "task-7"])
    assert result.exit_code == 0, result.output
    assert result.output.count("https://github.com/org/repo/pull/9") == 1
    confirm = agent.sends[0]["message"]
    assert confirm["task_id"] == "task-7"
    assert confirm["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}
    assert confirm["parts"][1]["data"]["repo"] == {"url": "https://github.com/org/other", "pat": "ghp_test"}


def test_approve_survives_the_agent_restarting_while_it_follows_the_resume(agent):
    agent.outage_after_confirm = 7
    agent.script(
        "task-7",
        _task("task-7", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-7", "TASK_STATE_WORKING"),
        _task("task-7", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    result = runner.invoke(cli.app, ["approve", "task-7"])
    assert result.exit_code == 0, result.output
    assert result.output.count("agent unavailable (tasks/get returned HTTP 503: no available server)") == 1
    assert "https://github.com/org/repo/pull/9" in result.output
    assert agent.failing_gets["task-7"] == 0


def test_tasks_follow_reattaches_to_a_running_task(agent):
    agent.script("task-5", _task("task-5", "TASK_STATE_WORKING"), _task("task-5", "TASK_STATE_COMPLETED", PR, "Opened."))
    result = runner.invoke(cli.app, ["tasks", "follow", "task-5"])
    assert result.exit_code == 0, result.output
    assert "working (task task-5)" in result.output
    assert "https://github.com/org/repo/pull/9" in result.output
    agent.script("task-6", _task("task-6", "TASK_STATE_FAILED", text="gave up"))
    result = runner.invoke(cli.app, ["tasks", "follow", "task-6"])
    assert result.exit_code == 1
    assert "gave up" in result.output


def test_approve_refuses_a_task_that_is_not_paused(agent):
    agent.script("task-7", _task("task-7", "TASK_STATE_COMPLETED", text="done"))
    result = runner.invoke(cli.app, ["approve", "task-7"])
    assert result.exit_code == 1
    assert "not waiting for approval" in result.output
    assert agent.sends == []


def test_approve_needs_a_pat_so_the_resume_can_open_the_pull_request(agent, monkeypatch):
    monkeypatch.delenv("GITHUB_PAT")
    agent.script("task-7", _task("task-7", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    result = runner.invoke(cli.app, ["approve", "task-7"])
    assert result.exit_code == 1
    assert "GITHUB_PAT" in result.output
    assert agent.sends == []


def test_tasks_get_redacts_secrets_and_cancel_reports_the_state(agent):
    history = [{"role": "ROLE_USER", "parts": [{"text": "fix"}, {"data": {"repo": {"url": "u", "pat": "ghp_secret"}, "llm": {"api_key": "sk-secret"}}}]}]
    agent.script("task-3", _task("task-3", "TASK_STATE_WORKING", history=history))
    result = runner.invoke(cli.app, ["tasks", "get", "task-3"])
    assert result.exit_code == 0, result.output
    assert "ghp_secret" not in result.output and "sk-secret" not in result.output
    assert json.loads(result.output)["history"][0]["parts"][1]["data"]["repo"]["pat"] == "***"
    result = runner.invoke(cli.app, ["tasks", "cancel", "task-3"])
    assert result.exit_code == 0, result.output
    assert agent.cancels == ["task-3"]
    assert "TASK_STATE_CANCELED" in result.output


def test_an_unreachable_agent_is_an_error_line_not_a_traceback(agent, monkeypatch):
    def make_client(settings):
        def refuse(request):
            raise httpx.ConnectError("All connection attempts failed", request=request)

        return AgentClient("http://127.0.0.1:9", "k", http=httpx.AsyncClient(transport=httpx.MockTransport(refuse), timeout=1))

    monkeypatch.setattr(cli, "make_client", make_client)
    result = runner.invoke(cli.app, ["card"])
    assert result.exit_code == 1
    assert "error: agent card failed after" in result.output
    assert "Traceback" not in result.output


def test_config_set_show_and_precedence(agent, monkeypatch, tmp_path):
    result = runner.invoke(cli.app, ["config", "set", "ARI_AGENT_URL", "http://from-file"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == 'ari_agent_url = "http://from-file"\n'

    assert cli.load_settings().ari_agent_url == "http://from-file"
    monkeypatch.setenv("ARI_AGENT_URL", "http://from-env")
    assert cli.load_settings().ari_agent_url == "http://from-env"
    assert cli.load_settings(ari_agent_url="http://from-flag").ari_agent_url == "http://from-flag"

    shown = runner.invoke(cli.app, ["config", "show"])
    assert shown.exit_code == 0, shown.output
    assert "llm_api_key = sk-t…" in shown.output
    assert "sk-test" not in shown.output
    assert "ari_agent_url = http://from-env" in shown.output


def test_config_set_prompts_for_a_secret_without_echo_and_round_trips_unicode(agent, tmp_path):
    result = runner.invoke(cli.app, ["config", "set", "github_pat"], input="ghp_prompted\n")
    assert result.exit_code == 0, result.output
    assert "ghp_prompted" not in result.output
    result = runner.invoke(cli.app, ["config", "set", "default_repo_ref", "feature/😀-ünïcode"])
    assert result.exit_code == 0, result.output
    saved = cli.read_config_file(tmp_path / "config.toml")
    assert saved["github_pat"] == "ghp_prompted"
    assert saved["default_repo_ref"] == "feature/😀-ünïcode"
    assert cli.load_settings().default_repo_ref == "feature/😀-ünïcode"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_config_file_is_private_to_the_user(agent, tmp_path):
    runner.invoke(cli.app, ["config", "set", "llm_api_key", "sk-x"])
    assert (tmp_path / "config.toml").stat().st_mode & 0o077 == 0


def test_config_set_rejects_unknown_keys_and_invalid_values(agent):
    result = runner.invoke(cli.app, ["config", "set", "nope", "x"])
    assert result.exit_code == 2
    assert "unknown setting" in result.output
    result = runner.invoke(cli.app, ["config", "set", "poll_seconds", "abc"])
    assert result.exit_code == 2
    assert "invalid value for poll_seconds" in result.output
    assert runner.invoke(cli.app, ["config", "show"]).exit_code == 0


def test_a_bad_environment_value_is_reported_not_traced(agent, monkeypatch):
    monkeypatch.setenv("POLL_SECONDS", "abc")
    result = runner.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 1
    assert "error: invalid setting: poll_seconds" in result.output
    assert "Traceback" not in result.output


def test_missing_llm_key_is_reported_not_traced(agent, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY")
    for args in (["send", "fix"], ["send", "fix", "--no-wait"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 1
        assert "LLM_API_KEY" in result.output
        assert "Traceback" not in result.output


def test_task_output_masks_the_trace_license():
    # A task started by ARI carries the caller's InsightFinder license in its `trace` block.
    cli = pytest.importorskip("ari_cli.main")
    shown = cli.redact({"trace": {"trace_admin_user_license": "if-license", "trace_project": "acme-LLM"}})
    assert shown == {"trace": {"trace_admin_user_license": "***", "trace_project": "acme-LLM"}}
