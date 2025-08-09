"""Shared message utilities for Discord bot plugins.

This module provides common functionality for gathering and processing
Discord messages across different bot plugins.
"""

from __future__ import annotations

import hikari
import logging
import re
from datetime import timezone
from typing import List

from smarter_dev.bot.agent import DiscordMessage

logger = logging.getLogger(__name__)


async def resolve_mentions(content: str, bot: hikari.GatewayBot) -> str:
    """Replace Discord user and channel mentions with readable format.
    
    Args:
        content: Message content with potential <@123456> and <#123456> mentions
        bot: Discord bot instance to resolve user/channel IDs
        
    Returns:
        Content with mentions resolved to @username and #channel-name format
    """
    if not content:
        return content
    
    # Replace user mentions: <@123456789> or <@!123456789>
    user_mention_pattern = r'<@!?(\d+)>'
    user_mentions = re.findall(user_mention_pattern, content)
    for user_id_str in user_mentions:
        user_id = int(user_id_str)
        try:
            user = await bot.rest.fetch_user(user_id)
            username = user.display_name or user.username
            # Replace both <@123> and <@!123> formats
            content = re.sub(f'<@!?{user_id}>', f'@{username}', content)
        except Exception:
            # If we can't resolve, use a readable fallback
            content = re.sub(f'<@!?{user_id}>', f'@user{user_id}', content)
    
    # Replace channel mentions: <#123456789>
    channel_mention_pattern = r'<#(\d+)>'
    channel_mentions = re.findall(channel_mention_pattern, content)
    for channel_id_str in channel_mentions:
        channel_id = int(channel_id_str)
        try:
            channel = await bot.rest.fetch_channel(channel_id)
            if hasattr(channel, 'name') and channel.name:
                content = re.sub(f'<#{channel_id}>', f'#{channel.name}', content)
            else:
                content = re.sub(f'<#{channel_id}>', f'#channel{channel_id}', content)
        except Exception:
            # If we can't resolve, use a readable fallback
            content = re.sub(f'<#{channel_id}>', f'#channel{channel_id}', content)
    
    # Replace role mentions: <@&123456789>
    role_mention_pattern = r'<@&(\d+)>'
    role_mentions = re.findall(role_mention_pattern, content)
    for role_id_str in role_mentions:
        role_id = int(role_id_str)
        # Role resolution is more complex since we need guild context
        # For now, just make it readable - could enhance later with guild parameter
        content = re.sub(f'<@&{role_id}>', f'@role{role_id}', content)
    
    return content


async def format_reply_context(message: hikari.Message, bot: hikari.GatewayBot) -> str:
    """Format reply context for a message that replies to another message.
    
    Args:
        message: The message that contains a reply
        bot: Discord bot instance to fetch referenced message
        
    Returns:
        Formatted string with reply context and response
    """
    content = message.content or ""
    
    # Check if this message is a reply
    if message.referenced_message:
        try:
            replied_msg = message.referenced_message
            replied_author = replied_msg.author.display_name or replied_msg.author.username
            replied_content = (replied_msg.content or "")[:100]  # Truncate long replies
            
            # Resolve mentions in the replied message too
            replied_content = await resolve_mentions(replied_content, bot)
            
            # Format with quoted reply context
            if replied_content.strip():
                content = f"> {replied_author}: {replied_content}{'...' if len(replied_msg.content or '') > 100 else ''}\n{content}"
            else:
                # Handle cases where replied message has no text (like images/embeds)
                content = f"> {replied_author}: [attachment/embed]\n{content}"
                
        except Exception as e:
            logger.debug(f"Failed to format reply context: {e}")
            # If we can't get the replied message, just indicate it's a reply
            content = f"> [replied to message]\n{content}"
    
    return content


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
        
        # When filtering short messages, we need to fetch more to ensure we get enough
        # Otherwise fetch exactly what we need since we include all messages
        max_fetch = limit * 2 if skip_short_messages else limit
        
        skipped_count = 0
        processed_count = 0
        
        async for message in bot.rest.fetch_messages(channel_id).limit(max_fetch):
            processed_count += 1
            
            # Include ALL messages - no filtering except for short messages if explicitly requested
            if skip_short_messages and len(message.content.strip()) < min_message_length:
                skipped_count += 1
                logger.debug(f"Skipped short message: '{message.content[:20]}...'")
                continue
            
            # Build message content starting with reply context
            content = await format_reply_context(message, bot)
            
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
            
            # Resolve Discord mentions to readable usernames (after reply formatting)
            content = await resolve_mentions(content, bot)
                
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