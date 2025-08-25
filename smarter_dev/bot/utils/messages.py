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
from smarter_dev.bot.cache import bot_cache

logger = logging.getLogger(__name__)


async def fetch_channel_info(bot: hikari.GatewayBot, channel_id: int) -> dict:
    """Fetch channel information for context using cache.
    
    Args:
        bot: Discord bot instance
        channel_id: Channel ID to fetch info for
        
    Returns:
        Dict with channel_name, channel_description, channel_type, and is_forum_thread info
    """
    return await bot_cache.get_channel_info(bot, channel_id)


async def fetch_user_roles(bot: hikari.GatewayBot, guild_id: int, user_id: int, guild_roles: dict[int, str] = None) -> list[str]:
    """Fetch role names for a user in a guild.
    
    Args:
        bot: Discord bot instance
        guild_id: Guild ID where the user is
        user_id: User ID to get roles for
        guild_roles: Optional pre-fetched guild roles dictionary (role_id -> role_name)
        
    Returns:
        List of role names (excluding @everyone)
    """
    try:
        member = await bot.rest.fetch_member(guild_id, user_id)
        
        role_names = []
        for role_id in member.role_ids:
            # Use pre-fetched roles if available, otherwise fetch individually
            if guild_roles and role_id in guild_roles:
                role_name = guild_roles[role_id]
                if role_name != "@everyone":
                    role_names.append(role_name)
            else:
                try:
                    role = await bot.rest.fetch_role(guild_id, role_id)
                    if role and role.name != "@everyone":
                        role_names.append(role.name)
                except Exception:
                    # If we can't get role info, skip it
                    continue
        
        return role_names
        
    except Exception as e:
        logger.debug(f"Failed to fetch user roles for {user_id} in guild {guild_id}: {e}")
        return []


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
            username = await bot_cache.get_user_name(bot, user_id)
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
            channel_info = await bot_cache.get_channel_info(bot, channel_id)
            channel_name = channel_info.get("channel_name")
            if channel_name:
                content = re.sub(f'<#{channel_id}>', f'#{channel_name}', content)
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


async def extract_reply_context(message: hikari.Message, bot: hikari.GatewayBot) -> tuple[str, str, str]:
    """Extract reply context from a message that replies to another message.
    
    Args:
        message: The message that contains a reply
        bot: Discord bot instance to fetch referenced message
        
    Returns:
        Tuple of (replied_author, replied_content, message_content):
        - replied_author: Author of the message being replied to (None if no reply)
        - replied_content: Content of the message being replied to (None if no reply)
        - message_content: The actual message content without inline reply formatting
    """
    content = message.content or ""
    
    # Check if this message is a reply
    if message.referenced_message:
        try:
            replied_msg = message.referenced_message
            replied_author = replied_msg.author.display_name or replied_msg.author.username
            replied_content = replied_msg.content or ""  # Get full content, don't truncate
            
            # Resolve mentions in the replied message too
            replied_content = await resolve_mentions(replied_content, bot)
            
            # Handle cases where replied message has no text (like images/embeds)
            if not replied_content.strip():
                replied_content = "[attachment/embed]"
                
            return replied_author, replied_content, content
                
        except Exception as e:
            logger.debug(f"Failed to extract reply context: {e}")
            # If we can't get the replied message, use fallback
            return "[unknown]", "[message]", content
    
    return None, None, content


async def gather_message_context(
    bot: hikari.GatewayBot, 
    channel_id: int, 
    limit: int = 5,
    skip_short_messages: bool = False,
    min_message_length: int = 10,
    guild_id: int = None
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
        
        # Fetch channel information for context
        channel_info = await fetch_channel_info(bot, channel_id)
        
        # Get guild roles from cache if we have guild context
        guild_roles = {}
        if guild_id:
            guild_roles = await bot_cache.get_guild_roles(bot, guild_id)
        
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
            
            # Extract reply context separately
            replied_author, replied_content, content = await extract_reply_context(message, bot)
            
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
            
            # Get user roles if we have guild context
            author_roles = []
            if guild_id and not message.author.is_bot:
                author_roles = await fetch_user_roles(bot, guild_id, message.author.id, guild_roles)
            
            # Check if this user is the original poster in a forum thread
            is_original_poster = (
                channel_info.get("is_forum_thread", False) and 
                channel_info.get("original_poster_id") == message.author.id
            )
                
            # Convert to our message format
            discord_msg = DiscordMessage(
                author=message.author.display_name or message.author.username,
                author_id=str(message.author.id),  # Include author ID for bot detection
                timestamp=message.created_at.replace(tzinfo=timezone.utc),
                content=content,
                replied_to_author=replied_author,
                replied_to_content=replied_content,
                # Channel context
                channel_name=channel_info.get("channel_name"),
                channel_description=channel_info.get("channel_description"),
                channel_type=channel_info.get("channel_type"),
                # User roles (excluding bots)
                author_roles=author_roles,
                # Forum context
                is_original_poster=is_original_poster
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