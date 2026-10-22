import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common import subprocess_watch  # noqa: E402

posix_only = pytest.mark.skipif(not hasattr(os, "posix_spawn") or not Path("/proc").is_dir(), reason="needs /proc and posix_spawn")


class _Transport:
    def __init__(self, future):
        self.future = future
        self.returncode = None

    def get_returncode(self):
        return self.returncode

    def _process_exited(self, returncode):
        self.returncode = returncode
        self.future.set_result(returncode)


class _LostExit:
    def __init__(self, pid):
        self.pid = pid
        self._transport = _Transport(asyncio.get_running_loop().create_future())

    def wait(self):
        return self._transport.future


def _spawn(code: str) -> int:
    return os.posix_spawn(sys.executable, [sys.executable, "-c", code], dict(os.environ))


async def _until_zombie(pid: int) -> None:
    for _ in range(200):
        if subprocess_watch.is_zombie(pid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"pid {pid} never became a zombie")


@posix_only
@pytest.mark.asyncio
async def test_a_child_whose_exit_asyncio_never_saw_is_reaped_and_reported():
    pid = _spawn("raise SystemExit(3)")
    await _until_zombie(pid)
    proc = _LostExit(pid)

    code = await asyncio.wait_for(subprocess_watch.wait(proc, check_seconds=0.02), timeout=5)

    assert code == 3
    assert not subprocess_watch.is_zombie(pid)


@posix_only
@pytest.mark.asyncio
async def test_a_child_that_is_still_running_is_left_alone():
    pid = _spawn("import time; time.sleep(30)")
    proc = _LostExit(pid)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(subprocess_watch.wait(proc, check_seconds=0.02), timeout=0.2)
        assert proc._transport.returncode is None
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)


@pytest.mark.asyncio
async def test_a_healthy_child_returns_without_any_reaping():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "print('ok')", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    stdout, _ = await subprocess_watch.communicate(proc, check_seconds=0.02)

    assert stdout.strip() == b"ok"
    assert proc.returncode == 0


def test_reap_declines_without_a_transport_it_can_notify():
    class _Bare:
        pid = os.getpid()

    assert subprocess_watch.reap(_Bare()) is False


def test_an_unknown_pid_is_not_a_zombie():
    assert subprocess_watch.is_zombie(2**22 + 7) is False
