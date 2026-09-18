"""Per-worker local mirror cache for target repos, so multiple requests
against the same repo don't each pay for a full fresh clone (time,
bandwidth, disk). One `git clone --mirror` per unique repo URL, kept
current with a `git fetch` immediately before every use — "latest every
time," not just on first use. Each request then gets its own independent
working copy via a plain local clone FROM that cache (see
`capabilities/clone_repo.py`), never touching the cache's own working
tree — so concurrent requests against the same repo can edit
independently without interfering, and one request's failure/half-applied
patch can never corrupt what another request sees.

Scope/limits, documented rather than silently assumed:
- This cache lives on whichever worker process/host handles the request.
  A single worker replica benefits fully; multiple replicas each keep
  their own independent cache unless `REPO_CACHE_ROOT` is a shared volume
  mounted at the same path on every replica.
- Locking is two-layered: an in-process `asyncio.Lock` (covers concurrent
  activities inside one worker process) plus a real `fcntl.flock` (covers
  multiple worker OS processes sharing the same cache root on one host).
  Neither covers multiple hosts without a shared filesystem that itself
  supports fcntl-style locking (most network filesystems used for this,
  e.g. NFS, do).
- **The lock covers the local clone-from-cache too, not just the fetch.**
  Confirmed live as a real bug, not a theoretical one: with the lock held
  only for the fetch (the original version of this module), one request's
  local `git clone` reading from the mirror could still be mid-copy when a
  second concurrent request's `git fetch --prune` on that same mirror
  deleted/repacked the exact pack file the first was reading —
  `fatal: failed to copy file to '.../pack-<hash>.pack': No such file or
  directory`. `ensure_cache_and_clone()` below holds one lock acquisition
  across the fetch-or-create step *and* the subsequent local clone (and,
  when no `ref` is given, the default-branch lookup in between), so a
  concurrent fetch can never run while another request is still reading.
  This fully serializes same-repo requests through that (typically short)
  window rather than letting reads and writes interleave — a deliberate
  correctness-over-throughput trade-off; requests against *different*
  repos are unaffected (they use different lock keys).
- No eviction/GC — the cache root grows with the number of distinct repos
  ever requested. Fine for a bounded set of well-known repos; revisit if
  that stops being true.
- Never persists a caller's credential to disk: the initial mirror clone
  and every later fetch pass a one-off authenticated URL on the command
  line (never stored), and the cache's persisted `origin` remote is
  scrubbed back to the plain URL immediately after the initial clone — a
  later request's cache *read* (the local clone into its own sandbox)
  needs no credential at all, since it's a plain local filesystem clone.
  An SSH deploy key, if used, is written to a throwaway temp file for the
  duration of one fetch and deleted immediately after, success or failure.
"""

import asyncio
import fcntl
import hashlib
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager

from ..settings import get_settings
from .git_ops import authed_url
from .git_ops import clone as git_clone
from .git_ops import default_branch as git_default_branch

_inprocess_locks: dict[str, asyncio.Lock] = {}


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:20]


def _run(args: list[str], cwd: str | None = None, env: dict | None = None) -> None:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {result.stderr}")


def _get_inprocess_lock(key: str) -> asyncio.Lock:
    lock = _inprocess_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _inprocess_locks[key] = lock
    return lock


def _sync_refresh_cache(repo_config: dict, cache_root: str) -> str:
    """Creates or fetches this repo's local mirror and returns its path.
    Does no locking of its own — every caller is responsible for holding
    this repo's lock (`_locked_cache_dir` below) for the duration of this
    call, since it mutates the mirror on disk."""
    plain_url = repo_config["url"]
    key = _cache_key(plain_url)
    cache_dir = os.path.join(cache_root, key)
    os.makedirs(cache_root, exist_ok=True)

    env = os.environ.copy()
    deploy_key_path = None
    if repo_config.get("ssh_key"):
        fd, deploy_key_path = tempfile.mkstemp(prefix="ari-repo-cache-key-")
        with os.fdopen(fd, "w") as f:
            f.write(repo_config["ssh_key"])
        os.chmod(deploy_key_path, 0o600)
        env["GIT_SSH_COMMAND"] = f"ssh -i {deploy_key_path} -o StrictHostKeyChecking=no"

    try:
        fetch_url = authed_url(plain_url, repo_config)
        if not os.path.isdir(cache_dir):
            _run(["git", "clone", "--mirror", fetch_url, cache_dir], env=env)
            # Scrub the credential out of the cache's own persisted
            # config immediately — every later fetch supplies it fresh on
            # the command line instead of relying on this.
            _run(["git", "remote", "set-url", "origin", plain_url], cwd=cache_dir)
        else:
            _run(["git", "fetch", fetch_url, "+refs/*:refs/*", "--prune"], cwd=cache_dir, env=env)
    finally:
        if deploy_key_path:
            os.remove(deploy_key_path)

    return cache_dir


@asynccontextmanager
async def _locked_cache_dir(url: str):
    """Holds this repo's lock (in-process + `fcntl`) for the duration of
    the caller's block. One acquisition per call site — never nest two
    calls to this for the same `url` in the same task, since
    `asyncio.Lock` isn't reentrant and a second acquire would deadlock
    against itself."""
    cache_root = get_settings().repo_cache_root
    key = _cache_key(url)
    os.makedirs(cache_root, exist_ok=True)
    lock_path = os.path.join(cache_root, key) + ".lock"

    async with _get_inprocess_lock(key):
        lock_file = open(lock_path, "w")
        try:
            await asyncio.to_thread(fcntl.flock, lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                await asyncio.to_thread(fcntl.flock, lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


async def ensure_cache(repo_config: dict) -> str:
    """Ensures a local mirror cache for `repo_config['url']` exists and is
    freshly fetched, then returns the cache directory path. Safe to call
    concurrently for the same or different repos — same-repo callers
    serialize on that repo's lock; different repos proceed independently.

    Only refreshes the mirror — does not itself protect a *subsequent*
    read of the returned directory from a later concurrent caller's
    refresh. Capabilities that also need to read from the cache right
    after (i.e. `clone_repo`) should use `ensure_cache_and_clone` instead,
    which holds the lock across both steps.
    """
    async with _locked_cache_dir(repo_config["url"]):
        return await asyncio.to_thread(_sync_refresh_cache, repo_config, get_settings().repo_cache_root)


async def ensure_cache_and_clone(repo_config: dict, ref: str | None, workspace: str) -> tuple[str, str]:
    """Refreshes this repo's mirror cache and clones from it into
    `workspace` (matching `git_ops.clone`'s own convention, this creates
    `workspace/repo`), all under one held lock — so a concurrent request's
    cache refresh can never delete/repack a pack file this clone is still
    reading (see the module docstring for the real failure this fixes). If
    `ref` is None, resolves the remote's actual default branch from the
    now-fresh cache before cloning, under the same lock, so the resolution
    and the clone see a consistent cache state. Returns
    `(cloned_path, resolved_ref)`.
    """
    async with _locked_cache_dir(repo_config["url"]):
        cache_dir = await asyncio.to_thread(_sync_refresh_cache, repo_config, get_settings().repo_cache_root)
        resolved_ref = ref or await git_default_branch(cache_dir)
        cloned_path = await git_clone({"url": cache_dir, "ref": resolved_ref}, workspace)
        return cloned_path, resolved_ref
