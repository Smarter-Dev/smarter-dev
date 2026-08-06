"""The AI triage tools' contribution to the bot's short-term event log.

The single-message delete is the one triage action that writes no
``ModerationAction`` row, so it is captured directly instead of riding the
``mod_action`` dispatch the other tools go through.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import hikari
import pytest

from smarter_dev.bot.agents import mod_tools

GUILD_ID = "111"
CHANNEL_ID = "555"
MESSAGE_ID = "777"


def _delete_tool(bot):
    tools, tracker = mod_tools.create_moderation_tools(
        bot,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        trigger_message_id=None,
        enabled_tools=["delete"],
    )
    delete_message = next(t for t in tools if t.__name__ == "delete_message")
    return delete_message, tracker


@pytest.fixture
def recorded_events(monkeypatch) -> list:
    events: list = []

    async def capture_event(bot, event, **kwargs) -> None:
        events.append(event)

    monkeypatch.setattr(mod_tools, "record_guild_event", capture_event)
    return events


@pytest.mark.asyncio
async def test_single_delete_is_written_to_the_event_log(recorded_events):
    bot = Mock()
    bot.rest = Mock()
    bot.rest.delete_message = AsyncMock()

    delete_message, _ = _delete_tool(bot)
    result = await delete_message(MESSAGE_ID, "posted a scam link")

    assert result["success"] is True
    assert len(recorded_events) == 1
    event = recorded_events[0]
    assert event.kind == "mod_action"
    assert event.guild_id == GUILD_ID
    assert event.action == "delete"
    assert event.channel_id == CHANNEL_ID
    assert event.reason == "posted a scam link"
    assert event.source == "ai"


@pytest.mark.asyncio
async def test_a_delete_that_never_happened_is_never_remembered(recorded_events):
    bot = Mock()
    bot.rest = Mock()
    bot.rest.delete_message = AsyncMock(
        side_effect=hikari.NotFoundError(
            url="url", headers={}, raw_body=b"", message="gone"
        )
    )

    delete_message, _ = _delete_tool(bot)
    result = await delete_message(MESSAGE_ID, "posted a scam link")

    assert result["success"] is False
    assert recorded_events == []


@pytest.mark.asyncio
async def test_triage_timeout_dispatches_with_the_bot_for_the_event_log(monkeypatch):
    """Actions that DO write a row reach the log through the dispatch instead."""
    dispatched: list = []

    async def capture_dispatch(action, *, bot=None) -> None:
        dispatched.append((action, bot))

    class _FakeSessionContext:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(mod_tools, "dispatch_mod_action", capture_dispatch)
    monkeypatch.setattr(
        mod_tools, "get_db_session_context", lambda: _FakeSessionContext()
    )
    monkeypatch.setattr(
        mod_tools.mod_action_ops,
        "create_action",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        mod_tools.lightbulb.utils,
        "permissions_for",
        lambda member: hikari.Permissions.NONE,
    )

    bot = Mock()
    bot.rest = Mock()
    bot.rest.fetch_member = AsyncMock(
        return_value=SimpleNamespace(
            display_name="Target",
            username="target",
            user=SimpleNamespace(fetch_dm_channel=AsyncMock(return_value=9)),
        )
    )
    bot.rest.create_message = AsyncMock()
    bot.rest.edit_member = AsyncMock()
    bot.get_me = Mock(return_value=SimpleNamespace(id=1))

    tools, _ = mod_tools.create_moderation_tools(
        bot, guild_id=GUILD_ID, channel_id=CHANNEL_ID, trigger_message_id=None
    )
    timeout_user = next(t for t in tools if t.__name__ == "timeout_user")

    result = await timeout_user("222", "10m", "invite-link spam")

    assert result["success"] is True
    assert len(dispatched) == 1
    assert dispatched[0][1] is bot
