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

from smarter_dev.bot.forum_posts import forum_parents
from smarter_dev.bot.plugins.proactive import (
    MODERATOR_DENIAL_MESSAGE,
    NOT_A_FORUM_POST_MESSAGE,
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
@lightbulb.option(
    "whole_forum",
    "Inside a forum post: configure the post's whole forum",
    type=bool,
    required=False,
    default=False,
)
@lightbulb.command("bot", "Open this channel's chat bot settings")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def configure_bot(ctx: lightbulb.Context) -> None:
    if await deny_without_moderator_permissions(ctx, MODERATOR_DENIAL_MESSAGE):
        return
    channel_id = str(ctx.channel_id)
    target = "this channel"
    is_forum = False
    if ctx.options.whole_forum:
        forum_id = await forum_parents(ctx.bot).forum_for(channel_id)
        if forum_id is None:
            await ctx.respond(
                NOT_A_FORUM_POST_MESSAGE, flags=hikari.MessageFlag.EPHEMERAL
            )
            return
        # The link names the forum, so the form switches every post that
        # has no setting of its own.
        channel_id = forum_id
        target = "this whole forum"
        is_forum = True
    url = build_config_link_url(
        guild_id=str(ctx.guild_id),
        channel_id=channel_id,
        discord_user_id=str(ctx.author.id),
        is_forum=is_forum,
    )
    logger.info(
        "configure link issued guild=%s channel=%s user=%s",
        ctx.guild_id,
        channel_id,
        ctx.author.id,
    )
    await ctx.respond(
        f"[Configure the chat bot for {target}]({url})\n"
        f"-# The link works for 15 minutes and only for {target}. "
        "Don't share it — anyone who opens it can change these settings.",
        flags=hikari.MessageFlag.EPHEMERAL,
    )


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    bot.remove_plugin(plugin)
