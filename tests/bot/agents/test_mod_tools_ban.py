"""The triage's ``ban_user``: new-account scams only, bounded in code.

The age limit, the policy's ban sanction and the guild are checked inside the
tool, so a prompt that is talked around still cannot ban an old account.
"""

from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from smarter_dev.bot.agents import mod_tools
from smarter_dev.shared import moderation_policy as mp
from tests.bot.fake_discord import GUILD_ID as FAKE_GUILD_ID
from tests.bot.fake_discord import FakeDiscord

GUILD_ID = str(FAKE_GUILD_ID)
JEFF_POST = (
    "earn $50k a month, pay me 10% of what you make, DM me on TELEGRAM @x"
)


def _snowflake(age: timedelta) -> int:
    created = datetime.now(timezone.utc) - age
    return (int(created.timestamp() * 1000) - mod_tools.DISCORD_EPOCH_MS) << 22


async def _no_staff_roles(guild_id):
    return ()


async def _allowed(guild_id, tool):
    return None


def _policy(**sanctions) -> mp.PolicyDocument:
    document = mp.default_document()
    return document.model_copy(
        update={"sanctions": document.sanctions.model_copy(update=sanctions)}
    )


def _tools(bot, *, policy=None, guild_id=GUILD_ID, enforcing=True, gate=_allowed):
    async def read(_guild_id):
        return policy if policy is not None else _policy()

    tools, tracker = mod_tools.create_moderation_tools(
        bot,
        guild_id=guild_id,
        channel_id="555",
        trigger_message_id=None,
        enabled_tools=["timeout", "delete"],
        config_gate=gate,
        staff_roles=_no_staff_roles,
        enforcing=enforcing,
        policy_reader=read,
    )
    return {t.name: t.function for t in tools}, tracker


@pytest.fixture
def announced(monkeypatch) -> list:
    from smarter_dev.bot import moderation_port

    actions: list = []

    async def capture(action, *, bot=None) -> None:
        actions.append(action)

    monkeypatch.setattr(moderation_port, "dispatch_mod_action", capture)
    return actions


def _member(discord, age: timedelta) -> int:
    user_id = _snowflake(age)
    discord.add_member(user_id, username="jeff")
    return user_id


async def test_jeff_style_post_from_an_18_hour_account_is_banned(announced):
    discord = FakeDiscord()
    user_id = _member(discord, timedelta(hours=18))
    tools, tracker = _tools(discord)

    result = await tools["ban_user"](str(user_id), "off-platform money pitch via Telegram")

    assert result["success"] is True, result
    assert [c[:2] for c in discord.destructive] == [("ban_user", user_id)]
    assert user_id in discord.bans
    [action] = announced
    assert action.action_type == "ban"
    assert action.reason == "off-platform money pitch via Telegram"
    assert action.source == "ai"
    assert tracker.bans == [
        {
            "user_id": str(user_id),
            "username": "jeff",
            "reason": "off-platform money pitch via Telegram",
        }
    ]
    assert tracker.has_actions
    # The member is told before the ban, with the reason.
    [(_, dm_text)] = [(c[0], c[1]) for c in discord.sent]
    assert "off-platform money pitch" in dm_text and "banned" in dm_text


async def test_an_account_older_than_the_ban_age_is_refused(announced):
    discord = FakeDiscord()
    user_id = _member(discord, timedelta(days=8))
    tools, tracker = _tools(discord)

    result = await tools["ban_user"](str(user_id), "money pitch")

    assert result["success"] is False
    assert "under 7 days" in result["error"] and "time out" in result["error"]
    assert discord.destructive == [] and discord.sent == []
    assert tracker.bans == [] and announced == []
    # The refusal takes no action slot: the fallback timeout still runs.
    timed_out = await tools["timeout_user"](str(user_id), "10m", "money pitch")
    assert timed_out["success"] is True


async def test_the_ban_age_follows_the_policy():
    discord = FakeDiscord()
    user_id = _member(discord, timedelta(days=2))
    tools, _ = _tools(discord, policy=_policy(scam_ban_account_age_days=1))

    result = await tools["ban_user"](str(user_id), "money pitch")

    assert result["success"] is False
    assert discord.destructive == []


async def test_a_ban_the_policy_does_not_allow_is_refused():
    discord = FakeDiscord()
    user_id = _member(discord, timedelta(hours=18))
    tools, tracker = _tools(discord, policy=_policy(allowed=["warn", "timeout", "delete"]))

    result = await tools["ban_user"](str(user_id), "money pitch")

    assert result["success"] is False
    assert "does not allow bans" in result["error"]
    assert discord.destructive == [] and discord.sent == [] and tracker.bans == []


async def test_an_unreadable_policy_never_bans():
    discord = FakeDiscord()
    user_id = _member(discord, timedelta(hours=18))

    async def broken(_guild_id):
        raise RuntimeError("db down")

    tools, _ = mod_tools.create_moderation_tools(
        discord,
        guild_id=GUILD_ID,
        channel_id="555",
        trigger_message_id=None,
        enabled_tools=[],
        config_gate=_allowed,
        staff_roles=_no_staff_roles,
        enforcing=True,
        policy_reader=broken,
    )
    ban_user = next(t.function for t in tools if t.name == "ban_user")

    result = await ban_user(str(user_id), "money pitch")

    assert result["success"] is False
    assert discord.destructive == []


async def test_a_freelancer_is_banned_only_if_the_model_asks_and_age_allows():
    # The tool cannot read intent: a freelancer's post is kept out by the
    # prompt. What the tool guarantees is that a reason is required and an old
    # account is never banned, whatever the post says.
    discord = FakeDiscord()
    user_id = _member(discord, timedelta(days=30))
    tools, _ = _tools(discord)

    result = await tools["ban_user"](str(user_id), "freelancer offering rates")

    assert result["success"] is False
    assert discord.destructive == []
    blank = await tools["ban_user"](str(_member(discord, timedelta(hours=1))), "   ")
    assert blank["success"] is False and "reason" in blank["error"]
    assert discord.destructive == []


async def test_already_banned_is_not_an_error(announced):
    discord = FakeDiscord()
    user_id = _snowflake(timedelta(hours=18))
    discord.bans.add(user_id)  # the coordinator banned them first
    tools, tracker = _tools(discord)

    result = await tools["ban_user"](str(user_id), "money pitch")

    assert result["success"] is True
    assert "already banned" in result["result"].lower()
    assert discord.destructive == [] and discord.sent == []
    assert tracker.bans == [] and announced == []


async def test_a_member_who_left_can_still_be_banned(announced):
    discord = FakeDiscord()
    user_id = _snowflake(timedelta(hours=3))  # not a member
    discord.users[user_id] = SimpleNamespace(
        id=user_id, is_bot=False, username="gone", fetch_dm_channel=None
    )
    tools, _ = _tools(discord)

    result = await tools["ban_user"](str(user_id), "money pitch")

    assert result["success"] is True, result
    assert user_id in discord.bans


async def test_staff_are_never_banned():
    discord = FakeDiscord()
    tools, _ = _tools(discord)

    result = await tools["ban_user"](str(discord.owner_id), "money pitch")

    assert result["success"] is False
    assert discord.destructive == []


def test_the_tool_exists_only_when_enforcing_in_the_ban_guild():
    discord = FakeDiscord()
    assert "ban_user" in _tools(discord)[0]
    assert "ban_user" not in _tools(discord, enforcing=False)[0]
    assert "ban_user" not in _tools(discord, guild_id="1")[0]
