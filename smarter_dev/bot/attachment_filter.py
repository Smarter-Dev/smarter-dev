"""Attachment filter module for filtering messages with blocked file types."""

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
    """Check if a message contains blocked attachments and take appropriate action.

    Args:
        bot: The bot instance
        event: The message create event

    Returns:
        bool: True if the message was handled (deleted or warned), False otherwise
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

            # Skip if no blocked extensions configured
            if not config.blocked_extensions:
                return False

            # Check each attachment
            blocked_attachments = []
            for attachment in event.message.attachments:
                filename = attachment.filename.lower()
                extension = os.path.splitext(filename)[1].lower()

                if extension in config.blocked_extensions:
                    blocked_attachments.append({
                        "filename": attachment.filename,
                        "extension": extension
                    })

            # No blocked attachments found
            if not blocked_attachments:
                return False

            # Get the first blocked attachment for the warning message
            blocked = blocked_attachments[0]
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

            # Determine action
            should_delete = config.action == "delete" and not user_has_manage_messages

            # Get warning message
            warning_message = config.get_warning_message(
                user_mention=user_mention,
                extension=blocked["extension"],
                filename=blocked["filename"]
            )

            # Take action
            try:
                if should_delete:
                    # Delete the message first
                    await event.message.delete()
                    logger.info(
                        f"Deleted message from {event.author} in guild {guild_id_str} "
                        f"due to blocked attachment: {blocked['filename']}"
                    )

                # Send warning message
                await bot.rest.create_message(
                    channel=event.channel_id,
                    content=warning_message,
                    user_mentions=[event.author.id]
                )

                action_taken = "deleted and warned" if should_delete else "warned"
                logger.info(
                    f"Attachment filter: {action_taken} user {event.author} "
                    f"for blocked file type {blocked['extension']} in guild {guild_id_str}"
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
