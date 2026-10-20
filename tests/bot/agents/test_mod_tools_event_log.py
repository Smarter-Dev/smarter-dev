"""The AI triage tools' contribution to the bot's short-term event log.

Every triage action goes through the moderation gateway, which writes one
``ModerationAction`` row per confirmed effect (the single delete included) and
announces it once through the ``mod_action`` dispatch, with the bot, which is
what writes the event log. Nothing that did not happen is announced.
"""

from __future__ import annotations

import pytest

from smarter_dev.bot.agents import mod_tools
from tests.bot.fake_discord import GUILD_ID as FAKE_GUILD_ID
from tests.bot.fake_discord import FakeDiscord
from tests.bot.fake_discord import not_found

GUILD_ID = str(FAKE_GUILD_ID)
CHANNEL_ID = "555"
MESSAGE_ID = "777"
TARGET_ID = 222


async def _no_staff_roles(guild_id):
    return ()


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
        staff_roles=_no_staff_roles,
    )
    return next(t.function for t in tools if t.name == name), tracker


@pytest.fixture
def discord() -> FakeDiscord:
    discord = FakeDiscord()
    discord.add_member(TARGET_ID, username="target")
    discord.add_message(int(MESSAGE_ID), TARGET_ID, content="scam link")
    return discord


@pytest.fixture
def announced(monkeypatch) -> list:
    from smarter_dev.bot import moderation_port

    actions: list = []

    async def capture(action, *, bot=None) -> None:
        actions.append((action, bot))

    monkeypatch.setattr(moderation_port, "dispatch_mod_action", capture)
    return actions


async def test_single_delete_is_recorded_and_announced(discord, announced):
    delete_message, _ = _tool(discord, "delete_message", ["delete"])
    result = await delete_message(MESSAGE_ID, "posted a scam link")

    assert result["success"] is True
    assert discord.destructive == [("delete_message", int(MESSAGE_ID))]
    [(action, bot)] = announced
    assert bot is discord
    assert action.action_type == "delete"
    assert action.guild_id == GUILD_ID
    assert action.channel_id == CHANNEL_ID
    assert action.reason == "posted a scam link"
    assert action.source == "ai"


async def test_a_message_already_gone_is_never_remembered(discord, announced):
    discord.fail["delete_message"] = not_found()

    delete_message, tracker = _tool(discord, "delete_message", ["delete"])
    result = await delete_message(MESSAGE_ID, "posted a scam link")

    # Gone is what the tool wanted, but the bot did not do it: no row, no claim.
    assert result["success"] is True
    assert "already deleted" in result["result"]
    assert announced == []
    assert tracker.deletions == []


async def test_triage_timeout_dispatches_with_the_bot_for_the_event_log(
    discord, announced
):
    timeout_user, _ = _tool(discord, "timeout_user", ["timeout"])
    result = await timeout_user(str(TARGET_ID), "10m", "invite-link spam")

    assert result["success"] is True
    [(action, bot)] = announced
    assert action.action_type == "timeout"
    assert bot is discord
