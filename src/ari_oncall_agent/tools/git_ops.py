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
    _, stderr = await proc.communicate()
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
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git symbolic-ref failed: {stderr.decode(errors='replace')}")
    return stdout.decode().strip()


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
    _, stderr = await proc.communicate()
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
    stdout, stderr = await proc.communicate()
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
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git add failed: {stderr.decode(errors='replace')}")


async def commit(workdir: str, title: str, body: str = "") -> None:
    """A fixed, generic author identity — this commit is ARI's, not any
    individual's, same reasoning as opencode's own agents never being
    asked to impersonate a person."""
    args = [
        "git", "-c", "user.email=support@insightfinder.com", "-c", "user.name=ARI Agent",
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
    _, stderr = await proc.communicate()
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
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git push failed: {stderr.decode(errors='replace')}")


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
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git diff --name-only failed: {stderr.decode(errors='replace')}")
    return [line for line in stdout.decode(errors="replace").splitlines() if line.strip()]
