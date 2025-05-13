"""
Smarter Dev Discord Bot implementation using Hikari and Hikari Lightbulb.

This module contains the main bot implementation with command handlers.
"""

import os
import logging
from datetime import datetime
from pathlib import Path
import asyncio
import httpx

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

    async def heartbeat_task():
        api_url = os.environ.get("SMARTER_DEV_API_URL", "http://localhost:8000")
        while True:
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(f"{api_url}/api/bot/heartbeat")
            except Exception as e:
                print(f"Heartbeat failed: {e}")
            await asyncio.sleep(60)

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

        # Start heartbeat task
        bot.loop.create_task(heartbeat_task())

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

        # Check file extension rules
        file_rules = await bot.d.api_client.get_file_extension_rules(event.guild_id)
        print(f"Checking file rules for guild {event.guild_id}: {file_rules}")  # Debug log

        blocked_attachments = []
        allowed_warnings = [] # Collect warnings for allowed files
        block_reasons = [] # Collect reasons for blocking

        for attachment in event.message.attachments:
            # Get file extension (without the dot)
            extension = os.path.splitext(attachment.filename)[1].lower().lstrip('.')
            if not extension:
                continue

            # Find matching rule
            matching_rule = next((rule for rule in file_rules if rule["extension"].lower() == extension), None)
            print(f"Checking extension {extension} against rules: {matching_rule}")  # Debug log

            if matching_rule:
                if not matching_rule["is_allowed"]:
                    blocked_attachments.append(attachment)
                    # Use the specific warning message if available, otherwise a default
                    reason = matching_rule.get("warning_message") or f"File extension `.{extension}` is blocked by a rule."
                    block_reasons.append(reason)
                elif matching_rule["is_allowed"] and matching_rule["warning_message"]:
                    # File is allowed, but has a warning message - collect the warning
                    allowed_warnings.append(matching_rule['warning_message'])
            else:
                # No rule exists for this extension, block it by default
                blocked_attachments.append(attachment)
                block_reasons.append(f"`.{extension}`")

        # Handle blocked attachments OR allowed warnings
        if blocked_attachments:
            # Format the reasons for deletion
            *unique_reasons, last_reason = sorted(set(block_reasons))
            unique_reasons.append(f"& {last_reason}" if len(unique_reasons) > 0 else last_reason)
            formatted_reasons = ", ".join(unique_reasons) if len(unique_reasons) > 2 else " ".join(unique_reasons)
            delete_message_content = f"-# <@{event.author.id}> Your message was deleted because the {formatted_reasons} attachement type{'s are' if len(unique_reasons) > 1 else ' is'} blocked for user safety."

            # Send the formatted deletion reason message
            await bot.rest.create_message(event.channel_id, delete_message_content, user_mentions=True)

            # Delete the original message
            try:
                await event.message.delete()
            except hikari.NotFound:
                pass  # Message was already deleted
            except hikari.Forbidden:
                # Maybe send a message indicating lack of perms, but avoid double messaging if delete_message_content already sent.
                logger.warning(f"Missing permissions to delete message {event.message.id} in guild {event.guild_id}")
            except Exception as e:
                logger.error(f"Error deleting message {event.message.id}: {e}")
                # Avoid sending another error message if the reason message already went through.

        elif allowed_warnings: # Only send warnings for allowed files if the message wasn't deleted
            unique_warnings = set(allowed_warnings)
            formatted_warnings = "\\n".join([f"-# {warning}" for warning in unique_warnings])
            await bot.rest.create_message(event.channel_id, formatted_warnings)

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
