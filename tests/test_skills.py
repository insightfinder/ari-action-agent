import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common import skills
from common.settings import Settings


def write_skill(root: Path, directory: str, frontmatter: str, body: str = "Do the thing.\n") -> Path:
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


def simple(root: Path, name: str, description: str = "Does a thing. Use when asked.") -> Path:
    return write_skill(root, name, f"name: {name}\ndescription: {description}")


@pytest.fixture
def configure(monkeypatch, tmp_path):
    def apply(**values):
        settings = Settings(
            **{
                "bundled_skills_dir": str(tmp_path / "bundled"),
                "skills_dir": str(tmp_path / "deployment"),
                **values,
            }
        )
        monkeypatch.setattr(skills, "get_settings", lambda: settings)
        return settings

    return apply


def resolve(rules: dict[str, str], command: str) -> str:
    action = "ask"
    for pattern, value in rules.items():
        regex = "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        if re.match(regex, command, re.DOTALL):
            action = value
    return action


def test_a_complete_skill_is_read_with_every_spec_field(tmp_path):
    path = write_skill(
        tmp_path,
        "pdf-tools",
        "name: pdf-tools\n"
        "description: Extracts text from PDF files. Use when a PDF is mentioned.\n"
        "license: Apache-2.0\n"
        "compatibility: Needs python3.\n"
        "metadata:\n  author: someone\n  version: '1.0'\n"
        "allowed-tools: Bash(git status:*) Bash(jq:*) Read",
        body="# PDF\nRun scripts/extract.py.\n",
    )
    report = skills.inspect_skill(path, "deployment")
    assert report.ok and report.warnings == []
    skill = report.skill
    assert skill.name == "pdf-tools"
    assert skill.source == "deployment"
    assert skill.license == "Apache-2.0"
    assert skill.compatibility == "Needs python3."
    assert skill.metadata == {"author": "someone", "version": "1.0"}
    assert skill.allowed_tools == ("Bash(git status:*)", "Bash(jq:*)", "Read")
    assert skill.body.startswith("# PDF")


@pytest.mark.parametrize(
    "name, message",
    [
        ("PDF-Tools", "lowercase"),
        ("-pdf", "must not start or end with a hyphen"),
        ("pdf-", "must not start or end with a hyphen"),
        ("pdf--tools", "single hyphens"),
        ("pdf_tools", "lowercase letters, digits and single hyphens"),
        ("a" * 65, "1 to 64 characters"),
    ],
)
def test_names_outside_the_spec_are_rejected(tmp_path, name, message):
    path = write_skill(tmp_path, name, f"name: {name}\ndescription: ok")
    report = skills.inspect_skill(path)
    assert not report.ok
    assert any(message in error for error in report.errors), report.errors


def test_the_name_must_match_the_directory(tmp_path):
    path = write_skill(tmp_path, "folder", "name: other\ndescription: ok")
    report = skills.inspect_skill(path)
    assert report.errors == ["'name' is 'other' but the directory is 'folder'; they must match"]


@pytest.mark.parametrize(
    "frontmatter, message",
    [
        ("name: x", "'description' is required"),
        ("name: x\ndescription: '   '", "'description' must be non-empty text"),
        ("name: x\ndescription: " + "d" * 1025, "at most 1024 characters"),
        ("name: x\ndescription: ok\ncompatibility: " + "c" * 501, "'compatibility' must be at most 500"),
        ("name: x\ndescription: ok\nmetadata:\n  version: 1", "'metadata' must map text keys to text values"),
        ("name: x\ndescription: ok\nallowed-tools:\n  - Read", "'allowed-tools' must be one space-separated string"),
        ("description: ok", "'name' is required"),
    ],
)
def test_field_rules(tmp_path, frontmatter, message):
    path = write_skill(tmp_path, "x", frontmatter)
    report = skills.inspect_skill(path)
    assert any(message in error for error in report.errors), report.errors


