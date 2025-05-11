"""
Smarter Dev Discord Bot implementation using Hikari and Hikari Lightbulb.

This module contains the main bot implementation with command handlers.
"""

import os
import logging
from datetime import datetime
from pathlib import Path

import hikari
import lightbulb

from bot.api_sync import create_synchronizer
from bot.api_client import APIClient

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

    # Create API client and store it in the bot
    api_url = os.environ.get("SMARTER_DEV_API_URL", "http://localhost:8000")
    api_key = os.environ.get("SMARTER_DEV_API_KEY", "TESTING")
    bot.d.api_client = APIClient(api_url, api_key)

    # Load plugins
    bot.load_extensions_from(Path(__file__).parent / "plugins")

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

        # For large guilds, we don't want to sync all members at once
        # Instead, we'll sync them on-demand as they interact with the bot
        try:
            # Get guild member count to decide how to proceed
            guild = await bot.rest.fetch_guild(event.guild_id)
            member_count = guild.approximate_member_count or 0

            if member_count > 1000:
                logger.info(f"Large guild detected with {member_count} members. Will sync users on-demand.")
                # For large guilds, we'll only sync active users as they interact with the bot
                return

            # For smaller guilds, proceed with syncing all members in batches
            logger.info(f"Fetching members for guild {event.guild_id} (approx. {member_count} members)...")

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

    @bot.listen(hikari.MessageCreateEvent)
    async def on_message(event: hikari.MessageCreateEvent) -> None:
        """
        Event fired when a message is created.
        """
        # Ignore messages from bots
        if event.is_bot:
            return

        # Handle file attachments
        if event.message.attachments:
            # Get file extension rules for the guild
            rules = await bot.d.api_client.get_file_extension_rules(event.guild_id)
            if not rules:
                return

            # Process each attachment
            for attachment in event.message.attachments:
                # Get file extension (without the dot)
                extension = attachment.filename.split(".")[-1].lower() if "." in attachment.filename else ""

                # Find matching rule
                rule = next((r for r in rules if r["extension"] == extension), None)

                # If no rule exists, treat as blocked
                if not rule:
                    # Track the blocked attachment
                    await bot.d.api_client.create_file_attachment(
                        guild_id=event.guild_id,
                        channel_id=event.channel_id,
                        message_id=event.message.id,
                        user_id=event.author.id,
                        extension=extension,
                        attachment_url=attachment.url,
                        was_allowed=False,
                        was_deleted=True
                    )

                    # Delete the message
                    await event.message.delete()
                    await event.message.respond(
                        f"File type `.{extension}` is not allowed in this server.",
                        user_mentions=False
                    )
                    return

                # If rule exists but file is not allowed
                if not rule["is_allowed"]:
                    # Track the blocked attachment
                    await bot.d.api_client.create_file_attachment(
                        guild_id=event.guild_id,
                        channel_id=event.channel_id,
                        message_id=event.message.id,
                        user_id=event.author.id,
                        extension=extension,
                        attachment_url=attachment.url,
                        was_allowed=False,
                        was_deleted=True
                    )

                    # Delete the message
                    await event.message.delete()
                    warning = rule["warning_message"] or f"File type `.{extension}` is not allowed in this server."
                    await event.message.respond(warning, user_mentions=False)
                    return

                # If file is allowed but has a warning message
                if rule["warning_message"]:
                    await event.message.respond(rule["warning_message"], user_mentions=False)

                # Track the allowed attachment
                await bot.d.api_client.create_file_attachment(
                    guild_id=event.guild_id,
                    channel_id=event.channel_id,
                    message_id=event.message.id,
                    user_id=event.author.id,
                    extension=extension,
                    attachment_url=attachment.url,
                    was_allowed=True,
                    was_deleted=False
                )

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
    @lightbulb.option("force", "Force sync all members even for large guilds", type=bool, required=False, default=False)
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

        await ctx.respond("Syncing guild with API...")

        # Get the guild
        guild = await bot.rest.fetch_guild(ctx.guild_id)

        # Sync the guild
        await api_sync.sync_guild(guild)

        # Check if this is a large guild
        member_count = guild.approximate_member_count or 0
        force_sync = ctx.options.force if hasattr(ctx.options, 'force') else False

        if member_count > 1000 and not force_sync:
            await ctx.respond(f"This is a large guild with approximately {member_count} members. "
                             f"Syncing all members would be resource-intensive. "
                             f"Users will be synced on-demand as they interact with the bot. "
                             f"Use `/sync force:true` to force a full sync if needed.")
            return

        # Get and sync all members in batches of 100
        await ctx.respond("Fetching all members...")
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
