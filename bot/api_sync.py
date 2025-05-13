"""
API synchronization module for the Smarter Dev Discord Bot.

This module handles synchronization between Discord and the website API,
ensuring that user and guild data is kept up-to-date.
"""

import logging
import os
from datetime import datetime
from typing import Optional, Dict, Any, List

import hikari

from bot.api_client import APIClient
from bot.api_models import Guild, DiscordUser, BytesConfig

# Configure logging
logger = logging.getLogger("bot.api_sync")


def _get_avatar_url(user: hikari.User | hikari.PartialUser) -> Optional[str]:
    """
    Helper function to safely get avatar URL from different user object types.

    Args:
        user: A User or PartialUser object

    Returns:
        The avatar URL as a string, or None if not available
    """
    # Full User objects have avatar_url attribute
    if hasattr(user, 'avatar_url') and user.avatar_url:
        return str(user.avatar_url)

    # PartialUser objects have avatar_hash but no direct URL
    # We can't construct the URL without additional information, so return None
    return None


class APISynchronizer:
    """
    Handles synchronization between Discord and the website API.

    This class provides methods to sync guilds and users with the website API,
    ensuring that data is kept up-to-date.
    """

    def __init__(self, api_url: str, api_key: str):
        """
        Initialize the API synchronizer.

        Args:
            api_url: The base URL of the API
            api_key: The API key to use for authentication
        """
        self.api_client = APIClient(api_url, api_key)
        self.guild_cache: Dict[int, Guild] = {}  # discord_id -> Guild
        self.user_cache: Dict[int, DiscordUser] = {}  # discord_id -> DiscordUser

    async def close(self):
        """Close the underlying API client."""
        await self.api_client.close()

    async def initialize_cache(self):
        """
        Initialize the cache with existing guilds from the API.

        This helps avoid unnecessary API calls for guilds that already exist.
        Users are loaded on-demand to avoid loading all users at startup,
        which would be inefficient for large guilds.
        """
        try:
            # Get all guilds
            guilds = await self.api_client.get_guilds()
            for guild in guilds:
                self.guild_cache[guild.discord_id] = guild
            logger.info(f"Initialized guild cache with {len(self.guild_cache)} guilds")

            # We no longer load all users at startup to avoid memory issues with large guilds
            # Users will be loaded on-demand when needed
            logger.info("User cache will be populated on-demand")
        except Exception as e:
            logger.error(f"Error initializing cache: {e}")

    async def sync_guild(self, discord_guild: hikari.Guild) -> Guild:
        """
        Sync a Discord guild with the website API.

        Args:
            discord_guild: The Discord guild to sync

        Returns:
            The synced Guild object from the API
        """
        try:
            # Check if we already have this guild in our cache
            if discord_guild.id in self.guild_cache:
                # Update existing guild
                guild = self.guild_cache[discord_guild.id]

                # Convert icon_url to string if it's a URL object
                icon_url = None
                if discord_guild.icon_url:
                    icon_url = str(discord_guild.icon_url)

                # Check if any fields need updating
                if (guild.name != discord_guild.name or
                    guild.icon_url != icon_url):

                    # Update fields
                    guild.name = discord_guild.name
                    guild.icon_url = icon_url

                    # Send update to API
                    updated_guild = await self.api_client.update_guild(guild)
                    self.guild_cache[discord_guild.id] = updated_guild
                    logger.info(f"Updated guild in API: {updated_guild.name} ({updated_guild.discord_id})")
                    return updated_guild

                return guild
            else:
                # Convert icon_url to string if it's a URL object
                icon_url = None
                if discord_guild.icon_url:
                    icon_url = str(discord_guild.icon_url)

                # Create new guild
                new_guild = Guild(
                    id=None,  # API will assign ID
                    discord_id=discord_guild.id,
                    name=discord_guild.name,
                    icon_url=icon_url,
                    joined_at=datetime.now()
                )

                # Send to API
                created_guild = await self.api_client.create_guild(new_guild)
                self.guild_cache[discord_guild.id] = created_guild
                logger.info(f"Created guild in API: {created_guild.name} ({created_guild.discord_id})")

                # Create default bytes configuration
                try:
                    bytes_config = BytesConfig(
                        guild_id=created_guild.id,
                        starting_balance=100,
                        daily_earning=10,
                        max_give_amount=50,
                        cooldown_minutes=1440,  # 24 hours
                        squad_join_bytes_required=100,
                        squad_switch_cost=50
                    )
                    await self.api_client.create_bytes_config(bytes_config)
                    logger.info(f"Created default bytes configuration for guild {created_guild.name}")
                except Exception as e:
                    logger.error(f"Error creating default bytes configuration for guild {created_guild.name}: {e}")

                return created_guild

        except Exception as e:
            logger.error(f"Error syncing guild {discord_guild.id}: {e}")
            raise

    async def get_user(self, user_id: int) -> Optional[DiscordUser]:
        """
        Get a user by ID, either from cache or from the API.

        Args:
            user_id: The Discord user ID

        Returns:
            The DiscordUser object or None if not found
        """
        try:
            # Check if we already have this user in our cache
            if user_id in self.user_cache:
                return self.user_cache[user_id]

            # Not in cache, try to get from API
            try:
                user = await self.api_client.get_user(user_id)
                if user:
                    # Add to cache
                    self.user_cache[user_id] = user
                    return user
            except Exception as e:
                logger.debug(f"User {user_id} not found in API: {e}")
                return None
        except Exception as e:
            logger.error(f"Error getting user {user_id}: {e}")
            return None

    async def sync_user(self, discord_user: hikari.User | hikari.PartialUser, guild_id: Optional[int] = None, joined_at: Optional[datetime] = None) -> DiscordUser:
        """
        Sync a Discord user with the website API.

        Args:
            discord_user: The Discord user to sync
            guild_id: Optional guild ID if this sync is related to a specific guild
            joined_at: Optional datetime when the user joined Discord

        Returns:
            The synced DiscordUser object from the API
        """
        try:
            # Check if we already have this user in our cache
            if discord_user.id in self.user_cache:
                # Update existing user
                user = self.user_cache[discord_user.id]

                # Check if any fields need updating
                # Get avatar URL safely from different user object types
                avatar_url = _get_avatar_url(discord_user)

                # Get discriminator if available
                discriminator = discord_user.discriminator if hasattr(discord_user, 'discriminator') else None

                if (user.username != discord_user.username or
                    user.avatar_url != avatar_url or
                    user.discriminator != discriminator):

                    # Update fields
                    user.username = discord_user.username
                    user.discriminator = discriminator
                    user.avatar_url = avatar_url

                    # Send update to API
                    updated_user = await self.api_client.update_user(user)
                    self.user_cache[discord_user.id] = updated_user
                    logger.info(f"Updated user in API: {updated_user.username} ({updated_user.discord_id})")
                    return updated_user

                return user
            else:
                # Get avatar URL safely from different user object types
                avatar_url = _get_avatar_url(discord_user)

                # Create new user
                new_user = DiscordUser(
                    id=None,  # API will assign ID
                    discord_id=discord_user.id,
                    username=discord_user.username,
                    discriminator=discord_user.discriminator if hasattr(discord_user, 'discriminator') else None,
                    avatar_url=avatar_url,
                    # Set created_at to the date the user joined Discord if available
                    created_at=joined_at or datetime.now()
                )

                # Send to API
                created_user = await self.api_client.create_user(new_user)
                self.user_cache[discord_user.id] = created_user
                logger.info(f"Created user in API: {created_user.username} ({created_user.discord_id})")
                return created_user

        except Exception as e:
            logger.error(f"Error syncing user {discord_user.id}: {e}")
            raise

    async def batch_sync_users(self, discord_users: List[hikari.User | hikari.PartialUser], guild_id: Optional[int] = None, joined_at_dates: Optional[List[Optional[datetime]]] = None) -> Dict[str, Any]:
        """
        Sync multiple Discord users with the website API in a single batch request.

        Args:
            discord_users: List of Discord users to sync
            guild_id: Optional guild ID if this sync is related to a specific guild
            joined_at_dates: Optional list of datetime objects when each user joined Discord

        Returns:
            Dictionary with results including synced users and counts
        """
        try:
            if not discord_users:
                return {"users": [], "created": 0, "updated": 0, "total": 0}

            # Prepare user objects for the batch request
            users_to_sync = []
            for i, discord_user in enumerate(discord_users):
                # Get joined_at date if available
                joined_at = None
                if joined_at_dates and i < len(joined_at_dates):
                    joined_at = joined_at_dates[i]
                # Get avatar URL safely from different user object types
                avatar_url = _get_avatar_url(discord_user)

                # Create a DiscordUser object for each user
                user = DiscordUser(
                    id=self.user_cache[discord_user.id].id if discord_user.id in self.user_cache else None,
                    discord_id=discord_user.id,
                    username=discord_user.username,
                    discriminator=discord_user.discriminator if hasattr(discord_user, 'discriminator') else None,
                    avatar_url=avatar_url,
                    # Set created_at to the date the user joined Discord if available
                    created_at=joined_at or datetime.now()
                )
                users_to_sync.append(user)

            # Send batch request to API
            result = await self.api_client.batch_create_users(users_to_sync)

            # Update cache with the returned users, but only if we're dealing with a reasonable number
            # For large guilds, we don't want to cache all users at once
            if len(result["users"]) <= 1000:  # Only cache if less than 1000 users
                for user in result["users"]:
                    self.user_cache[user.discord_id] = user
                logger.info(f"Added {len(result['users'])} users to cache")
            else:
                # For large batches, only cache users that were actually updated or created
                # This helps keep the cache size manageable
                logger.info(f"Large batch of {len(result['users'])} users - not adding all to cache")

            logger.info(f"Batch synced {result['total']} users: {result['created']} created, {result['updated']} updated")
            return result

        except Exception as e:
            logger.error(f"Error batch syncing {len(discord_users)} users: {e}")
            raise

# Factory function to create a synchronizer with environment variables
def create_synchronizer() -> APISynchronizer:
    """
    Create an API synchronizer using environment variables.

    Returns:
        An initialized APISynchronizer
    """
    # Get API URL and key from environment variables
    api_url = os.environ.get("SMARTER_DEV_API_URL", "http://localhost:8000")
    api_key = os.environ.get("SMARTER_DEV_API_KEY", "TESTING")

    # Check if we're in local development mode
    if os.environ.get("SMARTER_DEV_LOCAL", "0") == "1":
        logger.info("Running in local development mode")
        api_key = "TESTING"

    return APISynchronizer(api_url, api_key)
