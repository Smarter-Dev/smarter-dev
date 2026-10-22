"""The bot's staff-protection adapter: cache first, REST second, never raises."""

from __future__ import annotations

from types import SimpleNamespace

import hikari

from smarter_dev.bot.staff_protection import staff_refusal
from smarter_dev.shared import staff_protection as rule
from tests.bot.fake_discord import BOT_ID
from tests.bot.fake_discord import GUILD_ID
from tests.bot.fake_discord import OWNER_ID
from tests.bot.fake_discord import FakeDiscord
from tests.bot.fake_discord import overwrite
from tests.bot.fake_discord import server_error

USER = 3001


def _discord() -> FakeDiscord:
    discord = FakeDiscord()
    discord.roles[700] = hikari.Permissions.SEND_MESSAGES
    discord.add_member(USER, role_ids=(700,))
    return discord


async def test_an_ordinary_member_is_cleared_from_the_cache_alone():
    discord = _discord()

    assert await staff_refusal(discord, GUILD_ID, USER) is None
    assert discord.reads == []


async def test_without_a_cache_everything_comes_over_rest():
    discord = _discord()
    discord.cached = False

    assert await staff_refusal(discord, GUILD_ID, USER) is None
    assert {"fetch_member", "fetch_guild", "fetch_guild_channels"} <= set(discord.reads)


async def test_a_role_missing_from_the_cache_is_fetched():
    discord = _discord()
    real_roles = discord._role_objects

    # The cached guild lacks role 701; REST knows it carries Ban Members.
    discord.roles[701] = hikari.Permissions.BAN_MEMBERS
    discord.add_member(USER, role_ids=(700, 701))
    discord._role_objects = lambda: {k: v for k, v in real_roles().items() if k != 701}
    rest_roles = real_roles()

    async def fetch_roles(guild_id):
        discord.reads.append("fetch_roles")
        return list(rest_roles.values())

    discord.rest.fetch_roles = fetch_roles

    assert await staff_refusal(discord, GUILD_ID, USER) == rule.GUILD_STAFF_PERMISSION
    assert "fetch_roles" in discord.reads


async def test_a_role_overwrite_in_one_channel_protects_nobody():
    discord = _discord()
    discord.channels.append(
        SimpleNamespace(
            id=5009,
            permission_overwrites={
                700: overwrite(allow=hikari.Permissions.MANAGE_CHANNELS)
            },
        )
    )

    assert await staff_refusal(discord, GUILD_ID, USER) is None


async def test_an_everyone_overwrite_in_one_channel_protects_nobody():
    # #303: #the-sudo-squad's @everyone overwrite grants Manage Webhooks; a
    # new member with only a squad role was skipped by the spam engine.
    discord = _discord()
    discord.roles[1404243432469299335] = hikari.Permissions.NONE
    discord.add_member(USER, role_ids=(700, 1404243432469299335))
    discord.channels.append(
        SimpleNamespace(
            id=1404580470158524486,
            permission_overwrites={
                GUILD_ID: overwrite(
                    allow=hikari.Permissions.MANAGE_WEBHOOKS,
                    deny=hikari.Permissions.VIEW_CHANNEL,
                )
            },
        )
    )

    assert await staff_refusal(discord, GUILD_ID, USER) is None


async def test_owner_bot_and_bots():
    discord = _discord()
    discord.add_member(4000, is_bot=True)

    assert await staff_refusal(discord, GUILD_ID, OWNER_ID) == rule.GUILD_OWNER
    assert await staff_refusal(discord, GUILD_ID, BOT_ID) == rule.BOT_ITSELF
    assert await staff_refusal(discord, GUILD_ID, 4000) == rule.A_BOT
    assert await staff_refusal(discord, GUILD_ID, USER, user_is_bot=True) == rule.A_BOT


async def test_a_departed_user_is_cleared_unless_a_bot_or_unknown():
    discord = _discord()
    discord.users[5000] = SimpleNamespace(id=5000, is_bot=False)
    discord.users[5001] = SimpleNamespace(id=5001, is_bot=True)

    assert await staff_refusal(discord, GUILD_ID, 5000) is None
    assert await staff_refusal(discord, GUILD_ID, 5001) == rule.A_BOT
    # Not a member and no such user: whether it is a bot is unknown.
    assert (await staff_refusal(discord, GUILD_ID, 5002)).startswith(
        rule.UNKNOWN_PREFIX
    )


async def test_a_failing_lookup_refuses_and_never_raises():
    discord = _discord()
    discord.cached = False
    discord.fail["fetch_member"] = server_error()

    refusal = await staff_refusal(discord, GUILD_ID, USER)

    assert refusal.startswith(rule.UNKNOWN_PREFIX)
    assert "InternalServerError" in refusal
