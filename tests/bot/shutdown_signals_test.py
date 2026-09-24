"""SIGTERM handling for the bot process.

The bot starts with ``bot.start()``, so hikari installs no signal handlers.
``install_shutdown_signals`` must turn SIGTERM into a cancellation of the
running task: that is what lets ``run_bot`` reach its ``finally`` and close the
gateway, and what makes SIGTERM work at all when the bot runs as PID 1.
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from smarter_dev.bot.client import install_shutdown_signals


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signal_cancels_the_running_task(sig: signal.Signals) -> None:
    cleaned_up = False

    async def main() -> None:
        nonlocal cleaned_up
        install_shutdown_signals()
        try:
            os.kill(os.getpid(), sig)
            await asyncio.sleep(5)
        finally:
            cleaned_up = True

    task = asyncio.create_task(main())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert cleaned_up
    loop = asyncio.get_running_loop()
    loop.remove_signal_handler(signal.SIGTERM)
    loop.remove_signal_handler(signal.SIGINT)
