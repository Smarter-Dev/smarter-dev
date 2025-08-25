"""Simple caching system for Discord bot data."""

from __future__ import annotations

import asyncio
import hikari
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class BotCache:
    """Simple cache for frequently accessed Discord data."""
    
    def __init__(self):
        self.guild_roles: Dict[int, Dict[int, str]] = {}  # guild_id -> {role_id: role_name}
        self.channels: Dict[int, Dict] = {}  # channel_id -> channel_info
        self.users: Dict[int, str] = {}  # user_id -> display_name
        self.last_updated: Dict[str, datetime] = {}  # cache_key -> last_update_time
        self.cache_ttl = timedelta(minutes=5)  # Cache for 5 minutes
        
    def _is_expired(self, cache_key: str) -> bool:
        """Check if cache entry is expired."""
        if cache_key not in self.last_updated:
            return True
        return datetime.now() - self.last_updated[cache_key] > self.cache_ttl
        
    async def get_guild_roles(self, bot: hikari.GatewayBot, guild_id: int) -> Dict[int, str]:
        """Get guild roles, fetching from cache or API."""
        cache_key = f"roles_{guild_id}"
        
        if guild_id in self.guild_roles and not self._is_expired(cache_key):
            logger.debug(f"Using cached roles for guild {guild_id}")
            return self.guild_roles[guild_id]
            
        try:
            logger.debug(f"Fetching roles for guild {guild_id}")
            roles = await bot.rest.fetch_roles(guild_id)
            role_dict = {role.id: role.name for role in roles}
            
            self.guild_roles[guild_id] = role_dict
            self.last_updated[cache_key] = datetime.now()
            
            logger.debug(f"Cached {len(role_dict)} roles for guild {guild_id}")
            return role_dict
            
        except Exception as e:
            logger.debug(f"Failed to fetch guild roles for {guild_id}: {e}")
            return self.guild_roles.get(guild_id, {})
            
    async def get_channel_info(self, bot: hikari.GatewayBot, channel_id: int) -> Dict:
        """Get channel info, fetching from cache or API."""
        cache_key = f"channel_{channel_id}"
        
        if channel_id in self.channels and not self._is_expired(cache_key):
            logger.debug(f"Using cached channel info for {channel_id}")
            return self.channels[channel_id]
            
        try:
            logger.debug(f"Fetching channel info for {channel_id}")
            channel = await bot.rest.fetch_channel(channel_id)
            
            # Get channel name
            channel_name = getattr(channel, 'name', None)
            
            # Get channel description/topic
            channel_description = None
            if hasattr(channel, 'topic') and channel.topic:
                channel_description = channel.topic
            
            # Get channel type
            channel_type = str(channel.type) if hasattr(channel, 'type') else 'unknown'
            
            # Check if this is a forum thread
            is_forum_thread = False
            original_poster_id = None
            
            if channel.type == hikari.ChannelType.GUILD_PUBLIC_THREAD:
                try:
                    parent_channel = await bot.rest.fetch_channel(channel.parent_id)
                    if parent_channel.type == hikari.ChannelType.GUILD_FORUM:
                        is_forum_thread = True
                        original_poster_id = channel.owner_id
                except Exception as e:
                    logger.debug(f"Failed to check parent channel for forum thread: {e}")
            
            channel_info = {
                "channel_name": channel_name,
                "channel_description": channel_description,
                "channel_type": channel_type,
                "is_forum_thread": is_forum_thread,
                "original_poster_id": original_poster_id
            }
            
            self.channels[channel_id] = channel_info
            self.last_updated[cache_key] = datetime.now()
            
            logger.debug(f"Cached channel info for {channel_id}: {channel_name}")
            return channel_info
            
        except Exception as e:
            logger.debug(f"Failed to fetch channel info for {channel_id}: {e}")
            return self.channels.get(channel_id, {
                "channel_name": None,
                "channel_description": None,
                "channel_type": "unknown",
                "is_forum_thread": False,
                "original_poster_id": None
            })
            
    async def get_user_name(self, bot: hikari.GatewayBot, user_id: int) -> str:
        """Get user display name, fetching from cache or API."""
        cache_key = f"user_{user_id}"
        
        if user_id in self.users and not self._is_expired(cache_key):
            logger.debug(f"Using cached user name for {user_id}")
            return self.users[user_id]
            
        try:
            logger.debug(f"Fetching user info for {user_id}")
            user = await bot.rest.fetch_user(user_id)
            display_name = user.display_name or user.username
            
            self.users[user_id] = display_name
            self.last_updated[cache_key] = datetime.now()
            
            logger.debug(f"Cached user name for {user_id}: {display_name}")
            return display_name
            
        except Exception as e:
            logger.debug(f"Failed to fetch user info for {user_id}: {e}")
            return self.users.get(user_id, f"user{user_id}")


# Global cache instance
bot_cache = BotCache()