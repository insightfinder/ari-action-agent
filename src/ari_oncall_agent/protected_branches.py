"""Loads configs/oncall/protected_branches.yaml — a configurable blacklist
of branches this agent must never push to directly. Read-only access
(clone/fetch/pull, and opening a PR *against* one of these branches) is
always fine; only a direct push to the branch itself is blocked.

`main`/`master` are protected for *every* repo by default
(`default_protected_branches` in the YAML, `["main", "master"]` even if
the key is absent or the file doesn't exist — an explicit `[]` is the
only way to turn that off) — a per-repo `protected_branches` entry adds
to that default, it doesn't replace it.

Enforced in two different ways, because there are two different ways this
agent can end up pushing:

- `capabilities/publish_pr.py`'s deterministic primary path
  (`tools/git_ops.py:push`) always targets a freshly generated
  `ari/<slug>-<request-id>` branch, never an existing one — so this is
  unreachable there today, checked only as defense-in-depth against a
  future change accidentally passing a real branch name through.
- The opencode + GitHub MCP **fallback** (`ari-publish`, only reached if
  the primary path fails — see publish_pr.py) has real, LLM-driven `git
  push` bash access. `tools/opencode_cli.py` adds explicit bash-permission
  `deny` entries for the common push forms against each of this repo's
  protected branches, and the fallback's own instruction names them
  outright.

**That fallback enforcement is best-effort, not a hard guarantee.**
opencode's bash-permission matching (as used throughout this codebase) is
prefix + trailing-wildcard only — it cannot semantically parse arbitrary
git push syntax, so this covers the common invocation shapes, not every
syntactically possible one, and it has no visibility at all into the
GitHub MCP server's own tools (e.g. a hypothetical file-write API call
that never shells out to `git` in the first place). The only real
guarantee is server-side: configure branch protection on the GitHub repo
itself (Settings -> Branches -> protect these branches, restrict who/what
can push) — that can't be bypassed by any client-side git syntax and
covers the MCP path too.
"""

from functools import lru_cache
from urllib.parse import urlsplit

import yaml

from common.config_paths import config_file


def repo_slug(url: str) -> str:
    """`https://github.com/owner/repo(.git)` -> `owner/repo`, lowercased —
    the normalized key both the config file and lookups are matched by,
    so scheme/host/`.git`-suffix differences never cause a false miss."""
    path = urlsplit(url).path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path.lower()


_DEFAULT_PROTECTED_BRANCHES = frozenset({"main", "master"})


@lru_cache
def _load() -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    path = config_file("protected_branches.yaml")
    if not path.exists():
        return _DEFAULT_PROTECTED_BRANCHES, {}
    raw = yaml.safe_load(path.read_text()) or {}

    # `is not None`, not a truthiness check: an explicit `[]` in the YAML
    # must genuinely disable the default (an ops decision to make), while
    # an absent key falls back to protecting main/master — a falsy-empty
    # check would treat those two cases identically and make `[]` a no-op.
    defaults_raw = raw.get("default_protected_branches")
    defaults = (
        frozenset(str(b).strip() for b in defaults_raw) if defaults_raw is not None else _DEFAULT_PROTECTED_BRANCHES
    )

    per_repo = {
        repo_slug(repo_url): frozenset(str(b).strip() for b in (branches or []))
        for repo_url, branches in (raw.get("protected_branches") or {}).items()
    }
    return defaults, per_repo


def protected_branches(repo_url: str) -> frozenset[str]:
    """Every branch name blocked for direct push against this repo —
    `default_protected_branches` (main/master unless configured
    otherwise) plus this repo's own entry, if it has one. Never raises;
    a repo with no entry at all still gets the defaults."""
    defaults, per_repo = _load()
    return defaults | per_repo.get(repo_slug(repo_url), frozenset())


def is_protected(repo_url: str, branch: str) -> bool:
    return branch in protected_branches(repo_url)
