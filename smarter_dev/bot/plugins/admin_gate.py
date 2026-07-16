"""Shared Administrator gate for admin-only slash commands.

Centralizes the ``ADMINISTRATOR`` permission check so individual command
handlers never re-implement auth inline. Each command passes its own denial
message (the wording differs per command) while the guard logic stays identical.
"""

from __future__ import annotations

import hikari
import lightbulb


def is_admin(permissions: hikari.Permissions) -> bool:
    """True when ``permissions`` include the Discord ADMINISTRATOR bit."""
    return bool(permissions & hikari.Permissions.ADMINISTRATOR)


async def deny_if_not_admin(ctx: lightbulb.Context, denial_message: str) -> bool:
    """Gate a slash command to server admins; respond ephemerally when denied.

    Returns ``True`` (and sends ``denial_message``) when the invoker is not an
    admin or the command was not run in a guild, so callers can early-return.
    """
    if not isinstance(ctx.member, hikari.InteractionMember):
        await ctx.respond(
            "This command only works in a server.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return True
    if not is_admin(lightbulb.utils.permissions_for(ctx.member)):
        await ctx.respond(denial_message, flags=hikari.MessageFlag.EPHEMERAL)
        return True
    return False
