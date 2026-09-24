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


class FakeBot:
    """Stands in for the lightbulb app: records listeners, start and close."""

    def __init__(self, start_blocks: bool) -> None:
        self.start_blocks = start_blocks
        self.started = False
        self.closed = False
        self.calls: list[str] = []

    def listen(self, *_args, **_kwargs):
        return lambda func: func

    async def start(self) -> None:
        if self.start_blocks:
            await asyncio.Event().wait()
        self.started = True

    async def close(self) -> None:
        self.closed = True
        self.calls.append("close")


class FakeCoordinator:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run(self) -> None:
        self.calls.append("contend")
        await asyncio.Event().wait()

    async def stop_acting(self) -> None:
        self.calls.append("stop_acting")


class FakeHealthRunner:
    def __init__(self) -> None:
        self.cleaned_up = False

    async def cleanup(self) -> None:
        self.cleaned_up = True


def _patch_run_bot(monkeypatch: pytest.MonkeyPatch, bot: FakeBot) -> FakeHealthRunner:
    from types import SimpleNamespace

    from smarter_dev.bot import client

    runner = FakeHealthRunner()

    async def fake_setup(_bot) -> None:
        return None

    async def fake_health(_bot, _port) -> FakeHealthRunner:
        return runner

    settings = SimpleNamespace(
        discord_bot_token="t",
        discord_application_id="1",
        bot_api_key="k",
        bot_health_port=0,
    )
    monkeypatch.setattr(client, "get_settings", lambda: settings)
    monkeypatch.setattr(client, "create_bot", lambda _settings: bot)
    monkeypatch.setattr(client, "setup_bot_services", fake_setup)
    monkeypatch.setattr(client, "load_plugins", lambda _bot: None)
    monkeypatch.setattr(client, "start_health_server", fake_health)
    monkeypatch.setattr(
        client,
        "create_coordination",
        lambda _bot, _settings: (FakeCoordinator(bot.calls), object()),
    )

    async def fake_drain(_gate, _budget) -> None:
        bot.calls.append("drain")

    monkeypatch.setattr(client, "drain_accepted_work", fake_drain)
    return runner


async def _sigterm_after(delay: float) -> None:
    await asyncio.sleep(delay)
    os.kill(os.getpid(), signal.SIGTERM)


async def _remove_handlers() -> None:
    loop = asyncio.get_running_loop()
    loop.remove_signal_handler(signal.SIGTERM)
    loop.remove_signal_handler(signal.SIGINT)


async def test_sigterm_while_running_closes_the_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from smarter_dev.bot.client import run_bot

    bot = FakeBot(start_blocks=False)
    runner = _patch_run_bot(monkeypatch, bot)
    killer = asyncio.create_task(_sigterm_after(0.2))
    try:
        await asyncio.wait_for(asyncio.create_task(run_bot()), timeout=3)
    finally:
        await killer
        await _remove_handlers()
    assert bot.started
    assert runner.cleaned_up
    assert bot.closed
    # Hand over first, so the standby acts while this process drains.
    assert bot.calls == ["contend", "stop_acting", "drain", "close"]


async def test_sigterm_during_startup_still_closes_the_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from smarter_dev.bot.client import run_bot

    bot = FakeBot(start_blocks=True)
    _patch_run_bot(monkeypatch, bot)
    killer = asyncio.create_task(_sigterm_after(0.2))
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.create_task(run_bot()), timeout=3)
    finally:
        await killer
        await _remove_handlers()
    assert not bot.started
    assert bot.closed
    assert "contend" not in bot.calls
