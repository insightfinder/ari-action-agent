import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_agent.tools.git_ops import changed_files, default_branch, diff  # noqa: E402


def _run(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _run("init", "-q", cwd=tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    _run("add", ".", cwd=tmp_path)
    _run("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_changed_files_only_lists_tracked_modifications(repo):
    (repo / "a.py").write_text("x = 2\n")
    # An untracked scratch file (e.g. left behind by a verify step) must
    # never show up here — this is exactly what keeps publish_pr from
    # sweeping it into a PR.
    (repo / "scratch_test.py").write_text("# leftover\n")

    files = await changed_files(str(repo))
    assert files == ["a.py"]


@pytest.mark.asyncio
async def test_diff_reflects_tracked_change_only(repo):
    (repo / "a.py").write_text("x = 2\n")
    (repo / "scratch_test.py").write_text("# leftover\n")

    result = await diff(str(repo))
    assert "a.py" in result
    assert "scratch_test.py" not in result


@pytest.mark.asyncio
async def test_default_branch_reports_whatever_head_actually_points_at(tmp_path):
    # Deliberately not "main" — proves this reads the real HEAD symref
    # instead of assuming any particular fixed name.
    _run("init", "-q", "-b", "trunk", cwd=tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    _run("add", ".", cwd=tmp_path)
    _run("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=tmp_path)

    assert await default_branch(str(tmp_path)) == "trunk"
