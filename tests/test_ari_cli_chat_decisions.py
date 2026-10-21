import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ari_cli.main as cli
from tests.ari_cli_fakes import PENDING, PR, _task, proposal

runner = CliRunner()
SRC = Path(__file__).resolve().parents[1] / "src"
BASE_HINT = "If this request leads to a pull request, open it against the staging branch."


def chat(*lines: str):
    return runner.invoke(cli.app, [], input="".join(f"{line}\n" for line in lines))


def flat(result) -> str:
    return " ".join(result.output.split())


def confirms(agent):
    return [s for s in agent.sends if "confirm_pending_action" in s["message"]["parts"][1]["data"]]


def paused_then_done(agent, artifacts=PENDING):
    agent.script(
        "task-1",
        _task("task-1", "TASK_STATE_INPUT_REQUIRED", artifacts, "Ready."),
        _task("task-1", "TASK_STATE_COMPLETED", PR, "Opened."),
    )


def test_typing_yes_with_a_base_set_still_approves(chat_agent):
    paused_then_done(chat_agent)
    result = chat("/base staging", "fix the bug", "5", "yes", "/quit")
    assert result.exit_code == 0, result.output
    assert "✔ Pull request opened" in result.output
    assert len(confirms(chat_agent)) == 1
    assert chat_agent.sends[0]["message"]["parts"][0]["text"] == f"fix the bug\n{BASE_HINT}"


def test_typing_no_with_a_base_set_still_declines(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    result = chat("/base staging", "fix the bug", "5", "no", "/quit")
    assert result.exit_code == 0, result.output
    assert chat_agent.cancels == ["task-1"]
    assert "Declined. The branch ari/fix-it stays on the repository" in result.output
    assert len(chat_agent.sends) == 1


def test_a_base_is_a_condition_so_a_question_stays_a_question(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="ok"))
    result = chat("/base staging", "What is a deadlock?", "/quit")
    assert result.exit_code == 0, result.output
    assert chat_agent.sends[0]["message"]["parts"][0]["text"] == f"What is a deadlock?\n{BASE_HINT}"


def test_status_shows_the_waiting_proposal_and_its_menu(chat_agent):
    paused_then_done(chat_agent)
    result = chat("fix the bug", "5", "/status", "1", "/quit")
    assert result.exit_code == 0, result.output
    assert result.output.count("Proposed change") == 2
    assert result.output.count("What do you want to do?") == 2
    assert "✔ Pull request opened" in result.output


