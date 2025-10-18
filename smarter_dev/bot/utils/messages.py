"""Shared message utilities for Discord bot plugins.

This module provides common functionality for gathering and processing
Discord messages across different bot plugins.
"""

from __future__ import annotations

import hikari
import logging
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set

from smarter_dev.bot.agents.models import DiscordMessage
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


async def resolve_mentions(content: str, bot: hikari.GatewayBot, guild_id: int = None) -> str:
    """Replace Discord role and channel mentions with readable format.
    Keep user mentions as <@user_id> for disambiguation.
    
    Args:
        content: Message content with potential <@123456>, <@&123456> and <#123456> mentions
        bot: Discord bot instance to resolve role/channel IDs
        guild_id: Guild ID for role resolution (optional)
        
    Returns:
        Content with role/channel mentions resolved, user mentions preserved as <@user_id>
    """
    if not content:
        return content
    
    # Normalize user mentions to <@user_id> format (remove the ! if present)
    user_mention_pattern = r'<@!(\d+)>'
    content = re.sub(user_mention_pattern, r'<@\1>', content)
    
    # Replace role mentions: <@&123456789>
    role_mention_pattern = r'<@&(\d+)>'
    role_mentions = re.findall(role_mention_pattern, content)
    for role_id_str in role_mentions:
        role_id = int(role_id_str)
        try:
            # Try to fetch the role to get its name
            if guild_id:
                role = await bot.rest.fetch_role(guild_id, role_id)
                if role:
                    content = re.sub(f'<@&{role_id}>', f'@{role.name}', content)
                else:
                    content = re.sub(f'<@&{role_id}>', f'@role{role_id}', content)
            else:
                content = re.sub(f'<@&{role_id}>', f'@role{role_id}', content)
        except Exception:
            content = re.sub(f'<@&{role_id}>', f'@role{role_id}', content)
    
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
            replied_content = await resolve_mentions(replied_content, bot, message.guild_id)
            
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
            content = await resolve_mentions(content, bot, guild_id)
            
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


