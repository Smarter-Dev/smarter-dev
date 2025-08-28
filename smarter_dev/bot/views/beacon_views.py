"""Interactive beacon message modal for Discord bot.

This module provides modal components for squad beacon messages,
allowing users to send messages with role mentions through webhooks.
"""

from __future__ import annotations

import hikari
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, TYPE_CHECKING

from smarter_dev.bot.utils.webhooks import get_or_create_webhook, send_webhook_message
from smarter_dev.bot.utils.image_embeds import get_generator
from smarter_dev.bot.services.exceptions import ServiceError

if TYPE_CHECKING:
    from smarter_dev.bot.services.squads_service import SquadsService

logger = logging.getLogger(__name__)

# Rate limiting: user_id -> last_beacon_time
_beacon_cooldowns: Dict[int, datetime] = {}
BEACON_COOLDOWN_MINUTES = 720  # 12 hours


def create_beacon_message_modal() -> hikari.api.InteractionModalBuilder:
    """Create a beacon message modal.
    
    Returns:
        The modal builder instance
    """
    modal = hikari.impl.InteractionModalBuilder(
        title="Squad Beacon Message",
        custom_id="beacon_message_modal"
    )
    
    # Add message content text input
    modal.add_component(
        hikari.impl.ModalActionRowBuilder().add_component(
            hikari.impl.TextInputBuilder(
                custom_id="message_content",
                label="Beacon Message",
                placeholder="Enter your beacon message to alert your squad...",
                style=hikari.TextInputStyle.PARAGRAPH,
                required=True,
                min_length=1,
                max_length=1800  # Leave room for role mention
            )
        )
    )
    
    return modal


def is_user_on_cooldown(user_id: int) -> tuple[bool, Optional[int]]:
    """Check if user is on beacon cooldown.
    
    Args:
        user_id: Discord user ID
        
    Returns:
        Tuple of (is_on_cooldown, seconds_remaining)
    """
    if user_id not in _beacon_cooldowns:
        return False, None
    
    last_beacon = _beacon_cooldowns[user_id]
    cooldown_end = last_beacon + timedelta(minutes=BEACON_COOLDOWN_MINUTES)
    now = datetime.now(timezone.utc)
    
    if now >= cooldown_end:
        # Cooldown expired, remove from cache
        del _beacon_cooldowns[user_id]
        return False, None
    
    # Still on cooldown
    seconds_remaining = int((cooldown_end - now).total_seconds())
    return True, seconds_remaining


def set_user_cooldown(user_id: int) -> None:
    """Set beacon cooldown for user."""
    _beacon_cooldowns[user_id] = datetime.now(timezone.utc)


