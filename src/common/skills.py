import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import structlog
import yaml

from common.settings import get_settings

logger = structlog.get_logger()

SKILL_FILE = "SKILL.md"
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_COMPATIBILITY_LENGTH = 500
SPEC_FIELDS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")
REPO_SKILL_DIRS = (".opencode/skill", ".opencode/skills", ".claude/skills", ".agents/skills")
MATERIALIZED_MARKER = ".ari-skills"
MAX_SKILL_FILE_BYTES = 256 * 1024
FOREIGN_SCAN_BYTES = 64 * 1024
FOREIGN_SCAN_MAX_DEPTH = 8
FOREIGN_SCAN_MAX_FILES = 2000
HOME_SKILL_DIRS = (".opencode/skill", ".opencode/skills", ".claude/skills", ".agents/skills")
DECLARED_NAME = re.compile(r"(?<![\w-])['\"]?name['\"]?\s*:\s*([^\n,}]+)")
BUNDLED_SOURCE = "bundled"
DEPLOYMENT_SOURCE = "deployment"
BASH_TOOL_PATTERN = re.compile(r"^Bash\((?P<rule>[^()]+)\)$")
UNSAFE_BASH_PATTERNS = ("*/../*", "*$*", "*`*", "*>*")
OPENCODE_BUILTIN_SKILLS = frozenset({"customize-opencode"})


class SkillError(ValueError):
    pass


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    source: str = "local"
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    body: str = ""


@dataclass
class Report:
    path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skill: Skill | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


class _FrontmatterLoader(yaml.SafeLoader):
    pass


_FrontmatterLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_FrontmatterLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise SkillError(f"the frontmatter repeats the field {key!r}")
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_FrontmatterLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _delimiter(line: str) -> bool:
    return line.rstrip() == "---"


def split_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").split("\n")
    if not lines or not _delimiter(lines[0]):
        raise SkillError("SKILL.md must start with a '---' line that opens the YAML frontmatter")
    closing = next((i for i in range(1, len(lines)) if _delimiter(lines[i])), None)
    if closing is None:
        raise SkillError("the YAML frontmatter is never closed with a '---' line")
    block = "\n".join(lines[1:closing])
    try:
        data = yaml.load(block, Loader=_FrontmatterLoader) if block.strip() else {}
    except SkillError:
        raise
    except yaml.YAMLError as exc:
        raise SkillError(f"the frontmatter is not valid YAML: {_first_line(exc)}") from None
    except Exception as exc:
        raise SkillError(f"the frontmatter has a value YAML cannot read: {_first_line(exc)}") from None
    if not isinstance(data, dict):
        raise SkillError("the frontmatter must be a YAML mapping of fields")
    return data, "\n".join(lines[closing + 1 :]).lstrip("\n")


