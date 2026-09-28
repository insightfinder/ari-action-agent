import fcntl
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import common.cache_gc as cache_gc  # noqa: E402


def _usage(total: int, free: int) -> SimpleNamespace:
    return SimpleNamespace(total=total, free=free, used=total - free)


def test_evict_lru_entries_is_a_noop_when_space_is_fine(tmp_path, monkeypatch):
    (tmp_path / "some-repo").mkdir()
    monkeypatch.setattr(cache_gc.shutil, "disk_usage", lambda p: _usage(100, 50))

    evicted = cache_gc.evict_lru_entries_if_low_on_space(str(tmp_path), low_water=0.15, high_water=0.30)

    assert evicted == []
    assert (tmp_path / "some-repo").is_dir()


def test_evict_lru_entries_deletes_oldest_first_and_stops_at_high_water(tmp_path, monkeypatch):
    old_repo = tmp_path / "old-repo"
    new_repo = tmp_path / "new-repo"
    old_repo.mkdir()
    new_repo.mkdir()
    now = time.time()
    os.utime(old_repo, (now - 1000, now - 1000))
    os.utime(new_repo, (now, now))

    calls = {"n": 0}

    def fake_disk_usage(path):
        calls["n"] += 1
        # Low free space for the initial check and the pre-eviction check
        # on the first (oldest) candidate; plenty free from then on, so
        # the second (newer) candidate is never touched.
        return _usage(100, 10) if calls["n"] <= 2 else _usage(100, 50)

    monkeypatch.setattr(cache_gc.shutil, "disk_usage", fake_disk_usage)

    evicted = cache_gc.evict_lru_entries_if_low_on_space(str(tmp_path), low_water=0.15, high_water=0.30)

    assert evicted == ["old-repo"]
    assert not old_repo.exists()
    assert new_repo.exists()


def test_evict_lru_entries_never_touches_a_locked_entry(tmp_path, monkeypatch):
    old_repo = tmp_path / "old-repo"
    new_repo = tmp_path / "new-repo"
    old_repo.mkdir()
    new_repo.mkdir()
    now = time.time()
    os.utime(old_repo, (now - 1000, now - 1000))
    os.utime(new_repo, (now, now))

    monkeypatch.setattr(cache_gc.shutil, "disk_usage", lambda p: _usage(100, 5))  # always low

    lock_holder = open(str(old_repo) + ".lock", "a+")
    fcntl.flock(lock_holder, fcntl.LOCK_EX)
    try:
        evicted = cache_gc.evict_lru_entries_if_low_on_space(str(tmp_path), low_water=0.15, high_water=0.30)
    finally:
        fcntl.flock(lock_holder, fcntl.LOCK_UN)
        lock_holder.close()

    # old-repo is locked (in use) so it's skipped; new-repo is the only
    # thing actually evictable, even though it's the more-recently-used one.
    assert evicted == ["new-repo"]
    assert old_repo.exists()
    assert not new_repo.exists()


def test_evict_lru_children_is_a_noop_when_the_whole_cache_is_in_use(tmp_path, monkeypatch):
    root = tmp_path / "build-cache"
    root.mkdir()
    (root / "m2").mkdir()
    lock_path = str(root / ".build-cache.lock")

    monkeypatch.setattr(cache_gc.shutil, "disk_usage", lambda p: _usage(100, 5))  # always low

    holder = open(lock_path, "a+")
    fcntl.flock(holder, fcntl.LOCK_SH)  # simulates an in-flight verify run
    try:
        evicted = cache_gc.evict_lru_children_if_low_on_space(str(root), lock_path, low_water=0.15, high_water=0.30)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert evicted == []
    assert (root / "m2").is_dir()


def test_evict_lru_children_deletes_oldest_first_when_idle(tmp_path, monkeypatch):
    root = tmp_path / "build-cache"
    root.mkdir()
    old_child = root / "m2"
    new_child = root / "go"
    old_child.mkdir()
    new_child.mkdir()
    now = time.time()
    os.utime(old_child, (now - 1000, now - 1000))
    os.utime(new_child, (now, now))
    lock_path = str(root / ".build-cache.lock")

    calls = {"n": 0}

    def fake_disk_usage(path):
        calls["n"] += 1
        return _usage(100, 10) if calls["n"] <= 2 else _usage(100, 50)

    monkeypatch.setattr(cache_gc.shutil, "disk_usage", fake_disk_usage)

    evicted = cache_gc.evict_lru_children_if_low_on_space(str(root), lock_path, low_water=0.15, high_water=0.30)

    assert evicted == ["m2"]
    assert not old_child.exists()
    assert new_child.exists()