class ConversationContextBuilder:
    """Builds structured conversation context for DSPy agents."""
    
    def __init__(self, bot: hikari.GatewayBot, guild_id: Optional[int] = None):
        self.bot = bot
        self.guild_id = guild_id
        self._guild_roles: Dict[int, str] = {}
        self._fetched_messages: Dict[int, hikari.Message] = {}
        
    async def build_context(
        self, 
        channel_id: int, 
        trigger_message_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Build complete conversation context.
        
        Args:
            channel_id: Channel to gather context from
            trigger_message_id: Message ID that triggered the agent (for marking new messages)
            
        Returns:
            Dict with conversation_timeline, users, channel, and me fields
        """
        # Load guild roles if we have guild context
        if self.guild_id:
            self._guild_roles = await bot_cache.get_guild_roles(self.bot, self.guild_id)
            
        # Fetch base messages and complete reply threads
        messages = await self._fetch_base_messages(channel_id, limit=20)
        messages = await self._complete_reply_threads(messages)
        
        # Build user info first so we can enrich the timeline
        users = await self._build_user_list(messages)
        users_by_id = {user["user_id"]: user for user in users}
        
        # Build conversation timeline with enriched user info
        conversation_timeline = await self._build_conversation_timeline(messages, trigger_message_id, users_by_id)
        
        channel_info = await self._build_channel_info(channel_id)
        me_info = self._build_me_info()
        
        return {
            "conversation_timeline": conversation_timeline,
            "users": users, 
            "channel": channel_info,
            "me": me_info
        }
        
    async def _fetch_base_messages(self, channel_id: int, limit: int = 20) -> List[hikari.Message]:
        """Fetch the initial set of messages from the channel."""
        messages = []
        
        async for message in self.bot.rest.fetch_messages(channel_id).limit(limit):
            self._fetched_messages[message.id] = message
            messages.append(message)
            
        return list(reversed(messages))  # Return in chronological order
        
    async def _complete_reply_threads(self, messages: List[hikari.Message]) -> List[hikari.Message]:
        """Recursively fetch any replied-to messages not in the current list."""
        message_ids = {msg.id for msg in messages}
        additional_messages = {}
        
        def collect_reply_chain(message: hikari.Message):
            """Recursively collect messages in a reply chain."""
            if message.referenced_message and message.referenced_message.id not in message_ids:
                reply_msg = message.referenced_message
                additional_messages[reply_msg.id] = reply_msg
                message_ids.add(reply_msg.id)
                # Recursively check if the replied-to message is also a reply
                collect_reply_chain(reply_msg)
                
        # Check all messages for reply chains
        for message in messages:
            collect_reply_chain(message)
            
        # Add additional messages to our collection and sort by timestamp
        all_messages = messages + list(additional_messages.values())
        return sorted(all_messages, key=lambda m: m.created_at)
        
    async def _build_message_list(
        self, 
        messages: List[hikari.Message], 
        trigger_message_id: Optional[int]
    ) -> List[Dict[str, Any]]:
        """Build the channel_messages list with proper formatting."""
        message_list = []
        trigger_timestamp = None
        
        # Find trigger message timestamp
        if trigger_message_id:
            trigger_msg = next((m for m in messages if m.id == trigger_message_id), None)
            if trigger_msg:
                trigger_timestamp = trigger_msg.created_at
                
        for message in messages:
            # Resolve mentions in content
            content = await resolve_mentions(message.content or "", self.bot, self.guild_id)
            
            # Add attachment info
            if message.attachments:
                attachment_info = []
                for attachment in message.attachments:
                    if hasattr(attachment, 'filename') and attachment.filename:
                        attachment_info.append(f"📎 {attachment.filename}")
                        
                if attachment_info:
                    content = f"{content} {' '.join(attachment_info)}".strip()
                    
            # Determine if message is "new"
            is_new = False
            if trigger_timestamp:
                is_new = (message.created_at >= trigger_timestamp or 
                         message.id == trigger_message_id)
                         
            message_dict = {
                "author_id": str(message.author.id),
                "sent": message.created_at.isoformat(),
                "message_id": str(message.id),
                "content": content,
                "is_new": is_new,
                "reply_to_message": str(message.referenced_message.id) if message.referenced_message else None
            }
            
            message_list.append(message_dict)
            
        return message_list
        
    async def _build_conversation_timeline(self, messages: List[hikari.Message], trigger_message_id: Optional[int], users_by_id: Dict[str, Dict]) -> str:
        """Build a human-readable conversation timeline.
        
        Args:
            messages: List of Discord messages
            trigger_message_id: ID of message that triggered the agent
            users_by_id: Dict mapping user IDs to user info
            
        Returns:
            String representing the conversation timeline
        """
        timeline_parts = []
        trigger_timestamp = None
        
        # Find trigger message timestamp
        if trigger_message_id:
            trigger_msg = next((m for m in messages if m.id == trigger_message_id), None)
            if trigger_msg:
                trigger_timestamp = trigger_msg.created_at
        
        # Build timeline entries
        for message in messages:
            # Get user display name
            user_info = users_by_id.get(str(message.author.id), {})
            display_name = user_info.get("discord_name", f"User{message.author.id}")
            
            # Check if user is bot
            is_bot = user_info.get("is_bot", False)
            if is_bot and "bot_name" in user_info:
                display_name = user_info["bot_name"]
            
            # Resolve mentions in content
            content = await resolve_mentions(message.content or "", self.bot, self.guild_id)
            
            # Add attachment info
            if message.attachments:
                attachment_info = []
                for attachment in message.attachments:
                    if hasattr(attachment, 'filename') and attachment.filename:
                        attachment_info.append(f"📎 {attachment.filename}")
                        
                if attachment_info:
                    content = f"{content} {' '.join(attachment_info)}".strip()
            
            # Determine if message is "new" (recent activity)
            is_new = False
            if trigger_timestamp:
                is_new = (message.created_at >= trigger_timestamp or message.id == trigger_message_id)

            # Format timestamp as relative time
            now = datetime.now(timezone.utc)
            time_diff = now - message.created_at

            # Convert to relative time string
            total_seconds = int(time_diff.total_seconds())
            if total_seconds < 60:
                time_str = f"{total_seconds} seconds ago"
            elif total_seconds < 3600:  # Less than 1 hour
                minutes = total_seconds // 60
                time_str = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            elif total_seconds < 86400:  # Less than 1 day
                hours = total_seconds // 3600
                time_str = f"{hours} hour{'s' if hours != 1 else ''} ago"
            else:
                days = total_seconds // 86400
                time_str = f"{days} day{'s' if days != 1 else ''} ago"
            
            # Handle reply context
            reply_context = ""
            if message.referenced_message:
                # Find the replied-to message in our list
                replied_msg = next((m for m in messages if m.id == message.referenced_message.id), None)
                if replied_msg:
                    replied_user_info = users_by_id.get(str(replied_msg.author.id), {})
                    replied_display_name = replied_user_info.get("discord_name", f"User{replied_msg.author.id}")
                    if replied_user_info.get("is_bot", False) and "bot_name" in replied_user_info:
                        replied_display_name = replied_user_info["bot_name"]
                    
                    replied_content = (replied_msg.content or "")[:50]
                    if len(replied_msg.content or "") > 50:
                        replied_content += "..."
                    
                    reply_context = f" (replying to {replied_display_name}: \"{replied_content}\")"
            
            # Build timeline entry - include message ID so agent can reply/react to it
            new_indicator = " [NEW]" if is_new else ""
            message_id = str(message.id)
            timeline_entry = f"[ID: {message_id}] [{time_str}] {display_name}{reply_context}: {content}{new_indicator}"

            timeline_parts.append(timeline_entry)
        
        # Add context header
        channel_name = "#unknown"
        try:
            channel = await self.bot.rest.fetch_channel(messages[0].channel_id)
            if hasattr(channel, 'name'):
                channel_name = f"#{channel.name}"
        except Exception:
            pass
            
        header = f"\n=== Conversation in {channel_name} ===\n"
        footer = "\n=== End of conversation ===\n"
        
        # Add summary if conversation is long
        full_timeline = header + "\n".join(timeline_parts) + footer
        
        if len(timeline_parts) > 8:  # Add summary for longer conversations
            summary = self._generate_conversation_summary(messages, users_by_id)
            full_timeline = summary + "\n" + full_timeline
            
        return full_timeline
        
    def _generate_conversation_summary(self, messages: List[hikari.Message], users_by_id: Dict[str, Dict]) -> str:
        """Generate a brief summary of the conversation."""
        if len(messages) < 3:
            return ""
            
        # Count participants
        participants = set()
        topics = []
        
        for message in messages:
            user_info = users_by_id.get(str(message.author.id), {})
            display_name = user_info.get("discord_name", f"User{message.author.id}")
            participants.add(display_name)
            
            # Extract potential topics (simple keywords)
            content = (message.content or "").lower()
            if len(content) > 20:  # Only consider substantial messages
                topics.append(content[:50] + "..." if len(content) > 50 else content)
        
        participant_list = ", ".join(list(participants)[:5])  # Limit to first 5
        if len(participants) > 5:
            participant_list += f" and {len(participants) - 5} others"
            
        summary_parts = [
            f"=== Conversation Summary ===",
            f"Participants: {participant_list}",
            f"Messages: {len(messages)} total"
        ]
        
        # Add recent activity note if applicable  
        recent_messages = [m for m in messages if (datetime.now(timezone.utc) - m.created_at).total_seconds() < 300]
        if recent_messages:
            summary_parts.append(f"Recent activity: {len(recent_messages)} new messages in last 5 minutes")
            
        summary_parts.append("=" * 30)
        
        return "\n".join(summary_parts)
        
    async def _build_user_list(self, messages: List[hikari.Message]) -> List[Dict[str, Any]]:
        """Build the users list from all users mentioned or who sent messages."""
        user_ids: Set[int] = set()
        
        # Collect user IDs from message authors
        for message in messages:
            user_ids.add(message.author.id)
            
        # Collect user IDs from mentions in message content
        for message in messages:
            content = message.content or ""
            user_mentions = re.findall(r'<@!?(\d+)>', content)
            for user_id_str in user_mentions:
                user_ids.add(int(user_id_str))
                
        # Build user info for each unique user
        users = []
        for user_id in user_ids:
            user_info = await self._build_user_info(user_id)
            if user_info:
                users.append(user_info)
                
        return users
        
    async def _build_user_info(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Build user information dict for a single user."""
        try:
            user = await bot_cache.get_user_name(self.bot, user_id)
            
            # Get member info if we have guild context
            member = None
            if self.guild_id:
                try:
                    member = await self.bot.rest.fetch_member(self.guild_id, user_id)
                except Exception:
                    pass
                    
            # Get role names for non-bot users
            role_names = []
            is_bot = False
            
            if member:
                is_bot = member.is_bot
                if not is_bot:
                    role_names = await fetch_user_roles(self.bot, self.guild_id, user_id, self._guild_roles)
            else:
                # Try to determine if user is a bot from cached user info
                try:
                    user_obj = await self.bot.rest.fetch_user(user_id)
                    is_bot = user_obj.is_bot
                except Exception:
                    pass
                    
            # Get display name - prioritize cached user name
            discord_name = user if isinstance(user, str) else (
                user.username if hasattr(user, 'username') and user else f"user{user_id}"
            )
                    
            return {
                "user_id": str(user_id),
                "discord_name": discord_name,
                "nickname": member.nickname if member else None,
                "server_nickname": member.display_name if member else None,
                "role_names": role_names,
                "is_bot": is_bot
            }
            
        except Exception as e:
            logger.debug(f"Failed to build user info for {user_id}: {e}")
            return None
            
    async def _build_channel_info(self, channel_id: int) -> Dict[str, Any]:
        """Build channel information dict."""
        channel_info = await fetch_channel_info(self.bot, channel_id)
        
        return {
            "name": channel_info.get("channel_name"),
            "description": channel_info.get("channel_description")
        }
        
    def _build_me_info(self) -> Dict[str, Any]:
        """Build bot's own information."""
        bot_user = self.bot.get_me()
        
        return {
            "bot_name": bot_user.display_name if bot_user else "Bot",
            "bot_id": str(bot_user.id) if bot_user else None
        }