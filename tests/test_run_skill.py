import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ari_oncall_agent.tools.opencode_cli as opencode_cli  # noqa: E402
from ari_oncall_agent.capabilities import run_skill  # noqa: E402
from common import capabilities_registry, skills, tools_registry  # noqa: E402
from common.capabilities_base import StepContext  # noqa: E402

TRACE = {"elapsed_s": 1.0, "tool_call_counts": {}, "tool_call_total": 0, "slowest_gaps": []}
LLM = {"provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-test"}


def make_skill(tmp_path: Path, name: str = "tls-certificate-check", allowed_tools: tuple[str, ...] = ()) -> skills.Skill:
    path = tmp_path / "installed" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n", encoding="utf-8")
    return skills.Skill(name=name, description="d", path=path, source="bundled", allowed_tools=allowed_tools)


def ctx_for(tmp_path: Path, tool, params: dict | None = None, prompt: str = "is github.com's certificate ok?") -> StepContext:
    return StepContext(
        request_id="req-1",
        workspace=str(tmp_path / "ws"),
        prompt=prompt,
        config={"llm": LLM, "options": {}},
        capability="run_skill",
        params=params or {},
        results={},
        tools={"opencode_skill": tool},
    )


@pytest.fixture
def installed(monkeypatch, tmp_path):
    skill = make_skill(tmp_path)
    monkeypatch.setattr(skills, "available_skills", lambda: {skill.name: skill})
    monkeypatch.setattr(skills, "skill_homes", lambda extra=(): [tmp_path / "empty-home"])
    return skill


@pytest.mark.asyncio
async def test_a_skill_runs_and_its_report_and_commands_are_returned(installed, tmp_path):
    tool = AsyncMock(
        return_value={
            "text": "  github.com: ok, 46 days left  ",
            "tool_calls": [
                {"tool": "skill", "status": "completed", "input": {"name": installed.name}},
                {"tool": "bash", "status": "completed", "input": {"command": "python3 /s/scripts/check_tls.py github.com"}},
                {"tool": "bash", "status": "error", "input": {"command": "rm -rf /"}},
            ],
            "trace": TRACE,
        }
    )
    result = await run_skill.run(ctx_for(tmp_path, tool, {"skill": installed.name, "instruction": "check github.com"}))
    assert result == {
        "skill": "tls-certificate-check",
        "source": "bundled",
        "status": "completed",
        "output": "github.com: ok, 46 days left",
        "commands": [
            {"command": "python3 /s/scripts/check_tls.py github.com", "status": "completed"},
            {"command": "rm -rf /", "status": "error"},
        ],
        "trace": TRACE,
    }
    passed_ctx, passed_skill, instruction = tool.await_args.args
    assert passed_skill is installed
    assert "Use the tls-certificate-check skill" in instruction
    assert "Request: check github.com" in instruction


@pytest.mark.asyncio
async def test_without_an_instruction_the_users_own_words_are_used(installed, tmp_path):
    tool = AsyncMock(return_value={"text": "done", "trace": TRACE})
    await run_skill.run(ctx_for(tmp_path, tool, {"skill": installed.name, "instruction": "  "}))
    assert "Request: is github.com's certificate ok?" in tool.await_args.args[2]


@pytest.mark.asyncio
async def test_an_unknown_or_missing_skill_name_fails_and_lists_what_is_installed(installed, tmp_path):
    tool = AsyncMock()
    with pytest.raises(RuntimeError, match=r"no installed skill is named 'nope'; installed skills: tls-certificate-check"):
        await run_skill.run(ctx_for(tmp_path, tool, {"skill": "nope"}))
    with pytest.raises(RuntimeError, match=r"did not name a skill; installed skills: tls-certificate-check"):
        await run_skill.run(ctx_for(tmp_path, tool, {}))
    tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_skill_that_writes_no_report_still_records_what_it_ran(installed, tmp_path):
    calls = [{"tool": "bash", "status": "completed", "input": {"command": "python3 /s/scripts/x.py --apply"}}]
    tool = AsyncMock(return_value={"text": "   ", "tool_calls": calls, "trace": TRACE})
    result = await run_skill.run(ctx_for(tmp_path, tool, {"skill": installed.name}))
    assert result["status"] == "no_report"
    assert result["output"] == "The skill finished without writing a report."
    assert result["commands"] == [{"command": "python3 /s/scripts/x.py --apply", "status": "completed"}]


@pytest.mark.asyncio
async def test_long_output_and_many_commands_are_capped(installed, tmp_path):
    calls = [{"tool": "bash", "status": "completed", "input": {"command": f"c{i}"}} for i in range(50)]
    tool = AsyncMock(return_value={"text": "x" * 50000, "tool_calls": calls, "trace": TRACE})
    result = await run_skill.run(ctx_for(tmp_path, tool, {"skill": installed.name}))
    assert len(result["output"]) == run_skill.MAX_OUTPUT_CHARS
    assert len(result["commands"]) == run_skill.MAX_COMMANDS


