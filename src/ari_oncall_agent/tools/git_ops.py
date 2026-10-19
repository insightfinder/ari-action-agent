"""Clone helper supporting PAT (HTTPS) and SSH-key auth, matching
`RepoConfig` (api/schemas.py). Credentials are used only to build the
clone URL / SSH command for this one process call — never written to disk,
never logged.

`repo.url` is not restricted to a network remote — a local filesystem path
works too (git itself supports it), which matters whenever the source is
provided via a mounted volume rather than fetched from GitHub/GitLab. That
case can surface a real git behavior: git refuses to operate on a repo
whose directory is owned by a different user than the current process
("detected dubious ownership"), which is exactly what happens cloning a
bind-mounted host path in a container running as a different uid.
Confirmed live that this can *only* be worked around via real global git
config (`git config --global --add safe.directory '*'`, baked into the
Dockerfile at image build time) — passing `-c safe.directory=*` on this
command does NOT suppress the check for a local clone's source repo
(tried it; git still refused identically). Nothing to do here in Python —
just don't reach for `-c` if this check ever needs revisiting.
"""

import asyncio
import os
from urllib.parse import urlsplit, urlunsplit

from common.settings import get_settings


# Every git call here is a short, bounded operation — the slowest, `clone`
# of a large monorepo, is seconds to a couple of minutes. None of them has
# any business running for twenty minutes.
#
# They used to, though. Confirmed live on the test cluster: a
# `generate_change` run had opencode finish normally in 121s, then sat in
# `diff`'s `proc.communicate()` for a further 18 minutes until Temporal's
# 20-minute activity timeout cancelled it:
#
#   File "ari_oncall_agent/capabilities/generate_change.py", line 164, in run
#     diff = await git_diff(ctx.repo_dir)
#   File "ari_oncall_agent/tools/git_ops.py", line 132, in diff
#     stdout, stderr = await proc.communicate()
#   File "asyncio/base_subprocess.py", line 230, in _wait
#     return await waiter
#   asyncio.exceptions.CancelledError: Activity cancelled
#
# `communicate()` had already drained both pipes and was waiting on process
# exit; no git process was left in the container by the time it was
# inspected. The retry ran the identical command in the identical sandbox
# in under a second, so this is a race, not anything about the repo or the
# command. Whatever the underlying cause (a lost SIGCHLD under the worker's
# asyncio+threads mix is the leading suspect, unproven), an unbounded wait
# turns it into a 20-minute stall that the pipeline reports as nothing at
# all — `generate_change` has no `on_failure` policy, so two such attempts
# just fail the incident silently.
#
# A bound converts that into a fast, named failure the existing retry can
# act on. Generous on purpose: this is a deadlock guard, not a performance
# budget, and must never fire on a legitimately slow clone.
# Two tiers, because one number cannot serve both ends of this range.
# `clone` of a large monorepo is legitimately minutes; everything else is
# sub-second in practice (measured on InfrastructureCore: `diff` and
# `changed_files` both well under 1s). A single generous value would have
# left the observed `diff` stall costing five minutes instead of twenty —
# better, but still five minutes of nothing for an operation that has
# never taken more than a second.
#
# Still far above any real duration: this is a deadlock guard, and a guard
# that fires on healthy work is worse than no guard, because it turns a
# slow-but-fine run into a failed one.
# Three tiers, not two. `push` is the one non-clone command here that
# talks to the network, and it was sharing the 30s local-operation tier
# with `diff`/`add`/`commit`. Confirmed live (prod, 2026-10-08): three
# concurrent runs against ECommerce-Demo-APIServer all lost the
# deterministic publish path and fell through to the opencode + GitHub MCP
# fallback, which let the LLM invent branch names — two of them then
# reported the same pull request, one having silently *updated* the
# other's PR instead of opening its own. `git diff timed out after 30s`
# appears in the same worker log window, so 30s was demonstrably not
# enough headroom on that pod under concurrency, and a push to GitHub has
# strictly less business being held to a local-filesystem budget.
#
# Same principle as the two tiers below it: a deadlock guard, not a
# performance budget. A real push of a one-commit branch is well under a
# second; 120s is far above any healthy duration while still being a
# fraction of the step's own timeout.
_CLONE_TIMEOUT_SECONDS = float(os.getenv("GIT_CLONE_TIMEOUT_SECONDS", "600"))
_NETWORK_TIMEOUT_SECONDS = float(os.getenv("GIT_NETWORK_TIMEOUT_SECONDS", "120"))
_DEFAULT_TIMEOUT_SECONDS = float(os.getenv("GIT_SUBPROCESS_TIMEOUT_SECONDS", "30"))


