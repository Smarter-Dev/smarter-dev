"""Attachment filter module for filtering messages based on file type tiers.

Three-tier filtering approach:
1. Ignored extensions - Completely allowed, no action
2. Warn extensions - Send warning but don't delete
3. All others (blocked) - Delete + warn (or just warn if the author is protected
   staff: see smarter_dev.shared.staff_protection)

The delete goes through the moderation gateway, so it is recorded (one
``moderation_actions`` row) and never repeated: when another detector already
removed the message, this filter posts no second notice about it.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import hikari

from smarter_dev.bot.moderation_port import bot_gateway
from smarter_dev.bot.staff_protection import staff_refusal
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.web.crud import AttachmentFilterConfigOperations
from smarter_dev.web.moderation_gateway import Proposal
from smarter_dev.web.moderation_gateway import Trigger
from smarter_dev.web.moderation_gateway import types as gw

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
    3. Blocked (all others) - Delete + warn (or just warn if the author is protected staff)

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

            # Staff only ever get the notice, never the delete; an author whose
            # standing cannot be established counts as staff. Only a blocked
            # attachment needs the check.
            author_is_protected = False
            if blocked_attachments:
                refusal = await staff_refusal(
                    bot,
                    event.guild_id,
                    event.author.id,
                    member=getattr(event, "member", None),
                    user_is_bot=event.author.is_bot,
                )
                author_is_protected = refusal is not None

            # Determine action based on worst tier found
            # Blocked takes precedence over warn
            should_delete = bool(blocked_attachments) and not author_is_protected

            # Get the attachment to report (prioritize blocked over warned)
            if blocked_attachments:
                reported = blocked_attachments[0]
                is_blocked = not author_is_protected  # True if actually deleted
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
                    # Delete the message first, through the gateway.
                    outcome = await _delete_through_gateway(bot, event, reported)
                    if outcome.state in (gw.SKIPPED, gw.ABSENT, gw.FAILED):
                        # Someone else removed it (and said so), it is protected
                        # after all, or Discord refused: no notice about a
                        # delete this filter did not make.
                        logger.info(
                            f"Attachment filter did not delete message "
                            f"{event.message_id} in guild {guild_id_str}: "
                            f"{outcome.detail}"
                        )
                        return False
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
                logger.error(f"Error handling attachment filter action: {type(e).__name__}")
                return False

    except Exception as e:
        log_exception(logger, f"Error in attachment filter: {type(e).__name__}", level=logging.ERROR)
        return False


async def _delete_through_gateway(
    bot: BotApp, event: hikari.GuildMessageCreateEvent, reported: dict
) -> gw.Outcome:
    reason = f"blocked attachment: {reported['filename']}"
    result = await bot_gateway(bot).act(
        Trigger(
            guild_id=str(event.guild_id),
            actor="attachment_filter",
            reason=reason,
            source=gw.SOURCE_HANDLER,
            channel_id=str(event.channel_id),
            message_id=str(event.message_id),
            author_id=str(event.author.id),
            content=event.message.content or None,
        ),
        [
            Proposal(
                kind=gw.DELETE,
                target_user_id=str(event.author.id),
                target_username=event.author.username,
                reason=reason,
                channel_id=str(event.channel_id),
                message_ids=(str(event.message_id),),
                target_is_bot=bool(event.author.is_bot),
            )
        ],
    )
    return result.outcomes[0]
