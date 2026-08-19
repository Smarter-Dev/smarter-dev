"""The /proactive commands are gated on Manage Messages, not Administrator.

Moderators need to be able to switch the bot off in channels they moderate
without holding full admin.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import hikari
import pytest

from smarter_dev.bot.plugins import admin_gate, proactive


def _context(permissions: hikari.Permissions) -> SimpleNamespace:
    member = object.__new__(hikari.InteractionMember)
    return SimpleNamespace(
        member=member,
        respond=AsyncMock(),
        guild_id=2,
        channel_id=1,
        _permissions=permissions,
    )


async def _denied(ctx) -> bool:
    with patch.object(
        admin_gate.lightbulb.utils,
        "permissions_for",
        return_value=ctx._permissions,
    ):
        return await proactive.deny_without_moderator_permissions(
            ctx, proactive.MODERATOR_DENIAL_MESSAGE
        )


async def test_manage_messages_is_allowed():
    ctx = _context(hikari.Permissions.MANAGE_MESSAGES)
    assert await _denied(ctx) is False
    ctx.respond.assert_not_awaited()


async def test_administrator_is_still_allowed():
    ctx = _context(hikari.Permissions.ADMINISTRATOR)
    assert await _denied(ctx) is False


async def test_plain_member_is_denied_ephemerally():
    ctx = _context(hikari.Permissions.SEND_MESSAGES)
    assert await _denied(ctx) is True
    ctx.respond.assert_awaited_once()
    kwargs = ctx.respond.await_args.kwargs
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert "moderator" in ctx.respond.await_args.args[0].lower()


async def test_outside_a_guild_is_denied():
    ctx = SimpleNamespace(member=None, respond=AsyncMock())
    denied = await proactive.deny_without_moderator_permissions(
        ctx, proactive.MODERATOR_DENIAL_MESSAGE
    )
    assert denied is True
    assert "server" in ctx.respond.await_args.args[0].lower()


def test_moderator_check_accepts_either_permission():
    assert proactive.has_moderator_permissions(
        hikari.Permissions.MANAGE_MESSAGES
    )
    assert proactive.has_moderator_permissions(hikari.Permissions.ADMINISTRATOR)
    assert not proactive.has_moderator_permissions(
        hikari.Permissions.SEND_MESSAGES | hikari.Permissions.ADD_REACTIONS
    )