@pytest.mark.parametrize(
    "content, message",
    [
        ("no frontmatter here\n", "must start with a '---' line"),
        ("---\nname: x\ndescription: ok\n", "never closed"),
        ("---\nname: [unclosed\n---\nbody\n", "not valid YAML"),
        ("---\n- a list\n---\nbody\n", "must be a YAML mapping"),
    ],
)
def test_broken_frontmatter_is_explained(tmp_path, content, message):
    path = tmp_path / "x"
    path.mkdir()
    (path / "SKILL.md").write_text(content, encoding="utf-8")
    report = skills.inspect_skill(path)
    assert len(report.errors) == 1 and message in report.errors[0], report.errors


def test_windows_line_endings_and_a_byte_order_mark_are_accepted(tmp_path):
    path = tmp_path / "x"
    path.mkdir()
    (path / "SKILL.md").write_bytes("\ufeff---\r\nname: x\r\ndescription: ok\r\n---\r\nbody\r\n".encode("utf-8"))
    assert skills.inspect_skill(path).ok


def test_a_closing_line_must_be_exactly_three_dashes(tmp_path):
    path = write_skill(tmp_path, "x", "name: x\ndescription: 'a --- b'\nlicense: MIT", body="----\nbody\n")
    report = skills.inspect_skill(path)
    assert report.ok, report.errors
    assert report.skill.license == "MIT"


def test_unknown_fields_and_an_empty_body_are_warnings_not_errors(tmp_path):
    path = write_skill(tmp_path, "x", "name: x\ndescription: ok\nmodel: opus\nuser-invocable: true", body="")
    report = skills.inspect_skill(path)
    assert report.ok
    assert report.warnings == [
        "fields outside the Agent Skills spec are ignored: model, user-invocable",
        "the body after the frontmatter is empty, so the skill gives the model no instructions",
    ]


def test_a_wrongly_cased_skill_file_is_named_in_the_error(tmp_path):
    path = tmp_path / "x"
    path.mkdir()
    (path / "skill.md").write_text("---\nname: x\ndescription: ok\n---\nbody\n", encoding="utf-8")
    assert skills.inspect_skill(path).errors == ["found skill.md; the file must be named exactly SKILL.md"]


def test_load_skill_raises_with_every_problem(tmp_path):
    path = write_skill(tmp_path, "x", "name: Y")
    with pytest.raises(skills.SkillError) as caught:
        skills.load_skill(path)
    assert "'description' is required" in str(caught.value)
    assert "'name' is 'Y' but the directory is 'x'" in str(caught.value)


def test_discovery_lets_a_later_source_replace_a_skill_and_reports_broken_ones(tmp_path):
    simple(tmp_path / "bundled", "shared", "bundled version")
    simple(tmp_path / "bundled", "only-bundled")
    simple(tmp_path / "deployment", "shared", "deployment version")
    write_skill(tmp_path / "deployment", "broken", "name: broken")
    (tmp_path / "deployment" / "notes").mkdir()
    simple(tmp_path / "deployment" / "..data", "hidden")
    found, rejected = skills.discover([("bundled", tmp_path / "bundled"), ("deployment", tmp_path / "deployment")])
    assert sorted(found) == ["only-bundled", "shared"]
    assert found["shared"].description == "deployment version"
    assert found["shared"].source == "deployment"
    assert [r.path.name for r in rejected] == ["broken"]


def test_a_missing_source_directory_is_simply_empty(tmp_path):
    assert skills.discover([("deployment", tmp_path / "absent")]) == ({}, [])


def test_available_skills_honours_the_kill_switch(configure, tmp_path):
    simple(tmp_path / "bundled", "one")
    configure()
    assert list(skills.available_skills()) == ["one"]
    configure(skills_enabled=False)
    assert skills.available_skills() == {}


def test_planner_view_is_name_and_description_sorted_by_name(configure, tmp_path):
    simple(tmp_path / "bundled", "zeta", "Z.")
    simple(tmp_path / "deployment", "alpha", "A.")
    configure()
    assert skills.planner_view() == [{"name": "alpha", "description": "A."}, {"name": "zeta", "description": "Z."}]


def test_bundled_skills_default_to_the_repository_skills_directory(monkeypatch):
    monkeypatch.setattr(skills, "get_settings", lambda: Settings(bundled_skills_dir=""))
    assert skills.bundled_skills_dir() == Path(__file__).resolve().parents[1] / "skills"