@pytest.mark.asyncio
async def test_the_skill_agent_runs_in_its_own_empty_directory_with_its_own_config(monkeypatch, tmp_path):
    skill = make_skill(tmp_path, allowed_tools=("Bash(dig:*)",))
    fake = AsyncMock(return_value={"text": "ok", "trace": TRACE})
    monkeypatch.setattr(opencode_cli, "_run_opencode", fake)
    ctx = ctx_for(tmp_path, None, {"skill": skill.name})
    await opencode_cli.run_skill_agent(ctx, skill, "do it")
    workdir = Path(ctx.workspace) / "skill-run"
    config_path = Path(ctx.workspace) / ".opencode-state" / "ari-skill.json"
    _, args, kwargs = fake.mock_calls[0]
    assert args == (ctx, "do it", "ari-skill")
    assert kwargs == {
        "continue_session": False,
        "extra_env": {"OPENCODE_CONFIG": str(config_path), "OPENCODE_DISABLE_PROJECT_CONFIG": "1"},
        "workdir": str(workdir),
        "drop_env": opencode_cli.SERVICE_SECRET_ENV,
    }
    assert workdir.is_dir() and list(workdir.iterdir()) == []
    config = json.loads(config_path.read_text())
    assert "mcp" not in config
    assert config["provider"] == {"anthropic": {"options": {"apiKey": "sk-test"}}}
    assert config["tools"] == {"github_*": False, "task": False}
    assert list(config["agent"]) == ["ari-skill"]
    agent = config["agent"]["ari-skill"]
    assert agent["mode"] == "primary"
    assert agent["tools"] == {"task": False}
    permission = agent["permission"]
    for denied in ("edit", "webfetch", "websearch", "glob", "grep", "list"):
        assert permission[denied] == "deny", denied
    assert permission["skill"] == {"*": "deny", "tls-certificate-check": "allow"}
    skill_dir = (Path(ctx.workspace) / ".opencode-state" / "config" / "opencode" / "skills" / skill.name).as_posix()
    assert permission["read"] == {"*": "deny", f"{skill_dir}/*": "allow"}
    assert permission["external_directory"] == {"*": "deny", f"{skill_dir}/*": "allow"}
    bash = permission["bash"]
    assert list(bash)[0] == "*"
    assert bash[f"python3 {skill_dir}/scripts/*"] == "allow"
    assert bash["dig*"] == "allow"
    assert list(bash)[-1] == "*>*"


def test_the_service_secrets_never_reach_a_skill():
    assert {"AGENT_API_KEYS", "TEMPORAL_API_KEY", "TEMPORAL_PAYLOAD_ENCRYPTION_KEY"} <= set(opencode_cli.SERVICE_SECRET_ENV)


@pytest.mark.asyncio
async def test_a_copy_of_the_skill_in_a_home_directory_stops_the_run(installed, tmp_path, monkeypatch):
    home = tmp_path / "home"
    planted = home / ".opencode" / "skills" / installed.name
    planted.mkdir(parents=True)
    (planted / "SKILL.md").write_text(f"---\nname: {installed.name}\ndescription: d\n---\nx\n", encoding="utf-8")
    monkeypatch.setattr(skills, "skill_homes", lambda extra=(): [home])
    tool = AsyncMock()
    with pytest.raises(RuntimeError, match="exists in a home directory on the worker"):
        await run_skill.run(ctx_for(tmp_path, tool, {"skill": installed.name}))
    tool.assert_not_awaited()


def test_the_static_skill_agent_can_do_nothing_until_a_skill_is_chosen():
    permission = opencode_cli._AGENT_DEFS["ari-skill"]["permission"]
    assert permission == {
        "edit": "deny",
        "bash": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "glob": "deny",
        "grep": "deny",
        "list": "deny",
        "skill": "deny",
    }
    assert opencode_cli.CAPABILITY_AGENTS["run_skill"] == "ari-skill"


def test_run_skill_is_registered_for_the_planner_with_its_tool():
    capabilities_registry._load_specs.cache_clear()
    tools_registry._load_specs.cache_clear()
    spec = capabilities_registry.get_capability("run_skill")
    assert spec.tools == ["opencode_skill"]
    assert spec.retry.max_attempts == 1
    assert spec.timeout_seconds == 600
    assert "run_skill" in {c["name"] for c in capabilities_registry.catalog()}
    assert tools_registry.get_tools(["opencode_skill"])["opencode_skill"] is opencode_cli.run_skill_agent