def inspect_skill(directory: str | os.PathLike, source: str = "local") -> Report:
    path = Path(directory)
    report = Report(path=path)
    skill_file = path / SKILL_FILE
    try:
        if not path.is_dir():
            report.errors.append(f"{path} is not a directory")
            return report
        entries = {child.name for child in path.iterdir()}
        if SKILL_FILE not in entries or not skill_file.is_file():
            near = sorted(name for name in entries if name.lower() == SKILL_FILE.lower() and name != SKILL_FILE)
            if near:
                report.errors.append(f"found {near[0]}; the file must be named exactly {SKILL_FILE}")
            else:
                report.errors.append(f"{SKILL_FILE} is missing")
            return report
        if skill_file.stat().st_size > MAX_SKILL_FILE_BYTES:
            report.errors.append(f"{SKILL_FILE} is larger than {MAX_SKILL_FILE_BYTES // 1024} KiB")
            return report
        data, body = split_frontmatter(skill_file.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        report.errors.append(f"{SKILL_FILE} is not UTF-8 text")
        return report
    except OSError as exc:
        report.errors.append(f"cannot read the skill: {exc.strerror or exc}")
        return report
    except SkillError as exc:
        report.errors.append(str(exc))
        return report
    name = _check_name(data.get("name"), Path(os.path.abspath(path)).name, report.errors)
    description = _check_description(data.get("description"), report.errors)
    license_text = _optional_text(data, "license", None, report.errors)
    compatibility = _optional_text(data, "compatibility", MAX_COMPATIBILITY_LENGTH, report.errors)
    metadata = _check_metadata(data.get("metadata"), report.errors)
    allowed_tools = _check_allowed_tools(data.get("allowed-tools"), report.errors)
    unknown = sorted(str(key) for key in data if key not in SPEC_FIELDS)
    if unknown:
        report.warnings.append(f"fields outside the Agent Skills spec are ignored: {', '.join(unknown)}")
    if not body.strip():
        report.warnings.append("the body after the frontmatter is empty, so the skill gives the model no instructions")
    if not report.errors:
        report.skill = Skill(
            name=name,
            description=description,
            path=path,
            source=source,
            license=license_text,
            compatibility=compatibility,
            metadata=metadata,
            allowed_tools=allowed_tools,
            body=body,
        )
    return report


def load_skill(directory: str | os.PathLike, source: str = "local") -> Skill:
    report = inspect_skill(directory, source)
    if report.skill is None:
        raise SkillError(f"{report.path}: " + "; ".join(report.errors))
    return report.skill


def has_skill_file(directory: Path) -> bool:
    try:
        return any(entry.name.lower() == SKILL_FILE.lower() for entry in directory.iterdir())
    except OSError:
        return False


def scan(root: str | os.PathLike, source: str) -> list[Report]:
    base = Path(root)
    try:
        children = sorted(base.iterdir(), key=lambda p: p.name) if base.is_dir() else []
    except OSError as exc:
        logger.warning("skills_dir_unreadable", path=str(base), error=str(exc))
        return []
    reports = []
    for child in children:
        try:
            if child.name.startswith(".") or not child.is_dir():
                continue
        except OSError:
            continue
        if has_skill_file(child):
            reports.append(inspect_skill(child, source))
    return reports


def discover(sources: Iterable[tuple[str, str | os.PathLike]]) -> tuple[dict[str, Skill], list[Report]]:
    found: dict[str, Skill] = {}
    rejected: list[Report] = []
    for source, root in sources:
        for report in scan(root, source):
            if report.skill is None:
                rejected.append(report)
            else:
                found[report.skill.name] = report.skill
    return found, rejected


def bundled_skills_dir() -> Path | None:
    configured = get_settings().bundled_skills_dir
    if configured:
        return Path(configured)
    for candidate in (
        Path(__file__).resolve().parents[2] / "skills",
        Path.cwd() / "skills",
        Path("/app/skills"),
    ):
        if candidate.is_dir():
            return candidate
    return None


def skill_sources() -> list[tuple[str, Path]]:
    settings = get_settings()
    sources = []
    bundled = bundled_skills_dir()
    if bundled is not None:
        sources.append((BUNDLED_SOURCE, bundled))
    if settings.skills_dir:
        sources.append((DEPLOYMENT_SOURCE, Path(settings.skills_dir)))
    return sources


def available_skills() -> dict[str, Skill]:
    if not get_settings().skills_enabled:
        return {}
    try:
        found, rejected = discover(skill_sources())
    except Exception as exc:
        logger.warning("skills_unavailable", error=f"{exc.__class__.__name__}: {exc}")
        return {}
    for report in rejected:
        logger.warning("skill_rejected", path=str(report.path), errors=report.errors)
    return found


def planner_view(skills: dict[str, Skill] | None = None) -> list[dict[str, str]]:
    current = available_skills() if skills is None else skills
    return [{"name": s.name, "description": s.description} for s in sorted(current.values(), key=lambda s: s.name)]


def materialize(skills: dict[str, Skill], destination: str | os.PathLike) -> list[str]:
    target = Path(destination)
    marker = target / MATERIALIZED_MARKER
    if marker.exists():
        return marker.read_text(encoding="utf-8").split()
    target.mkdir(parents=True, exist_ok=True)
    names = sorted(skills)
    for name in names:
        shutil.copytree(
            skills[name].path,
            target / name,
            ignore=shutil.ignore_patterns("..*", "__pycache__", "*.pyc"),
            dirs_exist_ok=True,
        )
    marker.write_text("\n".join(names), encoding="utf-8")
    return names


def materialized_dir(config_home: str | os.PathLike) -> Path:
    return Path(config_home) / "opencode" / "skills"


def prepare(config_home: str | os.PathLike) -> list[str]:
    skills = available_skills()
    if not skills:
        return []
    try:
        return materialize(skills, materialized_dir(config_home))
    except OSError as exc:
        logger.warning("skills_not_materialized", error=str(exc))
        return []


def skill_homes(extra: Iterable[str | os.PathLike] = ()) -> list[Path]:
    homes = [Path.home(), Path(get_settings().build_cache_root), *(Path(h) for h in extra if h)]
    unique: list[Path] = []
    for home in homes:
        if home not in unique:
            unique.append(home)
    return unique


def foreign_skill_names(
    repo_dir: str | os.PathLike | None = None, homes: Iterable[str | os.PathLike] = ()
) -> set[str]:
    roots = [Path(repo_dir) / relative for relative in REPO_SKILL_DIRS] if repo_dir else []
    roots += [Path(home) / relative for home in homes for relative in HOME_SKILL_DIRS]
    budget = [FOREIGN_SCAN_MAX_FILES]
    names: set[str] = set()
    for root in roots:
        names |= _scan_foreign(root, budget)
    return names


def repo_skill_names(repo_dir: str | os.PathLike) -> set[str]:
    return foreign_skill_names(repo_dir)


def skill_permission(names: Iterable[str]) -> str | dict[str, str]:
    allowed = sorted(set(names))
    if not allowed:
        return "deny"
    return {"*": "deny", **{name: "allow" for name in allowed}}


def repo_agent_skill_permission(
    repo_dir: str | os.PathLike, homes: Iterable[str | os.PathLike] = ()
) -> str | dict[str, str]:
    settings = get_settings()
    if not settings.skills_enabled:
        return "deny"
    ours = set(available_skills())
    shadowed = ours & foreign_skill_names(repo_dir, skill_homes(homes))
    if shadowed:
        logger.warning("skills_shadowed", names=sorted(shadowed), repo_dir=str(repo_dir))
    if settings.trust_repo_skills:
        return {"*": "allow", **{name: "deny" for name in sorted(OPENCODE_BUILTIN_SKILLS | shadowed)}}
    return skill_permission(ours - shadowed)


def opencode_env() -> dict[str, str]:
    settings = get_settings()
    if settings.skills_enabled and settings.trust_repo_skills:
        return {}
    return {"OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"}


def bash_rules_for(skill_dir: str | os.PathLike, allowed_tools: Iterable[str] = ()) -> dict[str, str]:
    scripts = f"{Path(skill_dir).as_posix().rstrip('/')}/scripts/"
    rules = {"*": "deny"}
    for runner in ("python3 ", "python ", "bash ", "sh ", ""):
        rules[f"{runner}{scripts}*"] = "allow"
    for pattern in bash_patterns_from_allowed_tools(allowed_tools):
        rules[pattern] = "allow"
    for unsafe in UNSAFE_BASH_PATTERNS:
        rules[unsafe] = "deny"
    return rules


def bash_patterns_from_allowed_tools(allowed_tools: Iterable[str]) -> list[str]:
    patterns = []
    for entry in allowed_tools:
        match = BASH_TOOL_PATTERN.match(entry.strip())
        if not match:
            continue
        rule = match.group("rule").strip()
        if rule.endswith(":*"):
            prefix = rule[:-2].strip()
            if prefix:
                patterns.append(f"{prefix}*")
        elif rule and rule != "*":
            patterns.append(rule)
    return patterns


def _check_name(value, directory_name: str, errors: list[str]) -> str:
    if value is None:
        errors.append("'name' is required")
        return ""
    if not isinstance(value, str):
        errors.append("'name' must be a string")
        return ""
    if not 1 <= len(value) <= MAX_NAME_LENGTH:
        errors.append(f"'name' must be 1 to {MAX_NAME_LENGTH} characters")
    if not NAME_PATTERN.match(value):
        errors.append(
            "'name' may only use lowercase letters, digits and single hyphens, and must not start or end with a hyphen"
        )
    if value != directory_name:
        errors.append(f"'name' is {value!r} but the directory is {directory_name!r}; they must match")
    return value


def _check_description(value, errors: list[str]) -> str:
    if value is None:
        errors.append("'description' is required")
        return ""
    if not isinstance(value, str) or not value.strip():
        errors.append("'description' must be non-empty text")
        return ""
    if len(value) > MAX_DESCRIPTION_LENGTH:
        errors.append(f"'description' must be at most {MAX_DESCRIPTION_LENGTH} characters")
    return value.strip()


def _optional_text(data: dict, key: str, limit: int | None, errors: list[str]) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"'{key}' must be non-empty text when present")
        return None
    if limit is not None and len(value) > limit:
        errors.append(f"'{key}' must be at most {limit} characters")
    return value.strip()