def test_materialize_copies_each_skill_once_and_leaves_build_debris_behind(tmp_path):
    source = simple(tmp_path / "src", "tool")
    (source / "scripts").mkdir()
    (source / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "x.pyc").write_bytes(b"\0")
    found, _ = skills.discover([("bundled", tmp_path / "src")])
    destination = tmp_path / "config" / "opencode" / "skills"
    assert skills.materialize(found, destination) == ["tool"]
    assert (destination / "tool" / "SKILL.md").is_file()
    assert (destination / "tool" / "scripts" / "run.py").is_file()
    assert not (destination / "tool" / "__pycache__").exists()
    (source / "SKILL.md").write_text("---\nname: tool\ndescription: changed\n---\nnew\n", encoding="utf-8")
    assert skills.materialize(found, destination) == ["tool"]
    assert "changed" not in (destination / "tool" / "SKILL.md").read_text(encoding="utf-8")


def test_prepare_puts_skills_where_opencode_reads_global_skills(configure, tmp_path):
    simple(tmp_path / "bundled", "one")
    configure()
    config_home = tmp_path / "ws" / ".opencode-state" / "config"
    assert skills.prepare(config_home) == ["one"]
    assert (config_home / "opencode" / "skills" / "one" / "SKILL.md").is_file()


def test_prepare_with_no_skills_creates_nothing(configure, tmp_path):
    configure()
    config_home = tmp_path / "ws" / "config"
    assert skills.prepare(config_home) == []
    assert not config_home.exists()


def test_prepare_survives_a_copy_failure(configure, tmp_path, monkeypatch):
    simple(tmp_path / "bundled", "one")
    configure()

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(skills, "materialize", broken)
    assert skills.prepare(tmp_path / "config") == []


def test_repo_skill_names_cover_every_place_opencode_looks_in_a_repository(tmp_path):
    repo = tmp_path / "repo"
    simple(repo / ".opencode" / "skill", "a")
    simple(repo / ".opencode" / "skills" / "group", "b")
    simple(repo / ".claude" / "skills", "c")
    write_skill(repo / ".agents" / "skills", "folder", "name: declared\ndescription: ok")
    broken = repo / ".agents" / "skills" / "unreadable"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    simple(repo / "docs", "not-a-skill-location")
    assert skills.repo_skill_names(repo) == {"a", "b", "c", "declared", "folder", "unreadable"}


def test_skill_permission_denies_everything_else_first(tmp_path):
    assert skills.skill_permission([]) == "deny"
    rules = skills.skill_permission(["b", "a", "a"])
    assert list(rules.items()) == [("*", "deny"), ("a", "allow"), ("b", "allow")]
    assert resolve(rules, "a") == "allow"
    assert resolve(rules, "customize-opencode") == "deny"


def test_repo_agents_get_our_skills_minus_any_name_the_repository_also_defines(configure, tmp_path):
    simple(tmp_path / "bundled", "ours")
    simple(tmp_path / "bundled", "contested")
    repo = tmp_path / "repo"
    simple(repo / ".claude" / "skills", "contested")
    simple(repo / ".claude" / "skills", "theirs")
    configure()
    rules = skills.repo_agent_skill_permission(repo)
    assert rules == {"*": "deny", "ours": "allow"}
    assert resolve(rules, "theirs") == "deny"
    assert resolve(rules, "contested") == "deny"


def test_repo_skills_can_be_trusted_and_skills_switched_off(configure, tmp_path):
    simple(tmp_path / "bundled", "ours")
    simple(tmp_path / "bundled", "contested")
    simple(tmp_path / ".claude" / "skills", "contested")
    configure(trust_repo_skills=True)
    rules = skills.repo_agent_skill_permission(tmp_path)
    assert rules == {"*": "allow", "contested": "deny", "customize-opencode": "deny"}
    assert resolve(rules, "repo-own-skill") == "allow"
    assert resolve(rules, "ours") == "allow"
    assert resolve(rules, "contested") == "deny"
    configure(skills_enabled=False, trust_repo_skills=True)
    assert skills.repo_agent_skill_permission(tmp_path) == "deny"
    configure()
    (tmp_path / "bundled" / "ours" / "SKILL.md").unlink()
    assert skills.repo_agent_skill_permission(tmp_path) == "deny"


