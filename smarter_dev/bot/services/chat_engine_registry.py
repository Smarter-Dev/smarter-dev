"""Global registry mapping channel IDs to their active ``ChannelEngine``.

There is at most one engine per channel. ``ensure_engine`` is the entry point
for the mention plugin: it creates and starts an engine on the first
@mention/reply, returns the existing one on subsequent invocations.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from smarter_dev.bot.services.chat_engine import ChannelEngine

logger = logging.getLogger(__name__)


class ChatEngineRegistry:
    """Per-channel registry of active chat engines."""

    def __init__(self) -> None:
        self._engines: dict[int, ChannelEngine] = {}
        self._lock = asyncio.Lock()

    async def get(self, channel_id: int) -> ChannelEngine | None:
        async with self._lock:
            return self._engines.get(channel_id)

    async def has_active(self, channel_id: int) -> bool:
        async with self._lock:
            engine = self._engines.get(channel_id)
            return engine is not None and engine.active and not engine.is_expired

    async def ensure_engine(
        self,
        *,
        bot: Any,
        channel_id: int,
        guild_id: int,
        voice_send: Callable[[int, str, int | None], Awaitable[None]],
    ) -> ChannelEngine:
        """Return the active engine for the channel, creating it if needed.

        An engine that has aged past its inactivity window is treated as gone:
        it is torn down and replaced by a fresh one so the triggering mention
        starts a new engagement rather than being consumed by the stale
        engine's lazy deactivation.
        """
        stale: ChannelEngine | None = None
        async with self._lock:
            engine = self._engines.get(channel_id)
            if engine is not None and engine.active and not engine.is_expired:
                return engine
            if engine is not None and engine.active:
                # Expired but still parked here. Drop it from the registry now so
                # its own deactivation (below) can't pop the replacement we are
                # about to create for this channel.
                stale = engine
                self._engines.pop(channel_id, None)

        if stale is not None:
            try:
                await stale.expire()
            except Exception:
                logger.exception(
                    "Failed to expire stale chat engine for channel %s", channel_id
                )

        async with self._lock:
            engine = self._engines.get(channel_id)
            if engine is not None and engine.active and not engine.is_expired:
                return engine

            new_engine = ChannelEngine(
                bot=bot,
                channel_id=channel_id,
                guild_id=guild_id,
                voice_send=voice_send,
                on_deactivate=self._remove,
            )
            self._engines[channel_id] = new_engine
            new_engine.start()
            logger.info("Created chat engine for channel %s", channel_id)
            return new_engine

    async def _remove(self, channel_id: int) -> None:
        async with self._lock:
            self._engines.pop(channel_id, None)
        logger.info("Removed chat engine for channel %s", channel_id)

    async def fire_queued(self) -> None:
        """Answer what every engine has queued now, without its idle timer."""
        async with self._lock:
            engines = list(self._engines.values())
        for engine in engines:
            if engine.active and engine.queue and not engine.run_lock.locked():
                engine.fire_now()

    async def busy_channels(self) -> list[int]:
        """Channels with a turn running, queued or about to fire."""
        async with self._lock:
            engines = list(self._engines.values())
        return [engine.channel_id for engine in engines if engine.active and not engine.is_idle]

    async def drain(self, timeout: float) -> list[int]:
        """Run every queued turn now and wait for running ones to finish.

        For a process handing over to another: the messages its engines have
        queued were delivered to this process only, so it must answer them
        before it exits. Call before ``shutdown_all``, which drops queued
        messages. Engines activated meanwhile are included. Returns the
        channels still busy when ``timeout`` ran out.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            await self.fire_queued()
            busy = await self.busy_channels()
            if not busy:
                # A finished turn can refire just after releasing its lock;
                # look once more before calling it done.
                await asyncio.sleep(0.05)
                busy = await self.busy_channels()
                if not busy:
                    return []
            if loop.time() >= deadline:
                return busy
            await asyncio.sleep(0.1)

    async def abandon(self, channel_ids: list[int]) -> None:
        """Cancel turns still running in these channels; their replies are lost.

        ``shutdown`` waits for a running turn without a bound, so a process
        that has run out of shutdown time must cancel them first.
        """
        async with self._lock:
            engines = [self._engines[c] for c in channel_ids if c in self._engines]
        for engine in engines:
            if engine._runner_task and not engine._runner_task.done():
                engine._runner_task.cancel()

    async def shutdown_all(self) -> None:
        async with self._lock:
            engines = list(self._engines.values())
            self._engines.clear()
        for engine in engines:
            try:
                await engine.shutdown()
            except Exception:
                logger.exception("Error shutting down engine for %s", engine.channel_id)


_registry: ChatEngineRegistry | None = None


def get_chat_engine_registry() -> ChatEngineRegistry:
    """Return the process-global engine registry."""
    global _registry
    if _registry is None:
        _registry = ChatEngineRegistry()
    return _registry
