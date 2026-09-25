"""Run a parse or decode in a child Python that cannot outgrow its budget (#25).

Work whose memory has no bound of its own (pdfplumber's layout, decoding a
large image) runs as ``python -m <module> <args>``. The child calls
``limit_memory`` once its imports are done, capping its address space
(``RLIMIT_AS``) a fixed allowance above what the imports took: past it,
allocation fails inside the child, which exits non-zero, while the parent is
untouched. The child also dies with its parent and has a CPU limit.

``run`` kills and reaps the child before it returns or raises, however the
read ends (timeout, error, cancellation, a second cancel during the kill), so
a child never outlives the caller's ``media_read_slot``.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import resource
import signal
import sys

from smarter_dev.shared.media_reads import release_freed_memory

logger = logging.getLogger(__name__)

# How long a killed child may take to be reaped before the read gives up on it.
REAP_TIMEOUT_SECONDS = 10.0


class ChildTimedOut(TimeoutError):
    """The child outlasted its timeout and was killed."""


async def run(module: str, *args: str, timeout: float) -> tuple[int, bytes]:
    """Run ``python -m module *args``; return its exit code and stdout.

    Raises ``ChildTimedOut`` after ``timeout`` seconds. The child is gone
    (killed if need be, and reaped) when this returns or raises.
    """
    release_freed_memory()  # what the caller dropped, before the child starts
    child = None
    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            module,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(child.communicate(), timeout)
        except TimeoutError as timed_out:
            raise ChildTimedOut(f"{module} took over {timeout:.0f}s") from timed_out
    finally:
        if child is not None and child.returncode is None:
            await _kill_and_reap(child)
    return child.returncode, out


async def _kill_and_reap(child: asyncio.subprocess.Process) -> None:
    """SIGKILL ``child`` and wait for it to exit, through further cancellation.

    A cancel that arrives while waiting is held back until the child is gone,
    then re-raised, so the caller's slot is not released with a child alive.
    """
    try:
        child.kill()
    except ProcessLookupError:
        pass
    reaped = asyncio.ensure_future(child.wait())
    cancelled = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REAP_TIMEOUT_SECONDS
    while not reaped.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.error("bounded_child: child %s not reaped after SIGKILL", child.pid)
            break
        try:
            await asyncio.wait_for(asyncio.shield(reaped), remaining)
        except asyncio.CancelledError:
            cancelled = True
        except TimeoutError:
            pass
    if cancelled:
        raise asyncio.CancelledError


def limit_memory(extra: int, cpu_seconds: int) -> None:
    """In the child, after its imports: cap it at its current size + ``extra``.

    Also makes it die with its parent (``PR_SET_PDEATHSIG``) and stop after
    ``cpu_seconds`` of CPU, in case the parent can no longer kill it.
    """
    with open("/proc/self/status") as status:
        size = next(int(line.split()[1]) for line in status if line.startswith("VmSize"))
    limit = size * 1024 + extra
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
