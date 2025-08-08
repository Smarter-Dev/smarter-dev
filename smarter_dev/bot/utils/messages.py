"""Shared message utilities for Discord bot plugins.

This module provides common functionality for gathering and processing
Discord messages across different bot plugins.
"""

from __future__ import annotations

import hikari
import logging
from datetime import timezone
from typing import List

from smarter_dev.bot.agent import DiscordMessage

logger = logging.getLogger(__name__)


async def gather_message_context(
    bot: hikari.GatewayBot, 
    channel_id: int, 
    limit: int = 5,
    skip_short_messages: bool = False,
    min_message_length: int = 10
) -> List[DiscordMessage]:
    """Gather recent messages from a channel for context.
    
    Args:
        bot: Discord bot instance
        channel_id: Channel to gather messages from
        limit: Number of recent messages to gather
        skip_short_messages: Whether to skip very short messages
        min_message_length: Minimum message length to include (if skip_short_messages is True)
        
    Returns:
        List[DiscordMessage]: Recent messages for context
    """
    try:
        messages = []
        message_count = 0
        
        # Fetch extra messages in case we filter many
        fetch_limit = limit * 2 if skip_short_messages else limit
        
        async for message in bot.rest.fetch_messages(channel_id).limit(fetch_limit):
            # Skip bot messages and system messages
            if message.author.is_bot or message.type != hikari.MessageType.DEFAULT:
                continue
            
            # Skip short messages if requested (useful for summarization)
            if skip_short_messages and len(message.content.strip()) < min_message_length:
                continue
                
            # Convert to our message format
            discord_msg = DiscordMessage(
                author=message.author.display_name or message.author.username,
                timestamp=message.created_at.replace(tzinfo=timezone.utc),
                content=message.content or ""
            )
            messages.append(discord_msg)
            message_count += 1
            
            # Stop when we have enough messages
            if message_count >= limit:
                break
        
        # Return in chronological order (oldest first)
        return list(reversed(messages))
        
    except Exception as e:
        logger.warning(f"Failed to gather message context: {e}")
        return []