def test_bash_rules_allow_only_the_skills_own_scripts():
    root = "/tmp/ws/.opencode-state/config/opencode/skills/tool"
    rules = skills.bash_rules_for(root, ("Bash(git status:*)", "Bash(jq)", "Bash(*)", "Read"))
    keys = list(rules)
    assert keys[0] == "*" and rules["*"] == "deny"
    assert keys[-4:] == ["*/../*", "*$*", "*`*", "*>*"]
    assert resolve(rules, f"python3 {root}/scripts/check.py example.com") == "allow"
    assert resolve(rules, f"{root}/scripts/check.sh") == "allow"
    assert resolve(rules, "git status --short") == "allow"
    assert resolve(rules, "jq") == "allow"
    assert resolve(rules, "jq .a file.json") == "deny"
    assert resolve(rules, "rm -rf /") == "deny"
    assert resolve(rules, "python3 -c 'import os'") == "deny"
    assert resolve(rules, f"python3 {root}/SKILL.md") == "deny"
    assert resolve(rules, f"python3 {root}/scripts/../../other/x.py") == "deny"
    assert resolve(rules, f"python3 {root}/scripts/check.py $(id)") == "deny"
    assert resolve(rules, f"python3 {root}/scripts/check.py $TEMPORAL_API_KEY") == "deny"
    assert resolve(rules, f"python3 {root}/scripts/check.py ${{HOME}}") == "deny"
    assert resolve(rules, f"python3 {root}/scripts/check.py `id`") == "deny"
    assert resolve(rules, f"python3 {root}/scripts/check.py > /app/src/x.py") == "deny"
    assert resolve(rules, f"python3 {root}/scripts/check.py 2>&1") == "deny"


def test_allowed_tools_entries_that_are_not_bash_prefixes_grant_nothing():
    assert skills.bash_patterns_from_allowed_tools(["Read", "Bash(*)", "Bash()", "Bash(:*)", "Write(x)"]) == []
    assert skills.bash_patterns_from_allowed_tools([" Bash(npm run lint:*) "]) == ["npm run lint*"]


@pytest.mark.parametrize("word", ["yes", "no", "on", "off", "y", "n"])
def test_yaml_1_1_booleans_stay_text_like_opencode_reads_them(tmp_path, word):
    path = write_skill(tmp_path, word, f"name: {word}\ndescription: ok\nmetadata:\n  enabled: {word}")
    report = skills.inspect_skill(path)
    assert report.ok, report.errors
    assert report.skill.name == word
    assert report.skill.metadata == {"enabled": word}


@pytest.mark.parametrize(
    "frontmatter, message",
    [
        ("name: x\ndescription: ok\nname: y", "repeats the field 'name'"),
        ("name: x\ndescription: ok\nmetadata:\n  a: '1'\n  a: '2'", "repeats the field 'a'"),
        ("name: x\ndescription: ok\nlicense: 2026-13-45", "a value YAML cannot read"),
        ("name: x\ndescription: ok\nlicense: !!int nope", "a value YAML cannot read"),
        ("name: x\ndescription: ok\n? [a, b]\n: c", "cannot read"),
    ],
)
def test_frontmatter_that_breaks_the_yaml_constructor_is_reported_not_raised(tmp_path, frontmatter, message):
    path = write_skill(tmp_path, "x", frontmatter)
    report = skills.inspect_skill(path)
    assert not report.ok
    assert any(message in error for error in report.errors), report.errors


def test_one_broken_skill_never_takes_discovery_down(configure, tmp_path):
    simple(tmp_path / "bundled", "good")
    write_skill(tmp_path / "bundled", "bad", "name: bad\ndescription: ok\nlicense: 2026-13-45")
    configure()
    assert list(skills.available_skills()) == ["good"]


def test_available_skills_is_empty_rather_than_failing(configure, monkeypatch):
    configure()

    def broken(sources):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(skills, "discover", broken)
    assert skills.available_skills() == {}


def test_delimiters_with_trailing_spaces_are_accepted(tmp_path):
    path = tmp_path / "x"
    path.mkdir()
    (path / "SKILL.md").write_text("---  \nname: x\ndescription: ok\n--- \nbody\n", encoding="utf-8")
    assert skills.inspect_skill(path).ok


