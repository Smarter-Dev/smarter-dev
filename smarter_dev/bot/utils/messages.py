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
        
        skipped_count = 0
        processed_count = 0
        
        async for message in bot.rest.fetch_messages(channel_id).limit(max_fetch):
            processed_count += 1
            
            # Only skip system messages (keep bot messages as they can be part of conversation)
            if message.type != hikari.MessageType.DEFAULT:
                skipped_count += 1
                logger.debug(f"Skipped system message type: {message.type}")
                continue
            
            # Skip short messages if requested
            if skip_short_messages and len(message.content.strip()) < min_message_length:
                skipped_count += 1
                logger.debug(f"Skipped short message: '{message.content[:20]}...'")
                continue
            
            # Build message content with attachments
            content = message.content or ""
            
            # Add attachment information for context
            if message.attachments:
                attachment_info = []
                for attachment in message.attachments:
                    # Include filename and media type info
                    if hasattr(attachment, 'filename') and attachment.filename:
                        attachment_info.append(f"📎 {attachment.filename}")
                    elif hasattr(attachment, 'url') and attachment.url:
                        # Extract filename from URL if no filename attribute
                        url_parts = attachment.url.split('/')
                        if url_parts:
                            attachment_info.append(f"📎 {url_parts[-1].split('?')[0]}")
                
                if attachment_info:
                    content = f"{content} {' '.join(attachment_info)}".strip()
                
            # Convert to our message format
            discord_msg = DiscordMessage(
                author=message.author.display_name or message.author.username,
                timestamp=message.created_at.replace(tzinfo=timezone.utc),
                content=content
            )
            messages.append(discord_msg)
            
            # Stop when we have enough messages
            if len(messages) >= limit:
                break
        
        # Messages are collected newest-first, but we want to return them
        # in chronological order (oldest-first) for better summarization context
        reversed_messages = list(reversed(messages))
        
        # Log for debugging - show filtering results and selected messages
        logger.info(f"Message gathering results: processed {processed_count} messages, skipped {skipped_count}, selected {len(reversed_messages)}")
        
        if reversed_messages:
            logger.debug(f"Selected {len(reversed_messages)} messages for context:")
            for i, msg in enumerate(reversed_messages):
                logger.debug(f"  {i+1}. {msg.author}: {msg.content[:50]}... ({msg.timestamp.strftime('%H:%M:%S')})")
        
        return reversed_messages
        
    except Exception as e:
        logger.warning(f"Failed to gather message context: {e}")
        return []