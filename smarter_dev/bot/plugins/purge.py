"""Purge command plugin for moderators.

Bulk-delete recent messages in a channel, optionally scoped to a single user.
The one privileged moderation action that survives as a bot-core slash command
(Discord has no native bulk-delete). See
docs/v2/feature-parity/automated-and-command-moderation.md §4.2.
"""

from __future__ import annotations

import logging

import hikari
import lightbulb

from smarter_dev.bot.mod_action_dispatch import dispatch_mod_action
from smarter_dev.bot.purge_core import (
    delete_selected_messages,
    select_purgeable_messages,
)
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import ModerationActionOperations

logger = logging.getLogger(__name__)

mod_action_ops = ModerationActionOperations()

MIN_PURGE_COUNT = 1
MAX_PURGE_COUNT = 100

plugin = lightbulb.Plugin("purge")


@plugin.command
@lightbulb.option(
    "user",
    "Only delete messages sent by this user",
    type=hikari.OptionType.USER,
    required=False,
    default=None,
)
@lightbulb.option(
    "count",
    "Number of messages to delete (1-100)",
    type=hikari.OptionType.INTEGER,
    required=True,
    min_value=MIN_PURGE_COUNT,
    max_value=MAX_PURGE_COUNT,
)
@lightbulb.command(
    "purge",
    "Bulk-delete recent messages in this channel",
    auto_defer=True,
    ephemeral=True,
)
@lightbulb.implements(lightbulb.SlashCommand)
async def purge(ctx: lightbulb.Context) -> None:
    """Bulk-delete recent channel messages, optionally scoped to one user."""

    count = ctx.options.count
    target_user = ctx.options.user

    if not isinstance(ctx.member, hikari.InteractionMember):
        await ctx.respond(
            "This command can only be used in a server.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    member_perms = lightbulb.utils.permissions_for(ctx.member)
    if not (member_perms & hikari.Permissions.MANAGE_MESSAGES):
        await ctx.respond(
            "You don't have permission to purge messages.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    if count < MIN_PURGE_COUNT or count > MAX_PURGE_COUNT:
        await ctx.respond(
            f"Count must be between {MIN_PURGE_COUNT} and {MAX_PURGE_COUNT}.",
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

    # Per-user mode: a moderator without ADMINISTRATOR cannot purge the messages
    # of another MANAGE_MESSAGES holder. Bots are exempt from this guard.
    if target_user is not None:
        try:
            target_member = guild.get_member(target_user.id)
            if not target_member:
                target_member = await ctx.bot.rest.fetch_member(guild.id, target_user.id)
        except hikari.NotFoundError:
            target_member = None
        except Exception:
            logger.exception("Error checking target membership for purge")
            await ctx.respond(
                "Error checking permissions. Please try again.",
                flags=hikari.MessageFlag.EPHEMERAL,
            )
            return

        if target_member is not None and not target_member.is_bot:
            target_perms = lightbulb.utils.permissions_for(target_member)
            invoker_is_admin = bool(member_perms & hikari.Permissions.ADMINISTRATOR)
            if (target_perms & hikari.Permissions.MANAGE_MESSAGES) and not invoker_is_admin:
                await ctx.respond(
                    "Cannot purge messages from a moderator unless you are an administrator.",
                    flags=hikari.MessageFlag.EPHEMERAL,
                )
                return

    user_filter = str(target_user.id) if target_user is not None else None

    try:
        selection = await select_purgeable_messages(
            ctx.bot.rest.fetch_messages(ctx.channel_id).limit(count * 3),
            count=count,
            user_id=user_filter,
        )
        await delete_selected_messages(
            ctx.bot.rest, ctx.channel_id, selection.message_ids
        )
    except hikari.ForbiddenError:
        await ctx.respond(
            "I don't have permission to delete messages in this channel.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return
    except (hikari.NotFoundError, hikari.BadRequestError):
        # A selected message was deleted concurrently, or aged past the 14-day
        # bulk-delete boundary mid-command. Fail loud, write no audit row.
        await ctx.respond(
            "Some messages could not be deleted (they may have already been "
            "removed or are too old). Please try again.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    deleted_count = len(selection.message_ids)

    channel = guild.get_channel(ctx.channel_id)
    channel_name = channel.name if channel else str(ctx.channel_id)

    # Record the purge BEFORE responding so a slow/failed interaction response can
    # never lose the audit row, and only when something was actually deleted (no
    # row for an action that didn't happen). Fires the mod_action trigger so a
    # mod-log handler can format it (best-effort).
    if deleted_count:
        audit_reason = f"purged {deleted_count} messages in #{channel_name}"
        if target_user is not None:
            audit_target_id = str(target_user.id)
            audit_target_name = target_user.username
        else:
            audit_target_id = str(ctx.channel_id)
            audit_target_name = f"#{channel_name}"

        try:
            async with get_db_session_context() as session:
                action = await mod_action_ops.create_action(
                    session,
                    guild_id=str(guild.id),
                    target_user_id=audit_target_id,
                    target_username=audit_target_name,
                    moderator_user_id=str(ctx.author.id),
                    moderator_username=ctx.author.username,
                    action_type="purge",
                    reason=audit_reason,
                    source="manual",
                    channel_id=str(ctx.channel_id),
                )
                await session.commit()
            await dispatch_mod_action(action)
        except Exception:
            logger.exception("Failed to record purge action to moderation log")

    scope = f" from {target_user.mention}" if target_user is not None else ""
    plural = "s" if deleted_count != 1 else ""
    confirmation = f"Purged {deleted_count} message{plural}{scope}."
    if selection.skipped_too_old:
        confirmation += (
            f" Skipped {selection.skipped_too_old} message(s) older than 14 days "
            f"(Discord cannot bulk-delete them)."
        )
    await ctx.respond(confirmation, flags=hikari.MessageFlag.EPHEMERAL)

    logger.info(
        f"{ctx.author.username} ({ctx.author.id}) purged {deleted_count} message(s) "
        f"in channel {ctx.channel_id} of guild {guild.id}"
    )


def load(bot: lightbulb.BotApp) -> None:
    """Load the purge plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the purge plugin."""
    bot.remove_plugin(plugin)
