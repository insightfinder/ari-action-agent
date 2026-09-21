import fcntl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_oncall_agent.tools.build_cache import reserve_build_cache_home  # noqa: E402
from common.settings import get_settings  # noqa: E402


@pytest.fixture
def build_cache_settings(tmp_path, monkeypatch):
    root = tmp_path / "build-cache"
    monkeypatch.setenv("BUILD_CACHE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_reserve_build_cache_home_creates_and_yields_the_root(build_cache_settings):
    async with reserve_build_cache_home() as root:
        assert root == str(build_cache_settings)
        assert build_cache_settings.is_dir()


@pytest.mark.asyncio
async def test_reserve_build_cache_home_holds_a_shared_lock_for_the_duration(build_cache_settings):
    # Regression test for the reader/writer contract eviction depends on
    # (see common/cache_gc.py's evict_lru_children_if_low_on_space): while
    # a verify run is "inside" this context manager, an exclusive lock
    # attempt on the same lock file must fail -- eviction must never be
    # able to sweep the cache out from under an in-flight build.
    async with reserve_build_cache_home() as root:
        lock_path = Path(root) / ".build-cache.lock"
        assert lock_path.exists()
        competing = open(lock_path, "a+")
        try:
            with pytest.raises(OSError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            competing.close()

    # Once the context exits, the lock is released -- a fresh exclusive
    # attempt now succeeds.
    lock_path = build_cache_settings / ".build-cache.lock"
    after = open(lock_path, "a+")
    try:
        fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(after, fcntl.LOCK_UN)
    finally:
        after.close()
