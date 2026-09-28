"""Shared, persistent HOME for `verify`'s build/test subprocesses only
(mvn/go/npm, invoked via opencode's bash tool — see tools/opencode_cli.py
and settings.build_cache_root for why this exists and why it's scoped to
`verify` alone).

Needs a different lock shape than repo_cache.py's: every immediate child
here (`.m2`, `go`, `.npm`, ...) is one build tool's own shared cache
directory, not an independent per-repo unit a caller locks by name — so
there's no natural per-entry lock to reuse. Instead this is a classic
reader/writer lock over the whole root, one file:
  - a `verify` run holds it `LOCK_SH` (shared — many readers/writers of
    the actual cache contents can run at once, matching Maven/Go/npm's
    own concurrent-access design) for the duration of its opencode call.
  - eviction (common/cache_gc.py) only ever attempts `LOCK_EX | LOCK_NB` —
    non-blocking, so it either runs while genuinely idle (no verify in
    flight) or skips this round entirely. It never blocks a verify run,
    and a verify run never blocks waiting on a slow eviction sweep either
    (the sweep is a handful of `shutil.rmtree` calls, not network I/O).
"""

import asyncio
import fcntl
import os
from contextlib import asynccontextmanager

from common.cache_gc import evict_lru_children_if_low_on_space
from common.settings import get_settings

_LOCK_NAME = ".build-cache.lock"


@asynccontextmanager
async def reserve_build_cache_home():
    """Runs an opportunistic eviction sweep, then yields the build cache's
    root path held under a shared lock for the duration of the caller's
    block — guarantees eviction can never delete anything out from under
    an in-flight verify run."""
    settings = get_settings()
    root = settings.build_cache_root
    os.makedirs(root, exist_ok=True)
    lock_path = os.path.join(root, _LOCK_NAME)

    await asyncio.to_thread(
        evict_lru_children_if_low_on_space,
        root,
        lock_path,
        low_water=settings.cache_evict_low_water,
        high_water=settings.cache_evict_high_water,
    )

    lock_file = open(lock_path, "a+")
    try:
        await asyncio.to_thread(fcntl.flock, lock_file, fcntl.LOCK_SH)
        try:
            yield root
        finally:
            await asyncio.to_thread(fcntl.flock, lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()