def test_an_oversized_skill_file_is_refused_without_parsing(tmp_path):
    path = write_skill(tmp_path, "x", "name: x\ndescription: ok", body="a" * (skills.MAX_SKILL_FILE_BYTES + 1))
    assert skills.inspect_skill(path).errors == [f"SKILL.md is larger than {skills.MAX_SKILL_FILE_BYTES // 1024} KiB"]


def test_the_name_check_uses_the_real_directory_name(tmp_path, monkeypatch):
    path = simple(tmp_path, "real-name")
    monkeypatch.chdir(path)
    assert skills.inspect_skill(".").ok
    assert skills.inspect_skill(path / "scripts" / "..").ok if (path / "scripts").mkdir() is None else False


def test_an_unreadable_skills_directory_is_skipped(tmp_path, monkeypatch):
    root = tmp_path / "deployment"
    simple(root, "one")
    original = Path.iterdir

    def failing(self):
        if self == root:
            raise PermissionError(13, "Permission denied")
        return original(self)

    monkeypatch.setattr(Path, "iterdir", failing)
    assert skills.scan(root, "deployment") == []


def test_foreign_names_are_read_leniently_and_include_the_folder(tmp_path):
    repo = tmp_path / "repo"
    folder = repo / ".opencode" / "skills" / "folder"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: 'quoted-name'  # comment\ndescription: has: colons: that strict YAML rejects\n---\nbody\n",
        encoding="utf-8",
    )
    assert skills.foreign_skill_names(repo) == {"folder", "quoted-name"}


def test_foreign_scan_reads_only_the_head_of_a_huge_file(tmp_path):
    folder = tmp_path / "repo" / ".agents" / "skills" / "big"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: early\n---\n" + "x" * skills.FOREIGN_SCAN_BYTES + "\nname: late\n", encoding="utf-8")
    assert skills.foreign_skill_names(tmp_path / "repo") == {"big", "early"}


def test_foreign_scan_follows_directory_symlinks_without_looping(tmp_path):
    target = tmp_path / "elsewhere" / "linked"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: linked\n---\n", encoding="utf-8")
    skills_dir = tmp_path / "repo" / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    try:
        (skills_dir / "via-link").symlink_to(target, target_is_directory=True)
        (target / "loop").symlink_to(skills_dir, target_is_directory=True)
    except OSError:
        pytest.skip("this machine cannot create symlinks")
    assert skills.foreign_skill_names(tmp_path / "repo") == {"via-link", "linked"}


def test_home_directories_are_scanned_for_copies_of_our_skills(configure, tmp_path):
    simple(tmp_path / "bundled", "ours")
    simple(tmp_path / "bundled", "planted")
    home = tmp_path / "build-cache"
    simple(home / ".opencode" / "skills", "planted")
    configure(build_cache_root=str(home))
    assert skills.repo_agent_skill_permission(tmp_path / "repo") == {"*": "deny", "ours": "allow"}


def test_skill_homes_include_the_build_cache_and_extras_once(configure, tmp_path):
    configure(build_cache_root=str(tmp_path / "cache"))
    homes = skills.skill_homes([tmp_path / "cache", tmp_path / "other", ""])
    assert homes == [Path.home(), tmp_path / "cache", tmp_path / "other"]


def test_external_skill_folders_are_switched_off_unless_repository_skills_are_trusted(configure):
    configure()
    assert skills.opencode_env() == {"OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"}
    configure(trust_repo_skills=True)
    assert skills.opencode_env() == {}
    configure(trust_repo_skills=True, skills_enabled=False)
    assert skills.opencode_env() == {"OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"}


def test_flow_style_frontmatter_names_are_found_too(tmp_path):
    folder = tmp_path / "repo" / ".opencode" / "skills" / "folder"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text('---\n{"name": flow-name, description: d}\n---\n', encoding="utf-8")
    assert skills.foreign_skill_names(tmp_path / "repo") == {"folder", "flow-name"}


def test_a_field_that_merely_ends_in_name_is_not_a_name(tmp_path):
    folder = tmp_path / "repo" / ".opencode" / "skills" / "folder"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nusername: alice\nfile-name: x\nname: real\n---\n", encoding="utf-8")
    assert skills.foreign_skill_names(tmp_path / "repo") == {"folder", "real"}
