"""Least-recently-used eviction for a shared, size-bounded cache directory
— used by both `ari_oncall_agent/tools/repo_cache.py` (one entry per
cloned repo) and `ari_oncall_agent/tools/build_cache.py` (one entry per
build tool's own cache, e.g. `.m2`, `go`, `.npm`). Generic/reusable rather
than living inside either one: both PVCs are deliberately small (5Gi) next
to what they cache, so both need the same policy, just with different
locking shapes for what "in use, don't delete" means (see the two entry
points below).

Sizing signal is `shutil.disk_usage(cache_root)` — the actual filesystem
backing that mount point (a dedicated PVC in the real deployment), not a
recursive walk of file sizes. Cheap, and exactly matches what "the disk is
close to full" means for a bounded PVC.

"Least recently used" is approximated by the newest mtime found scanning a
few levels into each candidate — see `_last_used` — rather than an exact
last-access time (nothing here tracks explicit access events, and `atime`
is unreliable: many filesystems mount `noatime`). Good enough for eviction
ordering; the exact timestamp doesn't matter, only the relative order.
"""

import fcntl
import os
import shutil


def _last_used(path: str, max_depth: int = 3) -> float:
    """Newest mtime found scanning up to `max_depth` levels into `path`
    (stat calls only, never reads file contents) — cheap enough to run on
    every eviction check even for a multi-GB entry, unlike a full
    recursive walk."""
    best = os.stat(path).st_mtime
    frontier = [(path, 0)]
    while frontier:
        current, depth = frontier.pop()
        if depth >= max_depth:
            continue
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        best = max(best, entry.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        frontier.append((entry.path, depth + 1))
        except OSError:
            continue
    return best


def _free_fraction(cache_root: str) -> float | None:
    try:
        usage = shutil.disk_usage(cache_root)
    except OSError:
        return None
    if usage.total == 0:
        return None
    return usage.free / usage.total


def evict_lru_entries_if_low_on_space(
    cache_root: str,
    *,
    low_water: float,
    high_water: float,
    lock_suffix: str = ".lock",
) -> list[str]:
    """For a cache with one independently-lockable entry per immediate
    child directory (repo_cache.py's `<cache_root>/<repo-hash>`, each with
    its own `<entry><lock_suffix>` flock): if the filesystem is below
    `low_water` fraction free, deletes least-recently-used entries (oldest
    first) until back above `high_water` or nothing evictable remains.

    Never touches an entry whose lock is currently held (a non-blocking
    lock attempt, never a wait) — an in-flight fetch/clone survives an
    eviction sweep untouched. Eviction is opportunistic: a call that finds
    every entry busy simply evicts nothing this round; the next call (the
    next request against any repo) tries again. Returns the names deleted.
    """
    free_fraction = _free_fraction(cache_root)
    if free_fraction is None or free_fraction >= low_water:
        return []

    try:
        entries = [e for e in os.scandir(cache_root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    entries.sort(key=lambda e: _last_used(e.path))

    evicted = []
    for entry in entries:
        free_fraction = _free_fraction(cache_root)
        if free_fraction is None or free_fraction >= high_water:
            break

        lock_path = entry.path + lock_suffix
        lock_file = open(lock_path, "a+")
        try:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # in use right now -- skip, try again next sweep
            try:
                shutil.rmtree(entry.path, ignore_errors=True)
                evicted.append(entry.name)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()
        try:
            os.remove(lock_path)
        except OSError:
            pass
    return evicted


def evict_lru_children_if_low_on_space(
    cache_root: str,
    lock_path: str,
    *,
    low_water: float,
    high_water: float,
) -> list[str]:
    """For a cache with no natural per-entry lock — every immediate child
    is one shared tool's own cache directory (build_cache.py's `.m2`,
    `go`, `.npm`, ...), not an independent unit a caller locks by name —
    reader/writer semantics instead share one lock file for the whole
    root: an in-flight `verify` run holds it `LOCK_SH` (see
    build_cache.py's `reserve_build_cache_home`), and eviction here only
    ever attempts the exclusive form, non-blocking. If any run is
    currently using the cache, this is a no-op — never blocks, never
    deletes out from under an active build. Returns the names deleted.
    """
    free_fraction = _free_fraction(cache_root)
    if free_fraction is None or free_fraction >= low_water:
        return []

    lock_file = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return []  # a verify run is using the cache right now -- skip

        try:
            try:
                entries = [e for e in os.scandir(cache_root) if e.is_dir(follow_symlinks=False)]
            except OSError:
                return []
            entries.sort(key=lambda e: _last_used(e.path))

            evicted = []
            for entry in entries:
                free_fraction = _free_fraction(cache_root)
                if free_fraction is None or free_fraction >= high_water:
                    break
                shutil.rmtree(entry.path, ignore_errors=True)
                evicted.append(entry.name)
            return evicted
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()
