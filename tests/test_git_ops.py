import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_oncall_agent.tools import git_ops  # noqa: E402
from ari_oncall_agent.tools.git_ops import changed_files, default_branch, diff  # noqa: E402


def _run(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _output(*args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


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


@pytest.fixture
def remote_and_clone(tmp_path):
    """A bare "remote" repo plus a real clone of it with one commit
    already pushed to `main` — what publish_pr's git_add/commit/push
    actually operate against (a clone_repo checkout), not just a
    standalone local repo."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _run("init", "-q", "--bare", "-b", "main", cwd=remote)

    work = tmp_path / "work"
    _run("clone", "-q", str(remote), str(work), cwd=tmp_path)
    (work / "a.py").write_text("x = 1\n")
    _run("add", ".", cwd=work)
    _run("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=work)
    _run("push", "-q", "origin", "main", cwd=work)
    return remote, work


@pytest.mark.asyncio
async def test_add_stages_only_the_given_files(remote_and_clone):
    _, work = remote_and_clone
    (work / "a.py").write_text("x = 2\n")
    (work / "scratch.py").write_text("# leftover\n")

    await git_ops.add(str(work), ["a.py"])

    status = _output("status", "--porcelain", cwd=work)
    assert "M  a.py" in status
    assert "?? scratch.py" in status  # untouched -- never staged


@pytest.mark.asyncio
async def test_add_raises_clearly_on_a_bad_pathspec(remote_and_clone):
    _, work = remote_and_clone
    with pytest.raises(RuntimeError, match="git add failed"):
        await git_ops.add(str(work), ["does-not-exist.py"])


@pytest.mark.asyncio
async def test_commit_records_title_and_body(remote_and_clone):
    _, work = remote_and_clone
    (work / "a.py").write_text("x = 2\n")
    await git_ops.add(str(work), ["a.py"])

    await git_ops.commit(str(work), "Fix the thing", "Because it was broken.")

    log = _output("log", "-1", "--pretty=%B", cwd=work)
    assert "Fix the thing" in log
    assert "Because it was broken." in log


@pytest.mark.asyncio
async def test_commit_raises_clearly_when_nothing_is_staged(remote_and_clone):
    _, work = remote_and_clone
    with pytest.raises(RuntimeError, match="git commit failed"):
        await git_ops.commit(str(work), "Nothing to commit")


@pytest.mark.asyncio
async def test_push_creates_a_new_remote_branch_without_touching_the_checked_out_one(remote_and_clone):
    remote, work = remote_and_clone
    (work / "a.py").write_text("x = 2\n")
    await git_ops.add(str(work), ["a.py"])
    await git_ops.commit(str(work), "Fix the thing")

    await git_ops.push(str(work), {"url": str(remote)}, "ari/change-1")

    refs = _output("for-each-ref", "refs/heads", "--format=%(refname:short)", cwd=remote).split()
    assert "ari/change-1" in refs
    assert "main" in refs  # the real default branch is untouched
    main_head = _output("rev-parse", "main", cwd=remote).strip()
    new_branch_head = _output("rev-parse", "ari/change-1", cwd=remote).strip()
    assert main_head != new_branch_head  # main's tip didn't move


@pytest.mark.asyncio
async def test_push_raises_clearly_when_the_remote_is_unreachable(tmp_path, remote_and_clone):
    _, work = remote_and_clone
    with pytest.raises(RuntimeError, match="git push failed"):
        await git_ops.push(str(work), {"url": str(tmp_path / "does-not-exist")}, "ari/change-1")