def _check_metadata(value, errors: list[str]) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        errors.append("'metadata' must map text keys to text values")
        return {}
    return dict(value)


def _check_allowed_tools(value, errors: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        errors.append("'allowed-tools' must be one space-separated string, for example 'Bash(git:*) Read'")
        return ()
    return tuple(re.findall(r"\S+\([^)]*\)|\S+", value))


def _scan_foreign(root: Path, budget: list[int]) -> set[str]:
    names: set[str] = set()
    try:
        if not root.is_dir():
            return names
    except OSError:
        return names
    seen: set[str] = set()
    for current, dirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(current)
        depth = len(Path(current).relative_to(root).parts)
        if real in seen or depth > FOREIGN_SCAN_MAX_DEPTH:
            dirs[:] = []
            continue
        seen.add(real)
        for file_name in files:
            if file_name.lower() != SKILL_FILE.lower():
                continue
            budget[0] -= 1
            if budget[0] < 0:
                logger.warning("foreign_skill_scan_truncated", root=str(root))
                return names
            names.add(Path(current).name)
            names |= _declared_names(Path(current) / file_name)
    return names


def _declared_names(skill_file: Path) -> set[str]:
    try:
        with open(skill_file, "rb") as handle:
            head = handle.read(FOREIGN_SCAN_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return set()
    names = set()
    for match in DECLARED_NAME.finditer(head.replace("\r\n", "\n")):
        value = match.group(1).split(" #", 1)[0].strip().strip("'\"").strip()
        if value and len(value) <= 200:
            names.update({value, value.lower()})
    return names


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