async def _communicate(proc, what: str, timeout: float | None = None):
    """`proc.communicate()` with a deadline; kills the child and raises on expiry."""
    try:
        return await asyncio.wait_for(
            proc.communicate(), timeout=timeout or _DEFAULT_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        # Don't leave the child behind holding the sandbox: attempts of one
        # request share a workspace directory (sandbox is keyed by
        # request_id alone), so a survivor could still be writing to the
        # tree the retry is about to read.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError(
            f"{what} timed out after {timeout or _DEFAULT_TIMEOUT_SECONDS:g}s "
            f"(pid {proc.pid}) and was killed"
        ) from None


def authed_url(url: str, repo_config: dict) -> str:
    """Builds a PAT-embedded clone/remote URL when `repo_config` has one;
    otherwise returns `url` unchanged. Shared by `clone()` here and by
    `tools/repo_cache.py`, which needs the identical transform to fetch
    the mirror cache and to re-point a per-request clone's origin at the
    real upstream after cloning from that local cache."""
    if not repo_config.get("pat"):
        return url
    parts = urlsplit(url)
    authed_netloc = f"x-access-token:{repo_config['pat']}@{parts.netloc}"
    return urlunsplit((parts.scheme, authed_netloc, parts.path, parts.query, parts.fragment))


async def clone(repo_config: dict, workspace: str) -> str:
    url = repo_config["url"]
    ref = repo_config.get("ref", "main")
    dest = os.path.join(workspace, "repo")

    env = os.environ.copy()
    if repo_config.get("pat"):
        url = authed_url(url, repo_config)
    elif repo_config.get("ssh_key"):
        key_path = os.path.join(workspace, "deploy_key")
        with open(key_path, "w") as f:
            f.write(repo_config["ssh_key"])
        os.chmod(key_path, 0o600)
        env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o StrictHostKeyChecking=no"

    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", "--branch", ref, url, dest,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await _communicate(proc, "git clone", timeout=_CLONE_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {stderr.decode(errors='replace')}")
    return dest


