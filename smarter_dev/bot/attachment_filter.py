"""Attachment filter module for filtering messages based on file type tiers.

Three-tier filtering approach:
1. Ignored extensions - Completely allowed, no action
2. Warn extensions - Send warning but don't delete
3. All others (blocked) - Delete + warn (or just warn if user has manage_messages)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import hikari

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import AttachmentFilterConfigOperations

if TYPE_CHECKING:
    from lightbulb import BotApp

logger = logging.getLogger(__name__)


async def check_attachment_filter(
    bot: BotApp,
    event: hikari.GuildMessageCreateEvent
) -> bool:
    """Check message attachments against the three-tier filter and take appropriate action.

    Tiers:
    1. Ignored extensions - No action taken
    2. Warn extensions - Send warning, don't delete
    3. Blocked (all others) - Delete + warn (or just warn if user has manage_messages)

    Args:
        bot: The bot instance
        event: The message create event

    Returns:
        bool: True if any action was taken (warned or deleted), False otherwise
    """
    # Skip if no attachments
    if not event.message.attachments:
        return False

    # Skip if no guild
    if not event.guild_id:
        return False

    guild_id_str = str(event.guild_id)

    try:
        async with get_db_session_context() as session:
            filter_ops = AttachmentFilterConfigOperations()
            config = await filter_ops.get_config(session, guild_id_str)

            # Skip if no config or not active
            if config is None or not config.is_active:
                return False

            # Categorize attachments
            warn_attachments = []
            blocked_attachments = []

            for attachment in event.message.attachments:
                filename = attachment.filename.lower()
                extension = os.path.splitext(filename)[1].lower()

                # Check if ignored (completely allowed)
                if extension in (config.ignored_extensions or []):
                    continue

                # Check if in warn list
                if extension in (config.warn_extensions or []):
                    warn_attachments.append({
                        "filename": attachment.filename,
                        "extension": extension if extension else "(no extension)"
                    })
                else:
                    # Everything else is blocked
                    blocked_attachments.append({
                        "filename": attachment.filename,
                        "extension": extension if extension else "(no extension)"
                    })

            # No action needed if all attachments are ignored
            if not warn_attachments and not blocked_attachments:
                return False

            user_mention = f"<@{event.author.id}>"

            # Check if user has manage_messages permission (exempt from deletion)
            user_has_manage_messages = False
            try:
                guild = event.get_guild()
                if guild:
                    member = guild.get_member(event.author.id)
                    if member:
                        # Check member permissions
                        permissions = hikari.Permissions.NONE
                        for role_id in member.role_ids:
                            role = guild.get_role(role_id)
                            if role:
                                permissions |= role.permissions
                        # Also check if user is guild owner
                        if guild.owner_id == event.author.id:
                            user_has_manage_messages = True
                        elif permissions & hikari.Permissions.MANAGE_MESSAGES:
                            user_has_manage_messages = True
            except Exception as e:
                logger.error(f"Error checking user permissions: {e}")

            # Determine action based on worst tier found
            # Blocked takes precedence over warn
            should_delete = bool(blocked_attachments) and not user_has_manage_messages

            # Get the attachment to report (prioritize blocked over warned)
            if blocked_attachments:
                reported = blocked_attachments[0]
                is_blocked = not user_has_manage_messages  # True if actually deleted
            else:
                reported = warn_attachments[0]
                is_blocked = False  # Warn-only extensions are never deleted

            # Get appropriate message
            message = config.get_message(
                user_mention=user_mention,
                extension=reported["extension"],
                filename=reported["filename"],
                is_blocked=is_blocked
            )

            # Take action
            try:
                if should_delete:
                    # Delete the message first
                    await event.message.delete()
                    logger.info(
                        f"Deleted message from {event.author} in guild {guild_id_str} "
                        f"due to blocked attachment: {reported['filename']}"
                    )

                # Send message
                await bot.rest.create_message(
                    channel=event.channel_id,
                    content=message,
                    user_mentions=[event.author.id]
                )

                if should_delete:
                    action_taken = "deleted and warned"
                else:
                    action_taken = "warned"

                tier = "blocked" if blocked_attachments else "warn-list"
                logger.info(
                    f"Attachment filter: {action_taken} user {event.author} "
                    f"for {tier} file type {reported['extension']} in guild {guild_id_str}"
                )

                return True

            except hikari.ForbiddenError:
                logger.warning(
                    f"Missing permissions to handle attachment filter in guild {guild_id_str}"
                )
                return False
            except Exception as e:
                logger.error(f"Error handling attachment filter action: {e}")
                return False

    except Exception as e:
        logger.error(f"Error in attachment filter: {e}", exc_info=True)
        return False
