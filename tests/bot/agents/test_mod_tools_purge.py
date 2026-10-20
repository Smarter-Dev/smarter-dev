"""Regression tests for mod_tools.purge_messages after extracting the shared
paging/bulk-delete core into purge_core. Behavior must be unchanged."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock
from unittest.mock import patch

from smarter_dev.bot import moderation_port
from smarter_dev.bot.agents import mod_tools
from smarter_dev.bot.purge_core import DISCORD_EPOCH_MS
from tests.bot.fake_discord import GUILD_ID
from tests.bot.fake_discord import FakeDiscord


def _snowflake_for(dt: datetime) -> int:
    return (int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


async def _no_staff_roles(guild_id):
    return ()


async def _allowed(guild_id, tool):
    return None


def _get_purge_tool(bot):
    tools, tracker = mod_tools.create_moderation_tools(
        bot,
        guild_id=str(GUILD_ID),
        channel_id="555",
        trigger_message_id=None,
        enabled_tools=["purge"],
        config_gate=_allowed,
        staff_roles=_no_staff_roles,
    )
    purge = next(t.function for t in tools if t.name == "purge_messages")
    return purge, tracker


async def test_purge_messages_filters_user_and_bulk_deletes():
    now = datetime.now(UTC)
    recent = _snowflake_for(now - timedelta(minutes=1))
    old = _snowflake_for(now - timedelta(days=20))
    discord = FakeDiscord()
    discord.add_member(222, username="Target")
    discord.add_member(999)
    # Oldest first; the fake serves history newest first, as Discord does.
    discord.add_message(old + 1, 222)  # too old, skipped
    discord.add_message(recent + 1, 222)
    discord.add_message(recent + 2, 999)  # different author, skipped
    discord.add_message(recent + 3, 222)

    purge, tracker = _get_purge_tool(discord)

    with patch.object(moderation_port, "dispatch_mod_action", AsyncMock()):
        result = await purge("222", 5, "spam")

    assert result["success"] is True
    assert discord.destructive == [("delete_messages", [recent + 3, recent + 1])]
    assert tracker.purges[0]["count"] == 2


async def test_purge_messages_reports_when_no_recent_messages():
    discord = FakeDiscord()
    discord.add_member(222, username="Target")

    purge, tracker = _get_purge_tool(discord)
    result = await purge("222", 5, "spam")

    assert result["success"] is True
    assert "No recent messages" in result["result"]
    assert discord.destructive == []
