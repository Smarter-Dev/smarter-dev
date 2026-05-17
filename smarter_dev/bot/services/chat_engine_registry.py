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
            return engine is not None and engine.active

    async def ensure_engine(
        self,
        *,
        bot: Any,
        channel_id: int,
        guild_id: int,
        voice_send: Callable[[int, str, int | None], Awaitable[None]],
    ) -> ChannelEngine:
        """Return the active engine for the channel, creating it if needed."""
        async with self._lock:
            engine = self._engines.get(channel_id)
            if engine is not None and engine.active:
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
