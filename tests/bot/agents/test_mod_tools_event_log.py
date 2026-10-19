"""The AI triage tools' contribution to the bot's short-term event log.

The single-message delete is the one triage action that writes no
``ModerationAction`` row, so it is captured directly instead of riding the
``mod_action`` dispatch the other tools go through.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smarter_dev.bot.agents import mod_tools
from tests.bot.fake_discord import GUILD_ID as FAKE_GUILD_ID
from tests.bot.fake_discord import FakeDiscord
from tests.bot.fake_discord import not_found

GUILD_ID = str(FAKE_GUILD_ID)
CHANNEL_ID = "555"
MESSAGE_ID = "777"
TARGET_ID = 222


async def _allowed(guild_id, tool):
    return None


def _tool(bot, name, enabled):
    tools, tracker = mod_tools.create_moderation_tools(
        bot,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        trigger_message_id=None,
        enabled_tools=enabled,
        config_gate=_allowed,
    )
    return next(t.function for t in tools if t.name == name), tracker


@pytest.fixture
def discord() -> FakeDiscord:
    discord = FakeDiscord()
    discord.add_member(TARGET_ID, username="target")
    discord.add_message(int(MESSAGE_ID), TARGET_ID, content="scam link")
    return discord


@pytest.fixture
def recorded_events(monkeypatch) -> list:
    events: list = []

    async def capture_event(bot, event, **kwargs) -> None:
        events.append(event)

    monkeypatch.setattr(mod_tools, "record_guild_event", capture_event)
    return events


async def test_single_delete_is_written_to_the_event_log(discord, recorded_events):
    delete_message, _ = _tool(discord, "delete_message", ["delete"])
    result = await delete_message(MESSAGE_ID, "posted a scam link")

    assert result["success"] is True
    assert discord.destructive == [("delete_message", int(MESSAGE_ID))]
    assert len(recorded_events) == 1
    event = recorded_events[0]
    assert event.kind == "mod_action"
    assert event.guild_id == GUILD_ID
    assert event.action == "delete"
    assert event.channel_id == CHANNEL_ID
    assert event.reason == "posted a scam link"
    assert event.source == "ai"


async def test_a_delete_that_never_happened_is_never_remembered(discord, recorded_events):
    discord.fail["delete_message"] = not_found()

    delete_message, tracker = _tool(discord, "delete_message", ["delete"])
    result = await delete_message(MESSAGE_ID, "posted a scam link")

    assert result["success"] is False
    assert recorded_events == []
    # A 404 after the call went out may follow an earlier attempt that landed.
    assert tracker.deletions == [
        {"message_id": MESSAGE_ID, "reason": "posted a scam link", "possibly_applied": True}
    ]


async def test_triage_timeout_dispatches_with_the_bot_for_the_event_log(discord, monkeypatch):
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

    timeout_user, _ = _tool(discord, "timeout_user", ["timeout"])
    result = await timeout_user(str(TARGET_ID), "10m", "invite-link spam")

    assert result["success"] is True
    assert len(dispatched) == 1
    assert dispatched[0][1] is discord
