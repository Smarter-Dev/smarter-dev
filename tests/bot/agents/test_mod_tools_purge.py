"""Regression tests for mod_tools.purge_messages after extracting the shared
paging/bulk-delete core into purge_core. Behavior must be unchanged."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from smarter_dev.bot.agents import mod_tools
from smarter_dev.bot.purge_core import DISCORD_EPOCH_MS


def _snowflake_for(dt: datetime) -> int:
    return (int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def limit(self, n):
        return _AsyncIter(self._items[:n])

    def __aiter__(self):
        async def _gen():
            for item in self._items:
                yield item

        return _gen()


def _message(message_id: int, author_id):
    return SimpleNamespace(id=message_id, author=SimpleNamespace(id=author_id))


def _get_purge_tool(bot):
    tools, tracker = mod_tools.create_moderation_tools(
        bot, guild_id="111", channel_id="555", trigger_message_id=None
    )
    purge = next(t for t in tools if t.__name__ == "purge_messages")
    return purge, tracker


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_purge_messages_filters_user_and_bulk_deletes():
    now = datetime.now(timezone.utc)
    recent = _snowflake_for(now - timedelta(minutes=1))
    old = _snowflake_for(now - timedelta(days=20))
    messages = [
        _message(recent + 3, 222),
        _message(recent + 2, 999),  # different author, skipped
        _message(old + 1, 222),     # too old, skipped
        _message(recent + 1, 222),
    ]

    bot = Mock()
    bot.rest = Mock()
    member = SimpleNamespace(display_name="Target", username="target")
    bot.rest.fetch_member = AsyncMock(return_value=member)
    bot.rest.fetch_messages = Mock(return_value=_AsyncIter(messages))
    bot.rest.delete_messages = AsyncMock()
    bot.rest.delete_message = AsyncMock()

    purge, tracker = _get_purge_tool(bot)

    session = AsyncMock()
    with patch.object(mod_tools, "get_db_session_context", return_value=_FakeSessionCtx(session)), \
        patch.object(mod_tools.mod_action_ops, "create_action", AsyncMock(return_value=SimpleNamespace())), \
        patch.object(mod_tools, "dispatch_mod_action", AsyncMock()):
        result = await purge("222", 5, "spam")

    assert result["success"] is True
    bot.rest.delete_messages.assert_awaited_once_with(555, [recent + 3, recent + 1])
    assert tracker.purges[0]["count"] == 2


@pytest.mark.asyncio
async def test_purge_messages_reports_when_no_recent_messages():
    bot = Mock()
    bot.rest = Mock()
    member = SimpleNamespace(display_name="Target", username="target")
    bot.rest.fetch_member = AsyncMock(return_value=member)
    bot.rest.fetch_messages = Mock(return_value=_AsyncIter([]))
    bot.rest.delete_messages = AsyncMock()
    bot.rest.delete_message = AsyncMock()

    purge, tracker = _get_purge_tool(bot)
    result = await purge("222", 5, "spam")

    assert result["success"] is True
    assert "No recent messages" in result["result"]
    bot.rest.delete_messages.assert_not_awaited()
    bot.rest.delete_message.assert_not_awaited()
