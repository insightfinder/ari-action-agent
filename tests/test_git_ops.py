import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_oncall_agent.tools import git_ops  # noqa: E402
from ari_oncall_agent.tools.git_ops import branch_exists, changed_files, default_branch, diff  # noqa: E402


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


@pytest.mark.asyncio
async def test_branch_exists_true_for_a_real_branch_false_for_a_made_up_one(tmp_path):
    _run("init", "-q", "-b", "trunk", cwd=tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    _run("add", ".", cwd=tmp_path)
    _run("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=tmp_path)

    assert await branch_exists(str(tmp_path), "trunk") is True
    assert await branch_exists(str(tmp_path), "main") is False


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
async def test_commit_always_uses_the_fixed_ari_agent_identity(remote_and_clone):
    # Never the requester's own identity -- there is no per-request "who
    # is this" to commit as, only a repo.pat used for push auth (see
    # authed_url), so every commit is authored as ARI itself regardless of
    # whose request/PAT it is.
    _, work = remote_and_clone
    (work / "a.py").write_text("x = 2\n")
    await git_ops.add(str(work), ["a.py"])

    await git_ops.commit(str(work), "Fix the thing")

    author = _output("log", "-1", "--pretty=%an <%ae>", cwd=work).strip()
    assert author == "ARI Agent <support@insightfinder.com>"


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


# --- subprocess deadline ---------------------------------------------
#
# Live failure this guards: a `generate_change` run had opencode finish
# normally in 121s, then sat in `diff`'s `proc.communicate()` for another
# 18 minutes until Temporal's activity timeout cancelled it. The pipes had
# already drained -- it was stuck awaiting process exit -- and no git
# process remained in the container. The retry ran the same command in the
# same sandbox in under a second, so it is a race, not the repo or the
# command. Unbounded, it costs 20 minutes and (since `generate_change` has
# no `on_failure` policy) reports nothing.


class _NeverExits:
    """A process whose communicate() never returns, like the live hang."""

    def __init__(self):
        self.pid = 4242
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(3600)

    def kill(self):
        self.killed = True


class _Gone(_NeverExits):
    """Already reaped by the time we try to kill it."""

    def kill(self):
        raise ProcessLookupError


@pytest.mark.asyncio
async def test_communicate_returns_normally_within_the_deadline():
    class _Fast:
        pid = 1
        async def communicate(self):
            return b"out", b""

    assert await git_ops._communicate(_Fast(), "git diff") == (b"out", b"")


@pytest.mark.asyncio
async def test_communicate_raises_instead_of_hanging_forever():
    proc = _NeverExits()
    with pytest.raises(RuntimeError) as exc_info:
        await git_ops._communicate(proc, "git diff", timeout=0.05)
    message = str(exc_info.value)
    assert "git diff" in message, "must name the operation that stalled"
    assert "timed out" in message


@pytest.mark.asyncio
async def test_a_stalled_child_is_killed_not_left_holding_the_sandbox():
    # Attempts of one request share a workspace (the sandbox is keyed by
    # request_id alone), so a survivor could still be writing to the tree
    # the retry is about to read.
    proc = _NeverExits()
    with pytest.raises(RuntimeError):
        await git_ops._communicate(proc, "git diff", timeout=0.05)
    assert proc.killed is True


@pytest.mark.asyncio
async def test_kill_failing_because_the_child_already_exited_is_not_an_error():
    # The suspected cause is a lost exit notification, so the child being
    # gone already is the expected case, not an exceptional one.
    with pytest.raises(RuntimeError, match="timed out"):
        await git_ops._communicate(_Gone(), "git diff", timeout=0.05)


@pytest.mark.asyncio
async def test_every_git_call_goes_through_the_deadline_helper(repo):
    # A new git helper added later must not reintroduce a bare
    # communicate() -- that is exactly how this bug shipped.
    import inspect
    source = inspect.getsource(git_ops)
    bare = [
        line.strip()
        for line in source.splitlines()
        if "await proc.communicate()" in line and not line.strip().startswith("#")
    ]
    assert bare == [], f"bare communicate() without a deadline: {bare}"


def test_deadlines_are_tiered_to_what_each_operation_actually_costs():
    # One number cannot serve both ends: clone of a large monorepo is
    # legitimately minutes, while diff/add/commit/changed_files are
    # sub-second in practice. A single generous value left the observed
    # `diff` stall costing five minutes instead of twenty — better, but
    # still five minutes of nothing for a sub-second operation.
    assert git_ops._CLONE_TIMEOUT_SECONDS >= 300, "clone must tolerate a big monorepo"
    assert git_ops._DEFAULT_TIMEOUT_SECONDS >= 10, "still a guard, not a latency budget"
    assert git_ops._DEFAULT_TIMEOUT_SECONDS < git_ops._CLONE_TIMEOUT_SECONDS


def test_only_clone_gets_the_long_deadline():
    # Guards the wiring, not just the constants: clone is the one call
    # site that may pass the long tier; everything else must fall through
    # to the short default, or the tiering is decorative.
    import inspect
    calls = [
        line.strip()
        for line in inspect.getsource(git_ops).splitlines()
        if "_communicate(proc" in line and not line.strip().startswith("#")
    ]
    long_tier = [c for c in calls if "_CLONE_TIMEOUT_SECONDS" in c]
    assert len(long_tier) == 1, f"exactly one long-tier call expected, got {long_tier}"
    assert '"git clone"' in long_tier[0]
    assert len(calls) >= 8, "all git call sites should be routed through the helper"


@pytest.mark.asyncio
async def test_push_gets_the_network_tier_not_the_local_one(remote_and_clone, monkeypatch):
    # `push` is the only non-clone command here that talks to a remote, and
    # it used to share the 30s local-operation budget with diff/add/commit.
    # Confirmed live (prod, 2026-10-08): `git diff timed out after 30s` in
    # the same worker-log window where three concurrent runs all lost the
    # deterministic publish path and fell through to the opencode + GitHub
    # MCP fallback, which then reported one run's pull request as another's.
    remote, work = remote_and_clone
    seen = {}

    real_communicate = git_ops._communicate

    async def recording(proc, what, timeout=None):
        seen[what] = timeout
        return await real_communicate(proc, what, timeout=timeout)

    monkeypatch.setattr(git_ops, "_communicate", recording)
    (work / "a.py").write_text("x = 2\n")
    await git_ops.add(str(work), ["a.py"])
    await git_ops.commit(str(work), "Fix the thing")
    await git_ops.push(str(work), {"url": str(remote)}, "ari/net-tier")

    assert seen["git push"] == git_ops._NETWORK_TIMEOUT_SECONDS
    assert git_ops._NETWORK_TIMEOUT_SECONDS > git_ops._DEFAULT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_local_only_git_calls_keep_the_short_tier(repo, monkeypatch):
    # Contrast case: widening the budget for the network must not widen it
    # for the operations the short tier was measured against (diff and
    # changed_files are both well under a second on a real monorepo), or
    # the deadlock guard stops guarding anything.
    seen = {}
    real_communicate = git_ops._communicate

    async def recording(proc, what, timeout=None):
        seen[what] = timeout
        return await real_communicate(proc, what, timeout=timeout)

    monkeypatch.setattr(git_ops, "_communicate", recording)
    await diff(str(repo))
    await changed_files(str(repo))

    assert seen["git diff"] is None, "must fall through to _DEFAULT_TIMEOUT_SECONDS"
    assert seen["git changed files"] is None


def test_the_three_timeout_tiers_are_env_overridable_and_ordered():
    # Each tier has its own env var so a slow target repo can be given more
    # room without loosening the other two.
    assert git_ops._DEFAULT_TIMEOUT_SECONDS < git_ops._NETWORK_TIMEOUT_SECONDS < git_ops._CLONE_TIMEOUT_SECONDS
    import inspect
    source = inspect.getsource(git_ops)
    for var in ("GIT_SUBPROCESS_TIMEOUT_SECONDS", "GIT_NETWORK_TIMEOUT_SECONDS", "GIT_CLONE_TIMEOUT_SECONDS"):
        assert var in source, f"{var} must stay overridable without a redeploy"


@pytest.mark.asyncio
async def test_untracked_files_lists_new_files_and_honours_gitignore(repo):
    (repo / "brandnew.md").write_text("new content\n")
    (repo / "scratch.tmp").write_text("junk\n")
    (repo / ".gitignore").write_text("scratch.tmp\n")
    _run("add", ".gitignore", cwd=repo)
    _run("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "ignore", cwd=repo)

    found = await git_ops.untracked_files(str(repo))

    assert "brandnew.md" in found
    assert "scratch.tmp" not in found, "--exclude-standard must honour .gitignore"


@pytest.mark.asyncio
async def test_intent_to_add_makes_a_new_file_visible_to_diff_and_changed_files(repo):
    # The whole point: `git diff HEAD` cannot see an untracked path, so a
    # change whose entire content is a new file read as "nothing changed"
    # (live: test workflow 979a2b0e, 2026-10-09).
    (repo / "brandnew.md").write_text("new content\n")
    assert await changed_files(str(repo)) == []
    assert await diff(str(repo)) == ""

    await git_ops.intent_to_add(str(repo), ["brandnew.md"])

    assert "brandnew.md" in await changed_files(str(repo))
    # The body has to be in the diff too -- propose_pr's key_hunks() reads
    # it for the approval email, so without this a human would be approving
    # a new file whose contents they were never shown.
    assert "new content" in await diff(str(repo))


@pytest.mark.asyncio
async def test_intent_to_add_is_a_no_op_for_an_empty_list(repo):
    # Called unconditionally by generate_change, which usually has nothing
    # new to stage; `git add -N --` with no paths is an error, not a no-op.
    await git_ops.intent_to_add(str(repo), [])

    assert await changed_files(str(repo)) == []


@pytest.mark.asyncio
async def test_intent_to_add_raises_clearly_on_a_bad_path(repo):
    with pytest.raises(RuntimeError, match="git add -N failed"):
        await git_ops.intent_to_add(str(repo), ["no/such/file.md"])
