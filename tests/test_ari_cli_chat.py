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
from tests.ari_cli_fakes import PENDING, PR, _task, proposal

runner = CliRunner()

SETUP_ANSWERS = [
    "http://agent.test",
    "agent-key",
    "1",
    "",
    "sk-new",
    "ghp_new",
    "https://github.com/org/repo",
    "dev",
]


def chat(*lines: str):
    return runner.invoke(cli.app, [], input="".join(f"{line}\n" for line in lines))


def refusing_client(settings):
    def refuse(request):
        raise httpx.ConnectError("All connection attempts failed", request=request)

    return AgentClient(settings.ari_agent_url, "k", http=httpx.AsyncClient(transport=httpx.MockTransport(refuse), timeout=1))


def test_ari_on_its_own_opens_the_chat_with_a_banner(chat_agent):
    result = chat("/quit")
    assert result.exit_code == 0, result.output
    assert "ARI On-Call Agent" in result.output
    assert "Connected to agent.test" in result.output
    assert "Repository: org/repo" in result.output
    assert "Works on the repository's default branch; pull requests target it" in " ".join(result.output.split())
    assert "Type a request, or /help for commands." in result.output


def test_a_question_is_answered_under_a_clear_label(chat_agent):
    chat_agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_WORKING"),
        _task("task-1", "TASK_STATE_COMPLETED", text="Two processes each wait for the other."),
    )
    result = chat("What is a deadlock?", "/quit")
    assert result.exit_code == 0, result.output
    assert "ARI ›" in result.output
    assert "Two processes each wait for the other." in result.output
    assert "Error" not in result.output
    assert chat_agent.sends[0]["message"]["parts"][0]["text"] == "What is a deadlock?"


