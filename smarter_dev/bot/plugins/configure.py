"""`/configure bot` — hand a moderator a link to this channel's settings page.

The web dashboard has no per-guild notion of "admin", so the link carries
its own authorization: this command is the point where Discord confirms the
invoker's permissions, and the signed link records that decision for the
15 minutes it stays valid.

This is the first step in folding the configuration slash commands into the
web dashboard, where the settings have room to grow.
"""

from __future__ import annotations

import logging

import hikari
import lightbulb

from smarter_dev.bot.plugins.proactive import (
    MODERATOR_DENIAL_MESSAGE,
    deny_without_moderator_permissions,
)
from smarter_dev.shared.config_links import build_config_link_url

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("configure")


@plugin.command
@lightbulb.command("configure", "Configure this channel (moderators only)")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def configure_group(ctx: lightbulb.Context) -> None:
    pass


@configure_group.child
@lightbulb.command("bot", "Open this channel's chat bot settings")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def configure_bot(ctx: lightbulb.Context) -> None:
    if await deny_without_moderator_permissions(ctx, MODERATOR_DENIAL_MESSAGE):
        return
    url = build_config_link_url(
        guild_id=str(ctx.guild_id),
        channel_id=str(ctx.channel_id),
        discord_user_id=str(ctx.author.id),
    )
    logger.info(
        "configure link issued guild=%s channel=%s user=%s",
        ctx.guild_id,
        ctx.channel_id,
        ctx.author.id,
    )
    await ctx.respond(
        f"[Configure the chat bot for this channel]({url})\n"
        "-# The link works for 15 minutes and only for this channel. "
        "Don't share it — anyone who opens it can change these settings.",
        flags=hikari.MessageFlag.EPHEMERAL,
    )


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    bot.remove_plugin(plugin)
