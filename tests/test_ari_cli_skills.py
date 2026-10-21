import sys
from pathlib import Path

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ari_cli.main as cli  # noqa: E402

runner = CliRunner()
BUNDLED = Path(__file__).resolve().parents[1] / "skills"


def test_validating_the_bundled_skills_passes():
    result = runner.invoke(cli.app, ["skills", "validate", str(BUNDLED)])
    assert result.exit_code == 0, result.output
    assert "ok  tls-certificate-check" in result.output
    assert "3 valid, 0 invalid" in result.output


def test_validating_one_skill_directory(tmp_path):
    result = runner.invoke(cli.app, ["skills", "validate", str(BUNDLED / "changelog-entry")])
    assert result.exit_code == 0, result.output
    assert "1 valid, 0 invalid" in result.output


def test_problems_are_listed_and_the_exit_code_says_so(tmp_path):
    bad = tmp_path / "My_Skill"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: my-skill\nextra: 1\n---\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["skills", "validate", str(bad), str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "error: 'name' is 'my-skill' but the directory is 'My_Skill'; they must match" in result.output
    assert "error: 'description' is required" in result.output
    assert "warning: fields outside the Agent Skills spec are ignored: extra" in result.output
    assert "is not a directory" in result.output
    assert "0 valid, 2 invalid" in result.output


def test_a_directory_with_no_skills_at_all_is_an_error(tmp_path):
    result = runner.invoke(cli.app, ["skills", "validate", str(tmp_path)])
    assert result.exit_code == 1
    assert "none of the folders directly inside it has one" in " ".join(result.output.split())


def test_validating_the_current_directory_uses_its_real_name(tmp_path, monkeypatch):
    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: d\n---\nbody\n", encoding="utf-8")
    monkeypatch.chdir(skill)
    result = runner.invoke(cli.app, ["skills", "validate", "."])
    assert result.exit_code == 0, result.output
    assert "ok  my-skill" in result.output


def test_a_wrongly_cased_skill_file_is_reported_not_skipped(tmp_path):
    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "Skill.md").write_text("---\nname: my-skill\ndescription: d\n---\nbody\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["skills", "validate", str(skill)])
    assert result.exit_code == 1
    assert "found Skill.md; the file must be named exactly SKILL.md" in result.output
