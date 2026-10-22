import asyncio
import os

import structlog

logger = structlog.get_logger()

REAP_CHECK_SECONDS = float(os.getenv("SUBPROCESS_REAP_CHECK_SECONDS", "5"))


def is_zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return False
    end = data.rfind(b")")
    return end != -1 and data[end + 2:end + 3] == b"Z"


def reap(proc) -> bool:
    transport = getattr(proc, "_transport", None)
    exited = getattr(transport, "_process_exited", None)
    if exited is None or transport.get_returncode() is not None:
        return False
    try:
        pid, status = os.waitpid(proc.pid, os.WNOHANG)
    except ChildProcessError:
        return False
    if pid == 0:
        return False
    exited(os.waitstatus_to_exitcode(status))
    return True


async def watched(proc, awaitable, *, check_seconds: float | None = None):
    task = asyncio.ensure_future(awaitable)
    interval = REAP_CHECK_SECONDS if check_seconds is None else check_seconds
    suspected = False
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                return task.result()
            if not is_zombie(proc.pid):
                suspected = False
                continue
            if suspected and reap(proc):
                logger.warning("subprocess_exit_recovered", pid=proc.pid)
            suspected = True
    finally:
        if not task.done():
            task.cancel()


async def communicate(proc, *, check_seconds: float | None = None):
    return await watched(proc, proc.communicate(), check_seconds=check_seconds)


async def wait(proc, *, check_seconds: float | None = None):
    return await watched(proc, proc.wait(), check_seconds=check_seconds)
