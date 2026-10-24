"""The one way this codebase runs untrusted Python: Monty, out of process.

Monty 1.x runs every snippet in a ``monty`` worker subprocess. A snippet that
exhausts its memory or time limit, or crashes the interpreter, takes down only
that worker; the calling process (the bot, the agent worker) is unharmed.

Each run gets its own one-worker pool, so the worker exits when the run ends
and its memory goes back to the OS: a worker that peaked at the memory limit
never lingers. Spawning one costs about 5 ms. A run gives its place in the
budget back only once its worker has exited: closing the pool doesn't wait
for a worker whose run was cancelled (see ``_worker_exited``).

**Memory.** A worker is a real process in the pod's cgroup, so every running
sandbox counts against the pod's memory limit. Each process has a sandbox
memory budget, ``CODE_SANDBOX_BUDGET_MIB`` (set per deployment in ``k8s/``,
sized from the pod's limit and its own measured use). A run reserves its
memory cap plus ``WORKER_OVERHEAD_MIB`` before its worker starts, and waits,
first come first served, until that fits. A run's cap is what the caller asks
for (``max_memory``), cut to what the budget can ever hold, and Monty enforces
it inside the worker: past it the snippet gets ``MemoryError``. So the
sandboxes running in a process never add more than the budget to its pod.

The sandbox has no network, no filesystem (``open``/``os``/``pathlib`` raise
``PermissionError``), no environment and no packages; only Monty's bundled
modules import. Sleeps return at once (``sleep: zero``), so a run's wall time
is its execution time and ``max_feed_duration_secs`` bounds it; the pool's
``request_timeout`` kills a worker that overruns that by more than
``KILL_GRACE_SECONDS``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import math
import os as _os
import signal
import weakref
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

import pydantic_monty as monty

MIB = 1024 * 1024

# What a worker holds beyond its memory cap: Monty's own runtime. Measured on
# 1.3.0 at caps of 16 to 256 MiB, a worker that hit its cap peaked at most
# 10.5 MiB over it (tests/shared/code_sandbox_test.py checks this bound).
WORKER_OVERHEAD_MIB = 16

# A process that doesn't say: two runs at chat's 64 MiB cap. Every
# deployment that runs sandboxes sets its own (k8s/).
DEFAULT_BUDGET_MIB = 160
BUDGET_ENV = "CODE_SANDBOX_BUDGET_MIB"

# Past a feed's time limit, how long the pool waits for the sandbox to raise
# TimeoutError itself before killing the worker.
KILL_GRACE_SECONDS = 2.0

# Once its pool is closed, how long a run's worker gets to exit before it is
# killed. Its place in the budget is held until it has gone.
WORKER_EXIT_SECONDS = 2.0

# Sleeps return immediately: a sleeping snippet would otherwise hold a worker
# without counting against its time limit.
OS_POLICY: monty.OSPolicy = {"sleep": "zero"}


class RunNotWanted(Exception):
    """The run's caller gave up while it waited for a place; nothing ran."""


def budget_mib() -> int:
    """This process's sandbox memory budget, in MiB."""
    raw = _os.environ.get(BUDGET_ENV, "")
    try:
        budget = int(raw) if raw else DEFAULT_BUDGET_MIB
    except ValueError:
        budget = DEFAULT_BUDGET_MIB
    # Room for at least one small run.
    return max(budget, WORKER_OVERHEAD_MIB + 16)


def memory_cap_mib(requested_bytes: int | None, budget: int) -> int:
    """A run's memory cap: what was asked, cut to what the budget can hold."""
    most = budget - WORKER_OVERHEAD_MIB
    if requested_bytes is None:
        return most
    return max(1, min(math.ceil(requested_bytes / MIB), most))