async def default_branch(cache_dir: str) -> str:
    """The remote's actual default branch (e.g. "main" vs "master" vs
    something else entirely), read from a local `--mirror` cache rather
    than assumed. A `--mirror` clone mirrors the remote's `HEAD` symref
    verbatim at clone time, so `git symbolic-ref` here reports exactly what
    the remote considers its default branch — no guessing, no hardcoded
    fallback string."""
    proc = await asyncio.create_subprocess_exec(
        "git", "symbolic-ref", "--short", "HEAD",
        cwd=cache_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate(proc, "git default branch")
    if proc.returncode != 0:
        raise RuntimeError(f"git symbolic-ref failed: {stderr.decode(errors='replace')}")
    return stdout.decode().strip()


async def branch_exists(cache_dir: str, branch: str) -> bool:
    """Whether `branch` actually exists in this `--mirror` cache — checked
    *before* attempting to clone an explicitly-given `ref`, so a wrong
    guess (e.g. the planner assuming "main" for a repo whose real default
    branch is "dev") is caught proactively (no failed clone attempt, no
    fragile matching on git's own error text) rather than reactively."""
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
        cwd=cache_dir,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def set_origin(workdir: str, repo_config: dict) -> None:
    """Re-points `origin` at the real upstream URL (PAT-embedded if
    configured). Used after cloning from the local repo cache — that
    clone's `origin` initially points at the cache directory, not the
    real remote, which would be wrong for anything (a tool, or opencode
    itself poking around) that inspects `git remote -v` expecting to see
    the actual upstream."""
    url = authed_url(repo_config["url"], repo_config)
    proc = await asyncio.create_subprocess_exec(
        "git", "remote", "set-url", "origin", url,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await _communicate(proc, "git set origin")
    if proc.returncode != 0:
        raise RuntimeError(f"git remote set-url failed: {stderr.decode(errors='replace')}")


async def diff(workdir: str) -> str:
    """Working-tree diff (tracked file changes opencode made, staged or
    not) — used for the PR body / audit trail. Deliberately not sourced
    from opencode's own output: git is authoritative on what actually
    changed on disk, regardless of how opencode's event stream describes it.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "diff", "HEAD",
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate(proc, "git diff")
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed: {stderr.decode(errors='replace')}")
    return stdout.decode(errors="replace")


async def add(workdir: str, files: list[str]) -> None:
    """Stages exactly `files` — never `-A`/`.` (see publish_pr.py's own
    instruction text for why: other steps, e.g. verify, can leave scratch
    files behind in the working tree that must never end up in a PR)."""
    proc = await asyncio.create_subprocess_exec(
        "git", "add", "--", *files,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await _communicate(proc, "git add")
    if proc.returncode != 0:
        raise RuntimeError(f"git add failed: {stderr.decode(errors='replace')}")


async def commit(workdir: str, title: str, body: str = "") -> None:
    """A fixed, generic author identity — this commit is ARI's, not any
    individual's, same reasoning as opencode's own agents never being
    asked to impersonate a person."""
    settings = get_settings()
    args = [
        "git", "-c", f"user.email={settings.commit_author_email}", "-c", f"user.name={settings.commit_author_name}",
        "commit", "-m", title,
    ]
    if body:
        args += ["-m", body]
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await _communicate(proc, "git commit")
    if proc.returncode != 0:
        raise RuntimeError(f"git commit failed: {stderr.decode(errors='replace')}")


async def push(workdir: str, repo_config: dict, branch: str) -> None:
    """Pushes the current HEAD to a *new* remote branch (`branch`) —
    `HEAD:refs/heads/<branch>` creates that branch on the remote from
    whatever's currently checked out locally (e.g. still "main"), without
    needing to rename or check out a local branch first, and without
    touching the real main/master on the remote."""
    url = authed_url(repo_config["url"], repo_config)
    env = os.environ.copy()
    if not repo_config.get("pat") and repo_config.get("ssh_key"):
        key_path = os.path.join(workdir, "deploy_key_push")
        with open(key_path, "w") as f:
            f.write(repo_config["ssh_key"])
        os.chmod(key_path, 0o600)
        env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o StrictHostKeyChecking=no"

    proc = await asyncio.create_subprocess_exec(
        "git", "push", url, f"HEAD:refs/heads/{branch}",
        cwd=workdir,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await _communicate(proc, "git push", timeout=_NETWORK_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"git push failed: {stderr.decode(errors='replace')}")


async def untracked_files(workdir: str) -> list[str]:
    """Paths git does not track yet, honouring .gitignore and
    `.git/info/exclude` (so the per-request opencode.json this agent writes
    is already excluded — see tools/opencode_cli.py).

    Exists for `generate_change` to tell a file opencode just created apart
    from one that was already lying around: it snapshots this before the
    opencode call and again after, and only the difference is part of the
    change. Confirmed live why that is needed (test, 2026-10-09) — see
    `intent_to_add` below.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "ls-files", "--others", "--exclude-standard",
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate(proc, "git untracked files")
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files --others failed: {stderr.decode(errors='replace')}")
    return [line for line in stdout.decode(errors="replace").splitlines() if line.strip()]


async def intent_to_add(workdir: str, files: list[str]) -> None:
    """`git add -N` ("intent to add") for brand-new files, so the two
    functions around this one can see them at all.

    Both `diff` and `changed_files` below ask git for changes against HEAD,
    which by definition never includes an untracked file. That made every
    change whose whole content is a NEW file completely invisible:
    `changed_files` came back empty and `diff` came back blank, however
    much opencode had actually written.

    Confirmed live (test, 2026-10-09, workflow 979a2b0e against
    OnCall-ARI-InfraCore-testing@dev): asked for a CONTRIBUTING.md that
    the repository did not have, opencode called `write` (tool trace:
    read 2, glob 1, grep 1, write 1), the file was created — and the
    pipeline reported that the working tree was unmodified. The retry
    called `edit` on that same still-untracked file and reported the same
    thing. Before generate_change's empty-tree guard existed this surfaced
    one step later and just as wrongly, as `plan_pr`'s "generate_change
    reported no changed files to publish", after paying for a full verify
    run first. Either way: this agent could not ship "add a new file",
    which is a large share of real fixes (a new test, a new module, a new
    config).

    `-N` stages the path with an empty blob and no content, so `git diff
    HEAD` renders the whole file as additions. That matters beyond the
    file list: `propose_pr`'s `key_hunks()` reads that diff, so without
    this a human would be approving a new file whose contents the approval
    email never showed them.
    """
    if not files:
        return
    proc = await asyncio.create_subprocess_exec(
        "git", "add", "-N", "--", *files,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await _communicate(proc, "git intent-to-add")
    if proc.returncode != 0:
        raise RuntimeError(f"git add -N failed: {stderr.decode(errors='replace')}")


async def changed_files(workdir: str) -> list[str]:
    """Names of files generate_change actually modified (tracked files only,
    relative to HEAD). Used to scope publish_pr's staging to exactly this —
    live testing showed the verify agent can leave scratch files behind
    (e.g. a throwaway test script, __pycache__) despite being asked to
    clean up after itself; those are untracked and this list won't include
    them, so publish_pr never has to trust a blanket `git add -A`."""
    proc = await asyncio.create_subprocess_exec(
        "git", "diff", "--name-only", "HEAD",
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate(proc, "git changed files")
    if proc.returncode != 0:
        raise RuntimeError(f"git diff --name-only failed: {stderr.decode(errors='replace')}")
    return [line for line in stdout.decode(errors="replace").splitlines() if line.strip()]
