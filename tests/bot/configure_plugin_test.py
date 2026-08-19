"""The /configure bot command hands out a scoped, moderator-gated link."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import hikari

from smarter_dev.bot.plugins import configure
from smarter_dev.shared import config_links

GUILD = 644299523686006834
CHANNEL = 644299524151443487
USER = 266000000000000001

configure_bot = configure.configure_bot.callback


def _ctx(permissions: hikari.Permissions) -> SimpleNamespace:
    member = object.__new__(hikari.InteractionMember)
    return SimpleNamespace(
        member=member,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author=SimpleNamespace(id=USER),
        respond=AsyncMock(),
        _permissions=permissions,
    )


async def _run(ctx) -> None:
    with patch(
        "smarter_dev.bot.plugins.proactive.lightbulb.utils.permissions_for",
        return_value=ctx._permissions,
    ):
        await configure_bot(ctx)


async def test_moderator_gets_an_ephemeral_link_scoped_to_the_channel():
    ctx = _ctx(hikari.Permissions.MANAGE_MESSAGES)
    await _run(ctx)

    ctx.respond.assert_awaited_once()
    body = ctx.respond.await_args.args[0]
    assert ctx.respond.await_args.kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert "/admin/bot/configure/" in body
    token = body.split("/admin/bot/configure/")[1].split(")")[0]
    payload = config_links.verify_config_link(token)
    assert payload.authorizes(guild_id=str(GUILD), channel_id=str(CHANNEL))
    assert payload.discord_user_id == str(USER)


async def test_non_moderator_gets_no_link():
    ctx = _ctx(hikari.Permissions.SEND_MESSAGES)
    await _run(ctx)
    body = ctx.respond.await_args.args[0]
    assert "/admin/bot/configure/" not in body
    assert "moderator" in body.lower()
