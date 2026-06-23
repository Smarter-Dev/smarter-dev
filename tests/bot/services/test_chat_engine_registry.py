"""Tests for ``ChatEngineRegistry`` — get-or-create + stale-engine handling.

The key regression covered here: after the 30-minute inactivity window, the
first @mention used to be swallowed by the stale engine's lazy deactivation,
forcing users to mention the bot a second time to wake it. The registry now
treats an expired engine as gone and hands back a fresh one immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smarter_dev.bot.services.chat_engine import INACTIVITY_TIMEOUT
from smarter_dev.bot.services.chat_engine_registry import ChatEngineRegistry


@pytest.fixture
def fake_memory():
    """Stand-in ChatMemory — only the methods ``expire`` reaches."""
    m = MagicMock()
    m.clear_notes = AsyncMock()
    m.clear_history = AsyncMock()
    return m


@pytest.fixture
def fake_bot():
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    return bot


async def _noop_voice(channel_id, text, reply_to, instruction=None):
    pass


@pytest.mark.asyncio
async def test_ensure_engine_reuses_active_engine(fake_bot):
    """A second mention inside the window reuses the same engine."""
    registry = ChatEngineRegistry()
    first = await registry.ensure_engine(
        bot=fake_bot, channel_id=42, guild_id=99, voice_send=_noop_voice
    )
    second = await registry.ensure_engine(
        bot=fake_bot, channel_id=42, guild_id=99, voice_send=_noop_voice
    )
    assert second is first
    assert await registry.has_active(42) is True
    await first.shutdown()


@pytest.mark.asyncio
async def test_expired_engine_is_replaced_on_next_mention(fake_bot, fake_memory):
    """Regression: the first mention after the inactivity window must start a
    fresh engagement rather than be consumed by the stale engine's teardown."""
    registry = ChatEngineRegistry()
    with patch(
        "smarter_dev.bot.services.chat_engine.get_chat_memory",
        return_value=fake_memory,
    ), patch(
        "smarter_dev.bot.services.chat_engine.end_engagement",
        new=AsyncMock(),
    ):
        first = await registry.ensure_engine(
            bot=fake_bot, channel_id=42, guild_id=99, voice_send=_noop_voice
        )
        # Simulate 30+ minutes since the engine last spoke.
        first.last_sent_at = (
            datetime.now(UTC) - INACTIVITY_TIMEOUT - timedelta(seconds=1)
        )

        assert first.is_expired is True
        # The mention plugin's gate now sees the stale engine as inactive...
        assert await registry.has_active(42) is False

        # ...so ensure_engine tears it down and returns a brand-new engine that
        # responds on this very first mention.
        second = await registry.ensure_engine(
            bot=fake_bot, channel_id=42, guild_id=99, voice_send=_noop_voice
        )
        assert second is not first
        assert first.active is False
        assert second.active is True
        assert second.is_expired is False
        assert await registry.has_active(42) is True
        # Stale engagement was cleaned up exactly once.
        fake_memory.clear_history.assert_awaited()

        await second.shutdown()
        await first.shutdown()