def test_a_proposal_is_shown_as_a_card_and_approved_from_the_menu(chat_agent):
    chat_agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready to open a pull request."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    result = chat("fix the bug", "1", "/quit")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Proposed change" in out and "Fix it" in out
    assert "ari/fix-it → dev" in out
    assert "What do you want to do?" in out
    assert "1) Approve and open the pull request" in out
    assert "✔ Pull request opened: https://github.com/org/repo/pull/9" in out
    confirm = chat_agent.sends[1]["message"]
    assert confirm["task_id"] == "task-1"
    assert confirm["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}


def test_declining_from_the_menu_cancels_and_names_the_branch_left_behind(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    result = chat("fix the bug", "2", "/quit")
    assert result.exit_code == 0, result.output
    assert chat_agent.cancels == ["task-1"]
    assert "Declined. The branch ari/fix-it stays on the repository" in result.output


def test_show_the_diff_then_decide_later_then_approve_with_a_command(chat_agent):
    chat_agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    result = chat("fix the bug", "3", "5", "/approve", "/quit")
    assert result.exit_code == 0, result.output
    assert result.output.count("https://github.com/org/other/compare/dev...b") >= 2
    assert "The proposal is waiting. Type /approve or /decline" in result.output
    assert "✔ Pull request opened" in result.output


def test_a_follow_up_from_the_menu_continues_the_same_task(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    result = chat("fix the bug", "4", "also run the tests", "5", "/quit")
    assert result.exit_code == 0, result.output
    follow_up = chat_agent.sends[1]["message"]
    assert follow_up["task_id"] == "task-1"
    assert follow_up["parts"][0]["text"] == "also run the tests"
    assert result.output.count("What do you want to do?") == 2


def test_typing_yes_while_a_proposal_waits_approves_it(chat_agent):
    chat_agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )
    result = chat("fix the bug", "5", "yes", "/quit")
    assert result.exit_code == 0, result.output
    assert "✔ Pull request opened" in result.output
    assert len(chat_agent.sends) == 2
    assert chat_agent.sends[1]["message"]["parts"][1]["data"]["confirm_pending_action"] == {"id": "act-1"}


def test_help_lists_every_command_and_an_unknown_one_is_explained(chat_agent):
    result = chat("/help", "/nope", "/quit")
    assert result.exit_code == 0, result.output
    for command in ("/repo", "/branch", "/base", "/status", "/approve", "/decline", "/new", "/config", "/setup", "/quit"):
        assert command in result.output
    assert "Unknown command /nope. Type /help to see the commands." in result.output


def test_repo_branch_and_base_shape_the_next_request(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="ok"))
    result = chat(
        "/repo https://evil.example/x",
        "/repo https://github.com/org/other",
        "/branch dev",
        "/base staging",
        "fix it",
        "/quit",
    )
    assert result.exit_code == 0, result.output
    assert "Error: repository host 'evil.example' is not allowed" in result.output
    assert "Repository set to org/other" in result.output
    assert "Works on branch dev; pull requests target staging, from your next request on." in " ".join(result.output.split())
    assert len(chat_agent.sends) == 1
    message = chat_agent.sends[0]["message"]
    assert message["parts"][1]["data"]["repo"] == {"url": "https://github.com/org/other", "ref": "dev", "pat": "ghp_test"}
    assert message["parts"][0]["text"] == "fix it\nIf this request leads to a pull request, open it against the staging branch."


def test_new_starts_a_fresh_conversation(chat_agent):
    for task_id in ("task-1", "task-2", "task-3"):
        chat_agent.script(task_id, _task(task_id, "TASK_STATE_COMPLETED", text="ok"))
    result = chat("first", "second", "/new", "third", "/quit")
    assert result.exit_code == 0, result.output
    assert "Started a new conversation." in result.output
    first, second, third = (s["message"] for s in chat_agent.sends)
    assert "context_id" not in first
    assert second["context_id"] == "ctx-1"
    assert "context_id" not in third


def test_config_and_status_inside_the_chat(chat_agent):
    result = chat("/config", "/status", "/quit")
    assert result.exit_code == 0, result.output
    assert "llm_api_key" in result.output and "sk-t…" in result.output
    assert "sk-test" not in result.output and "ghp_test" not in result.output
    assert "no task in this conversation yet" in result.output


def test_a_failed_task_is_marked_as_such(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_FAILED", text="could not clone the repository"))
    result = chat("fix it", "/quit")
    assert result.exit_code == 0, result.output
    assert "✖ The agent could not finish this request." in result.output
    assert "could not clone the repository" in result.output


def test_an_unreachable_agent_shows_in_the_banner_and_the_chat_carries_on(chat_agent, monkeypatch):
    monkeypatch.setattr(cli, "make_client", refusing_client)
    result = chat("hello", "/quit")
    assert result.exit_code == 0, result.output
    assert "Cannot reach agent.test" in result.output
    assert "Run /setup to change the connection." in result.output
    assert "Error: message/send failed after 1 attempt(s)" in result.output
    assert "Traceback" not in result.output


def test_the_first_run_walks_through_setup_and_saves_a_private_file(first_run, tmp_path):
    result = chat(*SETUP_ANSWERS, "/quit")
    assert result.exit_code == 0, result.output
    assert cli.read_config_file(tmp_path / "config.toml") == {
        "ari_agent_url": "http://agent.test",
        "ari_api_key": "agent-key",
        "llm_provider": "anthropic",
        "llm_model": "claude-haiku-4-5",
        "llm_api_key": "sk-new",
        "github_pat": "ghp_new",
        "default_repo_url": "https://github.com/org/repo",
        "default_repo_ref": "dev",
    }
    out = result.output
    assert "Let's connect ari to an ARI agent." in out
    assert "✔ Connected to ARI On-Call Agent." in out
    assert "Saved to" in out
    assert "Repository: org/repo" in out
    assert "Works on branch dev; pull requests target dev" in out
    for secret in ("agent-key", "sk-new", "ghp_new"):
        assert secret not in out
    if os.name != "nt":
        assert (tmp_path / "config.toml").stat().st_mode & 0o077 == 0


def test_setup_can_be_abandoned_without_saving_anything(first_run, tmp_path):
    result = chat("http://agent.test")
    assert result.exit_code == 1
    assert "Setup canceled; nothing was saved." in result.output
    assert not (tmp_path / "config.toml").exists()


def test_setup_saves_an_unreachable_agent_only_when_asked(first_run, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "make_client", refusing_client)
    result = runner.invoke(cli.app, ["setup"], input="".join(f"{a}\n" for a in SETUP_ANSWERS + ["n"]))
    assert result.exit_code == 1
    assert "Error: could not reach the agent" in result.output
    assert "Nothing was saved." in result.output
    assert not (tmp_path / "config.toml").exists()
    result = runner.invoke(cli.app, ["setup"], input="".join(f"{a}\n" for a in SETUP_ANSWERS + ["y"]))
    assert result.exit_code == 0, result.output
    assert cli.read_config_file(tmp_path / "config.toml")["llm_api_key"] == "sk-new"


def test_running_setup_again_keeps_secrets_when_enter_is_pressed(first_run, tmp_path):
    cli.write_config_file(
        tmp_path / "config.toml",
        {
            "ari_agent_url": "http://agent.test",
            "ari_api_key": "old-key",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "llm_api_key": "sk-old",
            "github_pat": "ghp_old",
        },
    )
    result = runner.invoke(cli.app, ["setup"], input="\n\n2\n\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    assert "Enter keeps the current one" in result.output
    assert cli.read_config_file(tmp_path / "config.toml") == {
        "ari_agent_url": "http://agent.test",
        "ari_api_key": "old-key",
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "llm_api_key": "sk-old",
        "github_pat": "ghp_old",
    }
    assert "Type a request" not in result.output