class _Budget:
    """MiB of sandbox memory one event loop may have running at once.

    Reservations are granted in arrival order: a large run waiting at the
    head isn't passed by smaller ones, so it can't wait forever.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.used = 0
        self._waiting: collections.deque[tuple[int, asyncio.Future[None]]] = (
            collections.deque()
        )

    def _grant(self) -> None:
        while self._waiting:
            size, waiter = self._waiting[0]
            if waiter.done():
                self._waiting.popleft()
                continue
            if self.used + size > self.total:
                return
            self._waiting.popleft()
            self.used += size
            waiter.set_result(None)

    @contextlib.asynccontextmanager
    async def reserve(self, size: int) -> AsyncIterator[None]:
        waiter = asyncio.get_running_loop().create_future()
        self._waiting.append((size, waiter))
        self._grant()
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                self.used -= size  # granted just as it was cancelled
            waiter.cancel()
            self._grant()
            raise
        try:
            yield
        finally:
            self.used -= size
            self._grant()


_budgets: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Budget] = (
    weakref.WeakKeyDictionary()
)


def _watch_worker(pid: int | None) -> int | None:
    """A pidfd for the run's worker, readable once it has exited.

    ``None`` where the OS has no pidfds (not Linux): there the run doesn't
    wait for its worker.
    """
    if pid is None or not hasattr(_os, "pidfd_open"):
        return None
    try:
        return _os.pidfd_open(pid)
    except ProcessLookupError:
        return None


async def _worker_exited(pidfd: int) -> None:
    """Return once the worker behind ``pidfd`` has exited; close ``pidfd``.

    pydantic-monty 1.3.0 doesn't wait for every worker it ends. Closing the
    pool waits only for idle ones; a worker whose run was cancelled mid-feed
    is sent SIGKILL as its checkout is dropped and left for tokio to reap in
    the background (monty-python ``pool.rs`` ``AbandonGuard``, monty-pool
    ``checkout.rs`` ``impl Drop for Checkout``, ``pool.rs`` ``Pool::close``).
    Under load it can run on for a while after that, holding its memory.

    A worker that hasn't exited within ``WORKER_EXIT_SECONDS`` is killed and
    waited for. A cancellation meanwhile is held until the worker has gone,
    then raised. The zombie holds no memory; tokio, which owns the child,
    reaps it.
    """
    loop = asyncio.get_running_loop()
    gone = loop.create_future()
    loop.add_reader(pidfd, lambda: gone.done() or gone.set_result(None))
    deadline: float | None = loop.time() + WORKER_EXIT_SECONDS
    cancelled: asyncio.CancelledError | None = None
    try:
        while not gone.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(gone),
                    None if deadline is None else deadline - loop.time(),
                )
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                deadline = None
            except asyncio.CancelledError as error:
                cancelled = error
    finally:
        loop.remove_reader(pidfd)
        _os.close(pidfd)
    if cancelled is not None:
        raise cancelled


def _budget() -> _Budget:
    loop = asyncio.get_running_loop()
    budget = _budgets.get(loop)
    if budget is None:
        budget = _budgets[loop] = _Budget(budget_mib())
    return budget


async def run_sandboxed(
    code: str,
    *,
    limits: Mapping[str, Any],
    print_callback: monty.CollectStreams | Callable[[str, str], None] | None = None,
    inputs: dict[str, Any] | None = None,
    external_lookup: dict[str, Any] | None = None,
    os: monty.OsHandler | None = None,
    still_wanted: Callable[[], Awaitable[bool]] | None = None,
) -> Any:
    """Run ``code`` in a fresh Monty worker and return its trailing value.

    ``still_wanted``, when given, is asked once a place in the budget is
    held and before any worker starts; if it says no, nothing runs and
    ``RunNotWanted`` is raised.

    Raises ``monty.MontySyntaxError`` for code that does not parse,
    ``monty.MontyRuntimeError`` for an exception in the snippet (including a
    breached memory, time or recursion limit) and ``monty.MontyCrashedError``
    when the worker died or overran its time limit by the kill grace.
    """
    budget = _budget()
    cap = memory_cap_mib(limits.get("max_memory"), budget.total)
    limits = {**limits, "max_memory": cap * MIB}
    duration = limits.get("max_feed_duration_secs")
    request_timeout = None if duration is None else duration + KILL_GRACE_SECONDS
    async with budget.reserve(cap + WORKER_OVERHEAD_MIB):
        if still_wanted is not None and not await still_wanted():
            raise RunNotWanted
        # Leaving the pool ends its worker, a cancelled run's included, and
        # the place is held until that worker has exited
        # (tests/shared/code_sandbox_test.py and
        # tests/web/site_code_lifecycle_test.py check none is left alive when
        # the place is given back).
        worker = None
        try:
            async with monty.AsyncMonty(
                min_processes=1,
                max_processes=1,
                request_timeout=request_timeout,
            ) as pool:
                async with pool.checkout(limits=limits, os_policy=OS_POLICY) as session:
                    worker = _watch_worker(session.worker_pid)
                    return await session.feed_run(
                        code,
                        inputs=inputs,
                        external_lookup=external_lookup,
                        print_callback=print_callback,
                        os=os,
                    )
        finally:
            if worker is not None:
                await _worker_exited(worker)
