"""
Smarter Dev Discord Bot implementation using Hikari and Hikari Lightbulb.

This module contains the main bot implementation with command handlers.
"""

import os
import logging
from datetime import datetime

import hikari
import lightbulb

from bot.api_sync import create_synchronizer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("bot")

# Create the bot instance
def create_bot() -> lightbulb.BotApp:
    """
    Create and configure the bot instance.

    Returns:
        A configured BotApp instance
    """
    # Get the token from environment variable
    token = os.environ.get("SMARTER_DEV_BOT_TOKEN")

    if not token:
        raise ValueError(
            "No token provided. Please set the SMARTER_DEV_BOT_TOKEN environment variable."
        )

    # Create the bot with intents
    # Note: GUILD_MEMBERS and GUILD_PRESENCES are privileged intents that need to be enabled
    # in the Discord Developer Portal: https://discord.com/developers/applications
    bot = lightbulb.BotApp(
        token=token,
        prefix="!",  # Default command prefix
        intents=(
            hikari.Intents.ALL_UNPRIVILEGED  # Standard intents
            | hikari.Intents.MESSAGE_CONTENT  # For message content
            | hikari.Intents.GUILD_MEMBERS    # For member events (privileged)
            | hikari.Intents.GUILD_PRESENCES  # For presence updates (privileged)
        ),
        logs={
            "version": 1,
            "incremental": True,
            "loggers": {
                "hikari": {"level": "INFO"},
                "lightbulb": {"level": "INFO"},
            },
        },
    )

    # Create API synchronizer
    api_sync = create_synchronizer()

    # Register event listeners
    @bot.listen(hikari.StartedEvent)
    async def on_started(event: hikari.StartedEvent) -> None:
        """
        Event fired when the bot starts.
        """
        logger.info("Bot has started!")

        # Initialize API synchronizer cache
        await api_sync.initialize_cache()

        # Log the number of guilds the bot is in
        guilds = await bot.rest.fetch_my_guilds()
        logger.info(f"Bot is in {len(list(guilds))} guilds")

    @bot.listen(hikari.GuildJoinEvent)
    async def on_guild_join(event: hikari.GuildJoinEvent) -> None:
        """
        Event fired when the bot joins a new guild.
        """
        logger.info(f"Joined guild: {event.guild.name} ({event.guild_id})")

        # Sync the guild with the API
        await api_sync.sync_guild(event.guild)

        # Fetch and sync all members in batches of 100
        try:
            logger.info(f"Fetching members for guild {event.guild_id}...")

            # Get all members
            members = await bot.rest.fetch_members(event.guild_id)
            member_list = [member for member in members if not member.is_bot]

            # Process in batches of 100
            batch_size = 100
            total_synced = 0

            for i in range(0, len(member_list), batch_size):
                batch = member_list[i:i + batch_size]
                logger.info(f"Syncing batch of {len(batch)} members (batch {i//batch_size + 1}/{(len(member_list) + batch_size - 1)//batch_size})")

                # Extract user objects and joined_at dates from members
                users = [member.user for member in batch]
                joined_at_dates = [member.joined_at for member in batch]

                # Batch sync the users with their joined_at dates
                result = await api_sync.batch_sync_users(users, event.guild_id, joined_at_dates)
                total_synced += result["total"]

            logger.info(f"Completed syncing {total_synced} members for guild {event.guild.name}")

        except Exception as e:
            logger.error(f"Error syncing members for guild {event.guild_id}: {e}")

    @bot.listen(hikari.GuildUpdateEvent)
    async def on_guild_update(event: hikari.GuildUpdateEvent) -> None:
        """
        Event fired when a guild is updated.
        """
        logger.info(f"Guild updated: {event.guild.name} ({event.guild_id})")

        # Sync the guild with the API
        await api_sync.sync_guild(event.guild)

    @bot.listen(hikari.MemberCreateEvent)
    async def on_member_join(event: hikari.MemberCreateEvent) -> None:
        """
        Event fired when a user joins a guild.
        """
        logger.info(f"User {event.user.username} ({event.user.id}) joined guild {event.guild_id}")

        # Sync the user with the API
        # Pass the joined_at date from the member object
        await api_sync.sync_user(event.user, event.guild_id, event.member.joined_at)

    @bot.listen(hikari.MemberUpdateEvent)
    async def on_member_update(event: hikari.MemberUpdateEvent) -> None:
        """
        Event fired when a member is updated in a guild.
        """
        logger.info(f"Member updated: {event.user.username} ({event.user.id}) in guild {event.guild_id}")

        # Sync the user with the API
        await api_sync.sync_user(event.user, event.guild_id)

    @bot.listen(hikari.PresenceUpdateEvent)
    async def on_presence_update(event: hikari.PresenceUpdateEvent) -> None:
        """
        Event fired when a user's presence is updated.
        This can include username or avatar changes.
        """
        # Only process if the user object is included (indicating a user update)
        if event.user is not None:
            logger.info(f"User updated: {event.user.username} ({event.user_id})")

            # Sync the user with the API
            await api_sync.sync_user(event.user, event.guild_id)

    # Register shutdown handler
    @bot.listen(hikari.StoppingEvent)
    async def on_stopping(event: hikari.StoppingEvent) -> None:
        """
        Event fired when the bot is shutting down.
        """
        logger.info("Bot is shutting down, closing API client...")
        await api_sync.close()

    # Register commands
    @bot.command
    @lightbulb.command("ping", "Checks if the bot is alive")
    @lightbulb.implements(lightbulb.PrefixCommand, lightbulb.SlashCommand)
    async def ping_command(ctx: lightbulb.Context) -> None:
        """
        Ping command to check if the bot is alive.
        Responds with "Pong!" and the latency.
        """
        latency = bot.heartbeat_latency * 1000
        await ctx.respond(f"Pong! Latency: {latency:.2f}ms")

    # Add a sync command for admins to manually trigger synchronization
    @bot.command
    @lightbulb.add_checks(lightbulb.owner_only)
    @lightbulb.command("sync", "Manually sync the current guild with the API")
    @lightbulb.implements(lightbulb.PrefixCommand, lightbulb.SlashCommand)
    async def sync_command(ctx: lightbulb.Context) -> None:
        """
        Sync command to manually sync the current guild with the API.
        Only available to the bot owner.
        """
        if ctx.guild_id is None:
            await ctx.respond("This command can only be used in a guild.")
            return

        await ctx.respond("Syncing guild and members with API...")

        # Get the guild
        guild = await bot.rest.fetch_guild(ctx.guild_id)

        # Sync the guild
        await api_sync.sync_guild(guild)

        # Get and sync all members in batches of 100
        members = await bot.rest.fetch_members(ctx.guild_id)
        member_list = [member for member in members if not member.is_bot]

        # Process in batches of 100
        batch_size = 100
        total_synced = 0

        await ctx.respond(f"Syncing {len(member_list)} members in batches of {batch_size}...")

        for i in range(0, len(member_list), batch_size):
            batch = member_list[i:i + batch_size]
            current_batch = i//batch_size + 1
            total_batches = (len(member_list) + batch_size - 1)//batch_size

            # Extract user objects and joined_at dates from members
            users = [member.user for member in batch]
            joined_at_dates = [member.joined_at for member in batch]

            # Batch sync the users with their joined_at dates
            result = await api_sync.batch_sync_users(users, ctx.guild_id, joined_at_dates)
            total_synced += result["total"]

            # Update progress every few batches
            if current_batch % 5 == 0 or current_batch == total_batches:
                await ctx.respond(f"Progress: {current_batch}/{total_batches} batches processed ({total_synced} members synced so far)")

        await ctx.respond(f"Sync complete! Synced guild and {total_synced} members.")

    return bot