def test_decide_later_and_leaving_name_the_task_so_it_can_be_found_again(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    result = chat("fix the bug", "5", "/new", "/quit")
    assert result.exit_code == 0, result.output
    assert "(task task-1)" in flat(result)
    assert "A proposal is still waiting on task task-1. Approve it later with: ari approve task-1" in flat(result)
    chat_agent.script("task-2", _task("task-2", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    result = chat("fix the bug", "5")
    assert result.exit_code == 0, result.output
    assert "Approve it later with: ari approve task-2" in flat(result)


def test_commands_typed_at_the_follow_up_prompt_are_run_not_sent(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    result = chat("fix the bug", "4", "/quit", "never read")
    assert result.exit_code == 0, result.output
    assert len(chat_agent.sends) == 1
    assert "A proposal is still waiting on task task-1" in result.output


def test_a_pause_without_a_proposal_asks_for_a_reply_and_sends_it(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", text="Which service do you mean?"))
    result = chat("fix it", "yes, the payments one", "/quit")
    assert result.exit_code == 0, result.output
    assert "Which service do you mean?" in result.output
    assert "The agent is waiting for your reply. Type it below." in result.output
    assert "Proposed change" not in result.output
    reply = chat_agent.sends[1]["message"]
    assert reply["task_id"] == "task-1"
    assert reply["parts"][0]["text"] == "yes, the payments one"
    assert confirms(chat_agent) == []


def test_a_bare_yes_answering_a_question_is_sent_as_text(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", text="Shall I look at payments?"))
    result = chat("fix it", "yes", "/quit")
    assert result.exit_code == 0, result.output
    assert chat_agent.sends[1]["message"]["parts"][0]["text"] == "yes"
    assert confirms(chat_agent) == []


def test_approve_and_decline_with_nothing_waiting_are_explained(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_COMPLETED", text="ok"))
    result = chat("/decline", "/approve", "hello", "/decline", "/quit")
    assert result.exit_code == 0, result.output
    assert result.output.count("Nothing is waiting for a decision in this conversation.") == 2
    assert "Nothing is waiting for a decision; the last task is done." in result.output
    assert chat_agent.cancels == []


def test_agent_text_is_shown_literally_not_read_as_markup(chat_agent):
    action = proposal(changed_files=["app/[slug]/page.tsx"], verification={"status": "failed", "detail": "see [/tmp/build.log]"})
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", action, "Ready [bold]now[/bold]."))
    result = chat("fix it", "5", "/quit")
    assert result.exit_code == 0, result.output
    assert "app/[slug]/page.tsx" in result.output
    assert "[/tmp/build.log]" in result.output
    assert "Traceback" not in result.output


def test_a_follow_up_that_fails_still_says_the_old_proposal_is_gone(chat_agent):
    chat_agent.script("task-1", _task("task-1", "TASK_STATE_INPUT_REQUIRED", PENDING, "Ready."))
    chat_agent.after_follow_up["task-1"] = [_task("task-1", "TASK_STATE_FAILED", text="could not clone")]
    result = chat("fix the bug", "4", "also run the tests", "/quit")
    assert result.exit_code == 0, result.output
    assert "✖ The agent could not finish this request." in result.output
    assert "could not clone" in result.output
    assert "The earlier proposal on this task can no longer be approved" in result.output


def test_a_mistyped_agent_url_is_reported_not_a_traceback(chat_agent, monkeypatch):
    monkeypatch.setenv("ARI_AGENT_URL", "http://localhost:80OO")
    result = chat("hello", "/quit")
    assert result.exit_code == 0, result.output
    assert "is not a valid agent URL" in result.output
    assert "Traceback" not in result.output


def test_repo_urls_with_credentials_are_never_echoed(chat_agent):
    result = chat("/repo http://x-access-token:ghp_secret@github.com/org/repo", "/quit")
    assert result.exit_code == 0, result.output
    assert "must not embed credentials" in result.output
    assert "ghp_secret" not in result.output


def test_switching_repositories_keeps_the_configured_default_host_allowed(chat_agent, monkeypatch):
    monkeypatch.setenv("DEFAULT_REPO_URL", "https://git.corp.example/team/svc")
    result = chat("/repo https://github.com/org/other", "/repo https://git.corp.example/team/svc", "/quit")
    assert result.exit_code == 0, result.output
    assert "Repository set to team/svc" in result.output
    assert "not allowed" not in result.output


def setup_input(*answers: str) -> str:
    return "".join(f"{a}\n" for a in answers)


def test_setup_trims_secrets_and_uses_the_environment_url_as_default(first_run, monkeypatch, tmp_path):
    monkeypatch.setenv("ARI_AGENT_URL", "http://agent.test")
    result = runner.invoke(cli.app, ["setup"], input=setup_input("", "agent-key  ", "1", "", " sk-new ", "ghp_new", "", ""))
    assert result.exit_code == 0, result.output
    assert "Agent URL [http://agent.test]" in result.output
    saved = cli.read_config_file(tmp_path / "config.toml")
    assert saved["ari_agent_url"] == "http://agent.test"
    assert saved["ari_api_key"] == "agent-key"
    assert saved["llm_api_key"] == "sk-new"
    assert set(first_run.hosts) == {"agent.test"}


def test_enter_through_setup_keeps_an_openai_configuration(first_run, tmp_path):
    cli.write_config_file(tmp_path / "config.toml", {"ari_agent_url": "http://agent.test", "llm_model": "gpt-4o", "llm_api_key": "sk-openai"})
    result = runner.invoke(cli.app, ["setup"], input=setup_input(*[""] * 8))
    assert result.exit_code == 0, result.output
    assert "2) openai (current)" in result.output
    saved = cli.read_config_file(tmp_path / "config.toml")
    assert saved["llm_provider"] == "openai"
    assert saved["llm_model"] == "gpt-4o"
    assert saved["llm_api_key"] == "sk-openai"


def test_setup_refuses_an_unnamed_or_modelless_other_provider(first_run, tmp_path):
    result = runner.invoke(cli.app, ["setup"], input=setup_input("http://agent.test", "k", "3", "", "", "sk-new", "", "", ""))
    assert result.exit_code == 1
    assert "an LLM provider name is required" in result.output
    result = runner.invoke(cli.app, ["setup"], input=setup_input("http://agent.test", "k", "3", "azure", "", "sk-new", "", "", ""))
    assert result.exit_code == 1
    assert "LLM_MODEL is not set" in result.output
    assert not (tmp_path / "config.toml").exists()


def test_setup_refuses_an_agent_url_without_a_scheme(first_run, tmp_path):
    result = runner.invoke(cli.app, ["setup"], input=setup_input("localhost:8000", "k", "1", "", "sk-new", "", "", ""))
    assert result.exit_code == 1
    assert "the agent URL must start with http:// or https://" in result.output
    assert not (tmp_path / "config.toml").exists()


def test_a_broken_config_file_is_reported_and_setup_replaces_it(first_run, tmp_path):
    (tmp_path / "config.toml").write_text("llm_api_key = sk-unquoted\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 1
    assert "is not valid TOML" in result.output
    assert "sk-unquoted" not in result.output
    result = runner.invoke(cli.app, ["setup"], input=setup_input("http://agent.test", "k", "1", "", "sk-new", "", "", ""))
    assert result.exit_code == 0, result.output
    assert "Starting from scratch" in result.output
    assert cli.read_config_file(tmp_path / "config.toml")["llm_api_key"] == "sk-new"


def test_piped_setup_reads_every_answer_from_stdin_without_hanging(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ARI_", "LLM_", "GITHUB_", "DEFAULT_REPO"))}
    env.update({"PYTHONPATH": str(SRC), "ARI_CONFIG_FILE": str(tmp_path / "config.toml"), "PYTHONDONTWRITEBYTECODE": "1"})
    answers = setup_input("http://127.0.0.1:9", "agent-key", "1", "", "sk-piped", "ghp-piped", "", "", "n")
    done = subprocess.run(
        [sys.executable, "-m", "ari_cli.main", "setup"],
        input=answers,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=90,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "could not reach the agent" in done.stdout
    assert "Nothing was saved." in done.stdout
    assert "sk-piped" not in done.stdout + done.stderr
