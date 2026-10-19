"""Warn command plugin for moderators."""

from __future__ import annotations

import logging

import hikari
import lightbulb

from smarter_dev.bot.moderation_port import bot_gateway
from smarter_dev.bot.moderation_port import manual_trigger
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import ModerationActionOperations
from smarter_dev.web.moderation_gateway import Proposal
from smarter_dev.web.moderation_gateway import types as gw

logger = logging.getLogger(__name__)

mod_action_ops = ModerationActionOperations()

# Create the plugin
plugin = lightbulb.Plugin("warn")


@plugin.command
@lightbulb.option(
    "reason",
    "Reason for the warning",
    type=hikari.OptionType.STRING,
    required=True,
)
@lightbulb.option(
    "user",
    "The user to warn",
    type=hikari.OptionType.USER,
    required=True,
)
@lightbulb.command("warn", "Warn a user with a specified reason")
@lightbulb.implements(lightbulb.SlashCommand)
async def warn_user(ctx: lightbulb.Context) -> None:
    """Warn a user with a public notice and DM."""

    target_user = ctx.options.user
    reason = ctx.options.reason

    # Check if user has moderation permission
    if not isinstance(ctx.member, hikari.InteractionMember):
        await ctx.respond(
            "This command can only be used in a server.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    member_perms = lightbulb.utils.permissions_for(ctx.member)
    if not (member_perms & hikari.Permissions.MODERATE_MEMBERS):
        await ctx.respond(
            "You don't have permission to warn members.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    guild = ctx.get_guild()
    if not guild:
        await ctx.respond(
            "Could not access guild information.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    # Don't warn other moderators/admins
    try:
        target_member = guild.get_member(target_user.id)
        if not target_member:
            target_member = await ctx.bot.rest.fetch_member(guild.id, target_user.id)

        target_perms = lightbulb.utils.permissions_for(target_member)
        if target_perms & (
            hikari.Permissions.MODERATE_MEMBERS | hikari.Permissions.ADMINISTRATOR
        ):
            await ctx.respond(
                "Cannot warn users with moderation permissions.",
                flags=hikari.MessageFlag.EPHEMERAL,
            )
            return
    except hikari.NotFoundError:
        await ctx.respond(
            "User is not a member of this server.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return
    except Exception as e:
        logger.error(f"Error checking permissions for warn: {e}")
        await ctx.respond(
            "Error checking permissions. Please try again.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    # Post warning embed in the channel
    embed = hikari.Embed(
        title="Warning Issued",
        description=f"{target_user.mention} has been warned.",
        color=hikari.Color.from_rgb(255, 193, 7),  # Amber/yellow
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_footer(text=f"Warned by {ctx.author.username}")

    await ctx.respond(embed=embed)

    # DM the user
    try:
        dm_channel = await target_user.fetch_dm_channel()
        dm_message = (
            f"You have received a warning in **{guild.name}**.\n\n"
            f"**Reason:** {reason}\n\n"
            f"Please review the server rules. Continued violations may result "
            f"in further action (timeout, kick, or ban)."
        )
        await ctx.bot.rest.create_message(dm_channel, dm_message)
    except (hikari.ForbiddenError, hikari.NotFoundError):
        logger.info(f"Could not DM user {target_user.username} about warning")

    # Record the warning through the gateway (one row, and the mod_action
    # trigger for mod-log handlers); best-effort, the warning already stands.
    try:
        async with bot_gateway(ctx.bot).reserve(
            manual_trigger(
                guild_id=guild.id,
                channel_id=ctx.channel_id,
                moderator=ctx.author,
                reason=reason,
            ),
            Proposal(
                kind=gw.WARN,
                target_user_id=str(target_user.id),
                target_username=target_user.username,
                reason=reason,
                channel_id=str(ctx.channel_id),
            ),
        ) as slot:
            slot.confirm()
    except Exception:
        logger.exception("Failed to record warn action to moderation log")

    # Get warn count for context
    try:
        async with get_db_session_context() as session:
            warn_count = await mod_action_ops.count_warns_for_user(
                session, str(guild.id), str(target_user.id)
            )
            if warn_count > 1:
                logger.info(
                    f"User {target_user.username} ({target_user.id}) now has "
                    f"{warn_count} warnings in guild {guild.id}"
                )
    except Exception:
        logger.debug("Failed to fetch warn count")

    logger.info(
        f"User {target_user.username} ({target_user.id}) warned by "
        f"{ctx.author.username} ({ctx.author.id}). Reason: {reason}"
    )


def load(bot: lightbulb.BotApp) -> None:
    """Load the warn plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the warn plugin."""
    bot.remove_plugin(plugin)
