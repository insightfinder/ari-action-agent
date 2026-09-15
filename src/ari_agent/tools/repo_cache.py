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

from ..settings import get_settings
from .git_ops import authed_url

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


def _sync_ensure_cache(repo_config: dict, cache_root: str) -> str:
    plain_url = repo_config["url"]
    key = _cache_key(plain_url)
    cache_dir = os.path.join(cache_root, key)
    os.makedirs(cache_root, exist_ok=True)

    lock_path = cache_dir + ".lock"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
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
                    # config immediately — every later fetch supplies it
                    # fresh on the command line instead of relying on this.
                    _run(["git", "remote", "set-url", "origin", plain_url], cwd=cache_dir)
                else:
                    _run(["git", "fetch", fetch_url, "+refs/*:refs/*", "--prune"], cwd=cache_dir, env=env)
            finally:
                if deploy_key_path:
                    os.remove(deploy_key_path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

    return cache_dir


async def ensure_cache(repo_config: dict) -> str:
    """Ensures a local mirror cache for `repo_config['url']` exists and is
    freshly fetched, then returns the cache directory path. Safe to call
    concurrently for the same or different repos — same-repo callers
    serialize on that repo's lock; different repos proceed independently.
    """
    cache_root = get_settings().repo_cache_root
    key = _cache_key(repo_config["url"])
    async with _get_inprocess_lock(key):
        return await asyncio.to_thread(_sync_ensure_cache, repo_config, cache_root)