async def handle_beacon_modal_submit(
    event: hikari.InteractionCreateEvent,
    squads_service: 'SquadsService'
) -> None:
    """Handle beacon message modal submission.
    
    Args:
        event: Modal submission interaction event
        squads_service: Squad service instance
    """
    if not isinstance(event.interaction, hikari.ModalInteraction):
        return
    
    # We'll respond directly with the result instead of deferring
    
    try:
        # Get the message content from the modal
        message_content = None
        for component in event.interaction.components:
            for action_row in component:
                if hasattr(action_row, 'custom_id') and action_row.custom_id == "message_content":
                    message_content = action_row.value
                    break
        
        if not message_content or not message_content.strip():
            generator = get_generator()
            image_file = generator.create_error_embed("Message content cannot be empty!")
            try:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
            except (hikari.NotFoundError, hikari.BadRequestError):
                logger.warning("Failed to respond to modal interaction - interaction expired")
            return
        
        # Check rate limiting
        is_on_cooldown, seconds_remaining = is_user_on_cooldown(event.interaction.user.id)
        if is_on_cooldown:
            # Format time remaining in hours/minutes
            if seconds_remaining >= 3600:  # 1 hour or more
                hours_remaining = seconds_remaining // 3600
                minutes_part = (seconds_remaining % 3600) // 60
                if minutes_part > 0:
                    time_str = f"{hours_remaining} hour{'s' if hours_remaining != 1 else ''} and {minutes_part} minute{'s' if minutes_part != 1 else ''}"
                else:
                    time_str = f"{hours_remaining} hour{'s' if hours_remaining != 1 else ''}"
            else:
                minutes_remaining = max(1, seconds_remaining // 60)
                time_str = f"{minutes_remaining} minute{'s' if minutes_remaining != 1 else ''}"
            
            generator = get_generator()
            image_file = generator.create_error_embed(
                f"Please wait {time_str} before sending another beacon message."
            )
            try:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
            except (hikari.NotFoundError, hikari.BadRequestError):
                logger.warning("Failed to respond to modal interaction - interaction expired")
            return
        
        # Get user's current squad
        try:
            user_squad_response = await squads_service.get_user_squad(
                str(event.interaction.guild_id), 
                str(event.interaction.user.id)
            )
            
            if not user_squad_response.is_in_squad:
                generator = get_generator()
                image_file = generator.create_error_embed("You must be in a squad to send beacon messages!")
                try:
                    await event.interaction.create_initial_response(
                        hikari.ResponseType.MESSAGE_CREATE,
                        attachment=image_file,
                        flags=hikari.MessageFlag.EPHEMERAL
                    )
                except (hikari.NotFoundError, hikari.BadRequestError):
                    logger.warning("Failed to respond to modal interaction - interaction expired")
                return
            
            squad = user_squad_response.squad
            
        except ServiceError as e:
            logger.error(f"Error getting user squad for beacon: {e}")
            generator = get_generator()
            image_file = generator.create_error_embed("Failed to verify squad membership. Please try again later.")
            try:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
            except (hikari.NotFoundError, hikari.BadRequestError):
                logger.warning("Failed to respond to modal interaction - interaction expired")
            return
        
        # Verify we're in the correct channel
        if not squad.announcement_channel or str(event.interaction.channel_id) != squad.announcement_channel:
            generator = get_generator()
            
            # Try to get the channel name for a more helpful error message
            error_message = "Beacon messages can only be sent in your squad's announcement channel!"
            if squad.announcement_channel:
                try:
                    channel = await event.interaction.app.rest.fetch_channel(int(squad.announcement_channel))
                    # Filter to ASCII characters only and clean up the name
                    clean_name = ''.join(c for c in channel.name if ord(c) < 128)
                    error_message = f"Beacon messages can only be sent in your squad's announcement channel: #{clean_name}"
                    logger.debug(f"Successfully fetched channel name: {clean_name}")
                except Exception as e:
                    # If we can't fetch the channel, just show a generic message
                    logger.debug(f"Could not fetch channel name for {squad.announcement_channel}: {e}")
                    error_message = "Beacon messages can only be sent in your squad's announcement channel!"
            
            image_file = generator.create_error_embed(error_message)
            try:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
            except (hikari.NotFoundError, hikari.BadRequestError):
                logger.warning("Failed to respond to modal interaction - interaction expired")
            return
        
        # Get or create webhook
        webhook = await get_or_create_webhook(
            event.interaction.app,
            event.interaction.channel_id
        )
        
        if not webhook:
            generator = get_generator()
            image_file = generator.create_error_embed(
                "Bot lacks webhook permissions in this channel. Please contact an administrator."
            )
            try:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
            except (hikari.NotFoundError, hikari.BadRequestError):
                logger.warning("Failed to respond to modal interaction - interaction expired")
            return
        
        # Prepare the message with role mention
        role_mention = f"<@&{squad.role_id}>" if squad.role_id else ""
        full_message = f"{message_content.strip()}\n\n{role_mention}" if role_mention else message_content.strip()
        
        # Get user's display name and avatar
        user = event.interaction.user
        display_name = user.display_name or user.username
        avatar_url = str(user.avatar_url) if user.avatar_url else None
        
        # Send the webhook message
        success = await send_webhook_message(
            event.interaction.app,
            webhook,
            full_message,
            display_name,
            avatar_url
        )
        
        if success:
            # Set cooldown for user
            set_user_cooldown(event.interaction.user.id)
            
            # Send success response
            generator = get_generator()
            image_file = generator.create_success_embed(
                "BEACON SENT",
                f"Your beacon message has been sent to {squad.name}!"
            )
            try:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
            except (hikari.NotFoundError, hikari.BadRequestError):
                logger.warning("Failed to respond to modal interaction - interaction expired")
            
            logger.info(f"User {user.username} sent beacon message in squad {squad.name}")
        else:
            generator = get_generator()
            image_file = generator.create_error_embed("Failed to send beacon message. Please try again later.")
            try:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
            except (hikari.NotFoundError, hikari.BadRequestError):
                logger.warning("Failed to respond to modal interaction - interaction expired")
    
    except Exception as e:
        logger.exception(f"Error handling beacon modal submission: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
        try:
            await event.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                attachment=image_file,
                flags=hikari.MessageFlag.EPHEMERAL
            )
        except (hikari.NotFoundError, hikari.BadRequestError):
            logger.warning("Failed to respond to modal interaction - interaction expired")


def clear_beacon_cooldowns() -> None:
    """Clear all beacon cooldowns. Useful for testing."""
    global _beacon_cooldowns
    _beacon_cooldowns.clear()
    logger.debug("Beacon cooldowns cleared")