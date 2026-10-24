"""The one way this codebase runs untrusted Python: Monty, out of process.

Monty 1.x runs every snippet in a ``monty`` worker subprocess. A snippet that
exhausts its memory or time limit, or crashes the interpreter, takes down only
that worker; the calling process (the bot, the agent worker) is unharmed.

Each run gets its own one-worker pool, so the worker exits when the run ends
and its memory goes back to the OS: a worker that peaked at the memory limit
never lingers. Spawning one costs about 5 ms.

Member-written code is bounded per process by ``MAX_CONCURRENT_RUNS``. Every
worker can grow to ``max_memory`` plus Monty's own ~10 MiB, so this bound is
what keeps a burst of calls from adding several of those to the host pod's
memory at once. A caller past the bound waits for a free place. Handler
scripts pass ``bounded=False``: they are written by server admins, already
run at most one per worker job slot, and hold their worker while they await
Discord, so sharing two places with chat would stall every handler fire.

The sandbox has no network, no filesystem (``open``/``os``/``pathlib`` raise
``PermissionError``), no environment and no packages; only Monty's bundled
modules import. Sleeps return at once (``sleep: zero``), so a run's wall time
is its execution time and ``max_feed_duration_secs`` bounds it; the pool's
``request_timeout`` kills a worker that overruns that by more than
``KILL_GRACE_SECONDS``.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

import pydantic_monty as monty

# Workers that may run at once in one process. Each can reach max_memory plus
# ~10 MiB, so this is the most code execution adds to a pod's memory.
MAX_CONCURRENT_RUNS = 2

# Past a feed's time limit, how long the pool waits for the sandbox to raise
# TimeoutError itself before killing the worker.
KILL_GRACE_SECONDS = 2.0

# Sleeps return immediately: a sleeping snippet would otherwise hold a worker
# without counting against its time limit.
OS_POLICY: monty.OSPolicy = {"sleep": "zero"}

_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = _semaphores[loop] = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
    return semaphore


async def run_sandboxed(
    code: str,
    *,
    limits: Mapping[str, Any],
    print_callback: monty.CollectStreams | Callable[[str, str], None] | None = None,
    inputs: dict[str, Any] | None = None,
    external_lookup: dict[str, Any] | None = None,
    os: monty.OsHandler | None = None,
    bounded: bool = True,
) -> Any:
    """Run ``code`` in a fresh Monty worker and return its trailing value.

    Raises ``monty.MontySyntaxError`` for code that does not parse,
    ``monty.MontyRuntimeError`` for an exception in the snippet (including a
    breached memory, time or recursion limit) and ``monty.MontyCrashedError``
    when the worker died or overran its time limit by the kill grace.
    """
    duration = limits.get("max_feed_duration_secs")
    request_timeout = None if duration is None else duration + KILL_GRACE_SECONDS
    async with _semaphore() if bounded else contextlib.nullcontext():
        async with monty.AsyncMonty(
            min_processes=1,
            max_processes=1,
            request_timeout=request_timeout,
        ) as pool:
            async with pool.checkout(
                limits=dict(limits), os_policy=OS_POLICY
            ) as session:
                return await session.feed_run(
                    code,
                    inputs=inputs,
                    external_lookup=external_lookup,
                    print_callback=print_callback,
                    os=os,
                )
