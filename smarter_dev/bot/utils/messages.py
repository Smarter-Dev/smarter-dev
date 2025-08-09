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
        List[DiscordMessage]: Recent messages for context, in chronological order
    """
    try:
        messages = []
        
        # When filtering messages, we need to fetch more to ensure we get enough
        # Since we may skip bot messages and system messages
        max_fetch = limit * 2 if skip_short_messages else int(limit * 1.5)
        
        async for message in bot.rest.fetch_messages(channel_id).limit(max_fetch):
            # Skip bot messages and system messages
            if message.author.is_bot or message.type != hikari.MessageType.DEFAULT:
                continue
            
            # Skip short messages if requested
            if skip_short_messages and len(message.content.strip()) < min_message_length:
                continue
                
            # Convert to our message format
            discord_msg = DiscordMessage(
                author=message.author.display_name or message.author.username,
                timestamp=message.created_at.replace(tzinfo=timezone.utc),
                content=message.content or ""
            )
            messages.append(discord_msg)
            
            # Stop when we have enough messages
            if len(messages) >= limit:
                break
        
        # Messages are collected newest-first, but we want to return them
        # in chronological order (oldest-first) for better summarization context
        reversed_messages = list(reversed(messages))
        
        # Log for debugging - show which messages were selected
        if reversed_messages:
            logger.debug(f"Selected {len(reversed_messages)} messages for context:")
            for i, msg in enumerate(reversed_messages):
                logger.debug(f"  {i+1}. {msg.author}: {msg.content[:50]}... ({msg.timestamp.strftime('%H:%M:%S')})")
        
        return reversed_messages
        
    except Exception as e:
        logger.warning(f"Failed to gather message context: {e}")
        return []