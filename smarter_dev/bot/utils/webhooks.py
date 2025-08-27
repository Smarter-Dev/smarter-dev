"""Webhook management utilities for Discord bot.

This module provides utilities for creating and managing Discord webhooks,
particularly for sending messages with custom user identities.
"""

from __future__ import annotations

import hikari
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Cache for channel webhooks to avoid recreating them
_webhook_cache: Dict[int, hikari.IncomingWebhook] = {}


async def get_or_create_webhook(
    bot: hikari.GatewayBot,
    channel_id: int,
    webhook_name: str = "Squad Beacon System"
) -> Optional[hikari.IncomingWebhook]:
    """Get or create a webhook for the specified channel.
    
    Args:
        bot: Discord bot instance
        channel_id: Channel ID to get/create webhook for
        webhook_name: Name for the webhook
        
    Returns:
        Webhook instance or None if creation failed
    """
    try:
        # Check cache first
        if channel_id in _webhook_cache:
            return _webhook_cache[channel_id]
        
        # Get existing webhooks in the channel
        try:
            webhooks = await bot.rest.fetch_channel_webhooks(channel_id)
            
            # Look for existing webhook with our name
            for webhook in webhooks:
                if webhook.name == webhook_name:
                    _webhook_cache[channel_id] = webhook
                    logger.debug(f"Found existing webhook {webhook.id} in channel {channel_id}")
                    return webhook
                    
        except hikari.ForbiddenError:
            logger.error(f"No permission to fetch webhooks in channel {channel_id}")
            return None
        except hikari.NotFoundError:
            logger.warning(f"Channel {channel_id} not found")
            return None
        
        # Create new webhook if none exists
        try:
            webhook = await bot.rest.create_webhook(
                channel=channel_id,
                name=webhook_name,
                reason="Created for squad beacon messages"
            )
            _webhook_cache[channel_id] = webhook
            logger.info(f"Created new webhook {webhook.id} in channel {channel_id}")
            return webhook
            
        except hikari.ForbiddenError:
            logger.error(f"No permission to create webhook in channel {channel_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to create webhook in channel {channel_id}: {e}")
            return None
            
    except Exception as e:
        logger.error(f"Error getting/creating webhook for channel {channel_id}: {e}")
        return None


async def send_webhook_message(
    bot: hikari.GatewayBot,
    webhook: hikari.IncomingWebhook,
    content: str,
    username: str,
    avatar_url: Optional[str] = None
) -> bool:
    """Send a message through a webhook with custom user identity.
    
    Args:
        bot: Discord bot instance
        webhook: Webhook to send through
        content: Message content
        username: Display name for the message
        avatar_url: Avatar URL for the message
        
    Returns:
        True if message was sent successfully, False otherwise
    """
    try:
        await bot.rest.execute_webhook(
            webhook=webhook.id,
            token=webhook.token,
            content=content,
            username=username,
            avatar_url=avatar_url,
            role_mentions=True  # Allow role mentions to actually ping
        )
        logger.debug(f"Successfully sent webhook message as {username}")
        return True
        
    except hikari.BadRequestError as e:
        logger.error(f"Bad request sending webhook message: {e}")
        return False
    except hikari.ForbiddenError as e:
        logger.error(f"Forbidden sending webhook message: {e}")
        return False
    except hikari.NotFoundError as e:
        logger.error(f"Webhook not found: {e}")
        # Remove from cache if webhook is invalid
        for channel_id, cached_webhook in list(_webhook_cache.items()):
            if cached_webhook.id == webhook.id:
                del _webhook_cache[channel_id]
                break
        return False
    except Exception as e:
        logger.error(f"Error sending webhook message: {e}")
        return False


def clear_webhook_cache() -> None:
    """Clear the webhook cache. Useful for testing or error recovery."""
    global _webhook_cache
    _webhook_cache.clear()
    logger.debug("Webhook cache cleared")


def remove_webhook_from_cache(channel_id: int) -> None:
    """Remove a specific webhook from cache."""
    if channel_id in _webhook_cache:
        del _webhook_cache[channel_id]
        logger.debug(f"Removed webhook for channel {channel_id} from cache")