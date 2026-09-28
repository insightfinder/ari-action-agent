import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.capabilities_base import StepContext  # noqa: E402


def _ctx(results: dict, capability: str = "generate_change") -> StepContext:
    return StepContext(
        request_id="req-1",
        workspace="/tmp/ws",
        prompt="fix it",
        config={},
        capability=capability,
        params={},
        results=results,
    )


def test_repo_dir_falls_back_to_workspace_when_clone_repo_never_ran():
    # Deliberate, documented fallback -- e.g. a repo-less chat prompt, or
    # an agent kind (Jenkins) with no clone_repo capability at all.
    assert _ctx(results={}).repo_dir == "/tmp/ws"


def test_repo_dir_uses_clone_repos_cloned_path_when_present():
    ctx = _ctx(results={"clone_repo": {"cloned_path": "/tmp/ws/repo", "ref": "main"}})
    assert ctx.repo_dir == "/tmp/ws/repo"


def test_require_cloned_repo_raises_clearly_when_clone_repo_never_ran():
    # Regression test for a live failure: a planner (weaker/cheaper model)
    # produced a step list that included generate_change but omitted
    # clone_repo. Without this guard, that silently fell back to the
    # empty workspace root and only failed downstream inside opencode with
    # an opaque "Unexpected server error" -- no indication of the real,
    # simple cause.
    ctx = _ctx(results={}, capability="generate_change")

    with pytest.raises(RuntimeError, match="clone_repo must run before this step"):
        ctx.require_cloned_repo()


def test_require_cloned_repo_returns_repo_dir_when_clone_repo_did_run():
    ctx = _ctx(results={"clone_repo": {"cloned_path": "/tmp/ws/repo", "ref": "main"}})
    assert ctx.require_cloned_repo() == "/tmp/ws/repo"
