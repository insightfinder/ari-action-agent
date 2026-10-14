import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ari_oncall_agent.tools.opencode_cli as opencode_cli  # noqa: E402
from common import planner, skills  # noqa: E402
from common.capabilities_base import StepContext  # noqa: E402
from common import opencode_runtime  # noqa: E402

REPO = {"url": "https://github.com/example/no-protected-branches-configured"}
LLM = {"provider": "anthropic", "api_key": "sk-x", "model": "claude-x"}


def ctx(workspace: Path, capability: str = "generate_change") -> StepContext:
    return StepContext(
        request_id="req-1",
        workspace=str(workspace),
        prompt="p",
        config={"llm": LLM},
        capability=capability,
        params={},
        results={},
    )


class FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"", b""


@pytest.fixture
def captured(monkeypatch):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append({"cmd": list(args), "env": kwargs["env"]})
        return FakeProcess()

    monkeypatch.setattr("common.opencode_runtime.asyncio.create_subprocess_exec", fake_exec)
    return calls


def test_every_agent_starts_with_skills_denied():
    for name, definition in opencode_cli._AGENT_DEFS.items():
        assert definition["permission"]["skill"] == "deny", name


def test_write_config_gives_the_repo_agents_our_skills_only(monkeypatch, tmp_path):
    monkeypatch.setattr(skills, "repo_agent_skill_permission", lambda workdir: {"*": "deny", "ours": "allow"})
    (tmp_path / ".git" / "info").mkdir(parents=True)
    config = json.loads(Path(opencode_cli.write_config(str(tmp_path), LLM, REPO)).read_text())
    agents = config["agent"]
    assert agents["ari-change"]["permission"]["skill"] == {"*": "deny", "ours": "allow"}
    assert agents["ari-verify"]["permission"]["skill"] == {"*": "deny", "ours": "allow"}
    assert agents["ari-publish"]["permission"]["skill"] == "deny"
    assert agents["ari-skill"]["permission"]["skill"] == "deny"
    assert agents["ari-change"]["permission"]["skill"] is not agents["ari-verify"]["permission"]["skill"]


def test_write_config_asks_about_the_cloned_repository_itself(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(skills, "repo_agent_skill_permission", lambda workdir: seen.append(workdir) or "deny")
    opencode_cli.write_config(str(tmp_path), LLM, REPO)
    assert seen == [str(tmp_path)]


@pytest.mark.asyncio
async def test_run_opencode_materialises_skills_into_its_own_config_home(monkeypatch, tmp_path, captured):
    prepared = []
    monkeypatch.setattr(skills, "prepare", lambda home: prepared.append(home) or [])
    await opencode_runtime.run_opencode(ctx(tmp_path), "do it", "ari-change")
    home = opencode_runtime.opencode_config_home(str(tmp_path))
    assert prepared == [home]
    assert captured[0]["env"]["XDG_CONFIG_HOME"] == home


@pytest.mark.asyncio
async def test_run_opencode_uses_the_repo_unless_given_another_directory(monkeypatch, tmp_path, captured):
    monkeypatch.setattr(skills, "prepare", lambda home: [])
    await opencode_runtime.run_opencode(ctx(tmp_path), "a", "ari-change")
    await opencode_runtime.run_opencode(ctx(tmp_path), "b", "ari-skill", workdir=str(tmp_path / "skill-run"))
    first, second = (call["cmd"] for call in captured)
    assert first[first.index("--dir") + 1] == str(tmp_path)
    assert second[second.index("--dir") + 1] == str(tmp_path / "skill-run")


@pytest.mark.asyncio
async def test_the_real_bundled_skills_reach_the_config_home(monkeypatch, tmp_path, captured):
    await opencode_runtime.run_opencode(ctx(tmp_path), "a", "ari-change")
    installed = Path(opencode_runtime.opencode_config_home(str(tmp_path))) / "opencode" / "skills"
    for name in ("tls-certificate-check", "aws-security-group-allowlist", "changelog-entry"):
        assert (installed / name / "SKILL.md").is_file(), name
    assert (installed / "tls-certificate-check" / "scripts" / "check_tls.py").is_file()


@pytest.mark.asyncio
async def test_the_planner_is_shown_the_installed_skills(monkeypatch):
    complete = AsyncMock(return_value='{"steps": [], "rationale": "x"}')
    monkeypatch.setattr(planner, "complete", complete)
    monkeypatch.setattr(planner, "planner_view", lambda: [{"name": "tls-certificate-check", "description": "d"}])
    await planner.create_plan("check github.com", {"llm": LLM})
    request = json.loads(complete.await_args.args[2])
    assert request["available_skills"] == [{"name": "tls-certificate-check", "description": "d"}]
    assert '"run_skill"' in complete.await_args.args[1]


@pytest.mark.asyncio
async def test_the_planner_request_is_unchanged_when_no_skill_is_installed(monkeypatch):
    complete = AsyncMock(return_value='{"steps": [], "rationale": "x"}')
    monkeypatch.setattr(planner, "complete", complete)
    monkeypatch.setattr(planner, "planner_view", lambda: [])
    await planner.create_plan("hello", {"llm": LLM})
    assert set(json.loads(complete.await_args.args[2])) == {"prompt", "history", "options"}


@pytest.mark.asyncio
async def test_external_skill_folders_are_off_for_every_opencode_run_by_default(monkeypatch, tmp_path, captured):
    monkeypatch.setattr(skills, "prepare", lambda home: [])
    monkeypatch.setattr(skills, "opencode_env", lambda: {"OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"})
    await opencode_runtime.run_opencode(ctx(tmp_path), "a", "ari-change", extra_env={"HOME": "/cache"})
    env = captured[0]["env"]
    assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "1"
    assert env["HOME"] == "/cache"


@pytest.mark.asyncio
async def test_named_variables_can_be_kept_away_from_opencode(monkeypatch, tmp_path, captured):
    monkeypatch.setattr(skills, "prepare", lambda home: [])
    monkeypatch.setenv("AGENT_API_KEYS", "secret-key")
    monkeypatch.setenv("UNRELATED", "kept")
    await opencode_runtime.run_opencode(ctx(tmp_path), "a", "ari-skill", drop_env=("AGENT_API_KEYS", "NOT_SET"))
    env = captured[0]["env"]
    assert "AGENT_API_KEYS" not in env
    assert env["UNRELATED"] == "kept"
