"""
Bytes plugin for the Smarter Dev Discord bot.

This plugin provides commands for the Bytes system, which allows users to give
each other bytes as a form of recognition and earn roles based on their total bytes received.
"""

import asyncio
import logging
import math
from datetime import datetime, timedelta, UTC
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict

import hikari
import lightbulb
from lightbulb import commands, context

from bot.api_client import APIClient
from bot.api_models import Bytes, BytesConfig, BytesRole, BytesCooldown, DiscordUser, GuildMember

# Create plugin
bytes_plugin = lightbulb.Plugin("Bytes")
logger = logging.getLogger("bot.plugins.bytes")

# Cache for bytes configuration to avoid frequent API calls
# Format: {guild_id: (config, timestamp)}
bytes_config_cache: Dict[int, tuple] = {}

# Cache for guild members to avoid frequent API calls
# Format: {(user_id, guild_id): (member, timestamp)}
guild_member_cache: Dict[Tuple[int, int], tuple] = {}

# Cache for user data to avoid frequent API calls
# Format: {user_id: (user, timestamp)}
user_cache: Dict[int, tuple] = {}

# Cache for bytes balance to avoid frequent API calls
# Format: {(user_id, guild_id): (balance_info, timestamp)}
bytes_balance_cache: Dict[Tuple[int, int], tuple] = {}

# Cache for daily bytes eligibility to avoid frequent API calls
# Format: {(user_id, guild_id): (next_eligible_timestamp, timestamp)}
daily_bytes_eligibility_cache: Dict[Tuple[int, int], tuple] = {}

# Cache timeout in seconds
CACHE_TIMEOUT = 300  # 5 minutes

# System user ID cache
system_user_id: Optional[int] = None

# Create a bytes command group
@bytes_plugin.command
@lightbulb.command("bytes", "Commands for managing bytes")
@lightbulb.implements(commands.SlashCommandGroup)
async def bytes_group(ctx: context.Context) -> None:
    # This is just a command group and doesn't do anything on its own
    pass


def format_bytes(bytes_amount: int) -> str:
    """
    Format bytes into a human-readable format (KB/MB/GB) and a formatted string with commas.

    Args:
        bytes_amount: The amount of bytes to format

    Returns:
        A tuple containing (formatted_unit_string, formatted_bytes_string)
    """
    # Format the raw bytes with commas
    formatted_bytes = f"{bytes_amount:,}"

    # Convert to KB/MB/GB as appropriate
    if bytes_amount < 512:  # Less than 0.5 KB
        power = 0
        symbol = "bytes"

    elif bytes_amount < 512 * 2**10:  # Less than 0.5 MB
        power = 10
        symbol = "KB"

    elif bytes_amount < 512 * 2**20:  # Less than 0.5 GB
        power = 20
        symbol = "MB"

    else:  # GB or larger
        power = 30
        symbol = "GB"

    value = bytes_amount / 2**power
    formatted_output = f"{value:.2f} {symbol}" if power > 0 else f"{bytes_amount} {symbol}"
    if int(round(value, 2) * 2 ** power) != bytes_amount:
        formatted_output += f" ({formatted_bytes})"

    return formatted_output

async def get_bytes_config(client: APIClient, guild_id: int) -> BytesConfig:
    """
    Get bytes configuration for a guild, with caching.

    Args:
        client: API client
        guild_id: Guild ID

    Returns:
        BytesConfig object
    """
    now = datetime.now().timestamp()

    # Check cache first
    if guild_id in bytes_config_cache:
        config, timestamp = bytes_config_cache[guild_id]
        if now - timestamp < CACHE_TIMEOUT:
            return config

    # Get from API
    try:
        config = await client.get_bytes_config(guild_id)
        bytes_config_cache[guild_id] = (config, now)
        return config
    except Exception as e:
        logger.error(f"Error getting bytes config for guild {guild_id}: {e}")
        # Return default config
        return BytesConfig(guild_id=guild_id)


async def get_user_bytes_info(client: APIClient, user_id: int, guild_id: int) -> Dict[str, Any]:
    """
    Get a user's bytes information, with caching.

    Args:
        client: API client
        user_id: Discord user ID
        guild_id: Guild ID

    Returns:
        Dictionary with bytes information
    """
    now = datetime.now().timestamp()
    cache_key = (user_id, guild_id)

    # Check cache first
    if cache_key in bytes_balance_cache:
        balance_info, timestamp = bytes_balance_cache[cache_key]
        if now - timestamp < CACHE_TIMEOUT:
            logger.info(f"Using cached bytes balance for user {user_id} in guild {guild_id}")
            return balance_info

    try:
        logger.info(f"Making API request to /api/bytes/balance/{user_id}?guild_id={guild_id}")
        response = await client._request("GET", f"/api/bytes/balance/{user_id}?guild_id={guild_id}")
        result = await client._get_json(response)
        logger.info(f"API response for bytes balance: {result}")

        # Update cache
        bytes_balance_cache[cache_key] = (result, now)

        return result
    except Exception as e:
        logger.error(f"Error getting bytes info for user {user_id} in guild {guild_id}: {e}")
        default_result = {
            "user_id": user_id,
            "bytes_balance": 0,
            "bytes_received": 0,
            "bytes_given": 0,
            "earned_roles": []
        }

        # Cache the default result to avoid repeated failed requests
        bytes_balance_cache[cache_key] = (default_result, now)

        return default_result


async def check_for_earned_roles(client: APIClient, bot: lightbulb.BotApp, user_id: int, guild_id: int, channel_id: Optional[int] = None) -> None:
    """
    Check if a user has earned any roles based on their bytes balance and add them.

    Args:
        client: API client
        bot: Bot instance
        user_id: Discord user ID
        guild_id: Guild ID
        channel_id: Optional channel ID to send the award message to (defaults to system channel)
    """
    try:
        # Get the user's bytes info
        bytes_info = await get_user_bytes_info(client, user_id, guild_id)

        # Check if there are any earned roles
        earned_roles = bytes_info.get("earned_roles", [])
        if not earned_roles:
            logger.info(f"No earned roles found for user {user_id} in guild {guild_id}")
            return

        # Get the guild
        guild = await bot.rest.fetch_guild(guild_id)
        if not guild:
            logger.error(f"Could not fetch guild {guild_id}")
            return

        # Get the user details from the API to get the Discord user ID
        try:
            # Get the user from Discord API using the Discord user ID
            user = await bot.rest.fetch_user(user_id)
            if not user:
                logger.error(f"Could not fetch user with Discord ID {user_id}")
                return
        except Exception as e:
            logger.error(f"Error fetching user {user_id}: {e}")
            return

        # Add roles to user
        role_mentions = []
        for role_data in earned_roles:
            role_id = role_data["role_id"]
            role = guild.get_role(role_id)
            if role:
                # Check if user already has the role
                try:
                    member = await bot.rest.fetch_member(guild_id, user.id)
                    if role_id not in member.role_ids:
                        role_mentions.append(role.mention)
                        # Add role to user
                        try:
                            await bot.rest.add_role_to_member(guild_id, user.id, role_id, reason="Bytes role reward")
                            logger.info(f"Added role {role_id} to user {user.id}")
                        except Exception as e:
                            logger.error(f"Error adding role {role_id} to user {user.id}: {e}")
                    else:
                        logger.info(f"User {user.id} already has role {role_id}, not sending award message")
                except Exception as e:
                    logger.error(f"Error checking if user {user.id} has role {role_id}: {e}")

        # If roles were added, send a message
        if role_mentions:
            roles_str = ", ".join(role_mentions)
            congrats_embed = hikari.Embed(
                title="🎉 New Role Earned!",
                description=f"{user.mention} has earned new role{'s' if len(role_mentions) > 1 else ''}: {roles_str}",
                color=hikari.Color.from_rgb(255, 215, 0)  # Gold color
            )
            try:
                # Use the provided channel_id if available, otherwise fall back to system channel
                message_channel_id = channel_id if channel_id else guild.system_channel_id
                if message_channel_id:
                    await bot.rest.create_message(message_channel_id, embed=congrats_embed)
                    logger.info(f"Sent role earned message to channel {message_channel_id}")
                else:
                    logger.info("No channel available to send role earned message")
            except Exception as e:
                logger.error(f"Error sending role earned message: {e}")

    except Exception as e:
        logger.error(f"Error checking for earned roles: {e}")


async def check_cooldown(client: APIClient, user_id: int, guild_id: int) -> Dict[str, Any]:
    """
    Check if a user is on cooldown for giving bytes.

    Args:
        client: API client
        user_id: Discord user ID
        guild_id: Guild ID

    Returns:
        Dictionary with cooldown information or None if no cooldown
    """
    try:
        response = await client._request("GET", f"/api/bytes/cooldown/{user_id}/{guild_id}")
        data = await client._get_json(response)
        return data
    except Exception:
        # No cooldown found
        return None


async def get_leaderboard(client: APIClient, guild_id: int, limit: int = 10) -> Dict[str, Any]:
    """
    Get bytes leaderboard for a guild.

    Args:
        client: API client
        guild_id: Guild ID
        limit: Number of users to include

    Returns:
        Dictionary with leaderboard information
    """
    try:
        response = await client._request("GET", f"/api/bytes/leaderboard/{guild_id}?limit={limit}")
        return await client._get_json(response)
    except Exception as e:
        logger.error(f"Error getting bytes leaderboard for guild {guild_id}: {e}")
        return {
            "guild_id": guild_id,
            "leaderboard": []
        }


@bytes_group.child
@lightbulb.option("amount", "Amount of bytes to send", type=int, min_value=1, required=True)
@lightbulb.option("user", "User to send bytes to", type=hikari.User, required=True)
@lightbulb.option("reason", "Reason for sending bytes", type=str, required=False)
@lightbulb.command("send", "Send your bytes")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def bytes_give(ctx: context.SlashContext) -> None:
    """
    Give bytes to another user.
    """
    # Get API client
    client = ctx.bot.d.api_client

    # Get parameters
    receiver = ctx.options.user
    amount = ctx.options.amount
    reason = ctx.options.reason

    # Check if user is trying to give bytes to themselves
    if receiver.id == ctx.author.id:
        await ctx.respond("You can't give bytes to yourself!", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Check if user is trying to give bytes to a bot
    if receiver.is_bot:
        await ctx.respond("You can't give bytes to a bot!", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Get guild ID
    guild_id = ctx.guild_id
    if not guild_id:
        await ctx.respond("This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Get bytes config
    config = await get_bytes_config(client, guild_id)

    # Check if amount is within limits
    if amount > config.max_give_amount:
        await ctx.respond(f"You can only give up to {config.max_give_amount} bytes at once!", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Check cooldown
    cooldown = await check_cooldown(client, ctx.author.id, guild_id)
    if cooldown and cooldown.get("cooldown_active", False):
        minutes_left = cooldown.get("minutes_left", 0)
        hours = minutes_left // 60
        minutes = minutes_left % 60

        if hours > 0:
            time_str = f"{hours} hour{'s' if hours != 1 else ''} and {minutes} minute{'s' if minutes != 1 else ''}"
        else:
            time_str = f"{minutes} minute{'s' if minutes != 1 else ''}"

        await ctx.respond(
            f"You're on cooldown! You can send bytes again in {time_str}.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    # Get user info
    try:
        # Get giver
        giver_response = await client._request("GET", f"/api/users?discord_id={ctx.author.id}")
        giver_data = await client._get_json(giver_response)
        if not giver_data.get("users"):
            await ctx.respond("Error: Your user profile was not found. Please try again later.", flags=hikari.MessageFlag.EPHEMERAL)
            return
        giver = giver_data["users"][0]

        # Get receiver
        receiver_response = await client._request("GET", f"/api/users?discord_id={receiver.id}")
        receiver_data = await client._get_json(receiver_response)
        if not receiver_data.get("users"):
            await ctx.respond(f"Error: User {receiver.username} was not found.", flags=hikari.MessageFlag.EPHEMERAL)
            return
        receiver_user = receiver_data["users"][0]

        # Check if giver has enough bytes
        if giver["bytes_balance"] < amount:
            balance_formatted = format_bytes(giver['bytes_balance'])
            await ctx.respond(f"You don't have enough bytes! Your balance: {balance_formatted}", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Create bytes transaction
        bytes_obj = Bytes(
            giver_id=ctx.author.id,  # Use Discord user ID
            receiver_id=receiver.id,  # Use Discord user ID
            guild_id=int(guild_id),
            amount=amount,
            reason=reason or "No reason provided"
        )

        # Send transaction to API
        response = await client._request("POST", "/api/bytes", data=client._dict_from_model(bytes_obj))
        result = await client._get_json(response)

        # Check for earned roles
        earned_roles = result.get("earned_roles", [])

        # Format the bytes values
        amount_formatted = format_bytes(amount)
        giver_balance_formatted = format_bytes(result['giver_balance'])

        # Create ephemeral confirmation message for the sender
        confirmation_message = f"Transaction confirmed! You sent {amount_formatted} to {receiver.username}. Your new balance: {giver_balance_formatted}"

        # Send ephemeral confirmation to the sender
        await ctx.respond(confirmation_message, flags=hikari.MessageFlag.EPHEMERAL)

        # Create public notification message for the receiver
        # Include reason if provided
        if reason:
            notification_message = f"{receiver.mention} {ctx.author.mention} sent you {amount_formatted} for {reason}"
        else:
            notification_message = f"{receiver.mention} {ctx.author.mention} sent you {amount_formatted}"

        # Send public notification
        await ctx.get_channel().send(notification_message)

        # Check for earned roles and add them to the user
        # Pass the current channel ID so the award message is sent in the same channel
        await check_for_earned_roles(client, ctx.bot, receiver.id, guild_id, ctx.channel_id)

    except Exception as e:
        logger.error(f"Error giving bytes: {e}")
        await ctx.respond("An error occurred while giving bytes. Please try again later.", flags=hikari.MessageFlag.EPHEMERAL)


@bytes_group.child
@lightbulb.option("user", "User to read bytes details for", type=hikari.User, required=False)
@lightbulb.command("read", "Read a details of a user's bytes")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def bytes_lookup(ctx: context.SlashContext) -> None:
    """
    Check a user's bytes cache.
    """
    # Get API client
    client = ctx.bot.d.api_client

    # Get user to check
    target_user = ctx.options.user or ctx.author

    # Get guild ID
    guild_id = ctx.guild_id
    if not guild_id:
        await ctx.respond("This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        # Get user from API
        logger.info(f"Looking up user with discord_id: {target_user.id}")
        user_response = await client._request("GET", f"/api/users?discord_id={target_user.id}")
        user_data = await client._get_json(user_response)
        logger.info(f"API response for user lookup: {user_data}")

        if not user_data.get("users"):
            await ctx.respond(f"User {target_user.username} was not found in the database.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        user_id = user_data["users"][0]["id"]
        logger.info(f"Found user with ID: {user_id}")

        # Get bytes info
        logger.info(f"Getting bytes info for user_id: {user_id}, guild_id: {guild_id}")
        bytes_info = await get_user_bytes_info(client, user_id, guild_id)
        logger.info(f"Bytes info response: {bytes_info}")

        # Try multiple approaches to get the user's display name
        guild = ctx.get_guild()
        member = guild.get_member(target_user.id)

        # Create embed
        embed = hikari.Embed(
            title=f"{member.display_name}'s Bytes",
            color=hikari.Color.from_rgb(87, 242, 135)  # Green color
        )

        # The API returns bytes_received as the sum of all bytes received by the user
        # and bytes_given as the sum of all bytes given by the user

        # Format the bytes values
        balance_formatted = format_bytes(bytes_info['bytes_balance'])
        received_formatted = format_bytes(bytes_info['bytes_received'])
        given_formatted = format_bytes(bytes_info['bytes_given'])

        # Add fields with formatted values - set inline=False to stack them
        embed.add_field(name="In Cache", value=balance_formatted, inline=False)
        embed.add_field(name="Received", value=received_formatted, inline=False) # Roles are based on this value
        embed.add_field(name="Sent", value=given_formatted, inline=False)

        # Add earned roles
        if bytes_info.get("earned_roles"):
            roles_text = ""
            for role_data in bytes_info["earned_roles"]:
                role_id = role_data["role_id"]
                role = ctx.get_guild().get_role(role_id)
                if role:
                    roles_text += f"{role.mention} ({role_data['bytes_required']} bytes)\n"

            if roles_text:
                embed.add_field(name="Earned Roles", value=roles_text, inline=False)

        # Get next role to earn
        try:
            roles_response = await client._request("GET", f"/api/bytes/roles/{guild_id}")
            roles_data = await client._get_json(roles_response)

            if roles_data.get("roles"):
                # Sort roles by bytes required
                roles = sorted(roles_data["roles"], key=lambda r: r["bytes_required"])

                # Find next role to earn based on bytes_received
                next_role = None
                for role in roles:
                    if role["bytes_required"] > bytes_info["bytes_received"]:
                        next_role = role
                        break

                if next_role:
                    role_id = next_role["role_id"]
                    role = ctx.get_guild().get_role(role_id)
                    if role:
                        bytes_needed = next_role["bytes_required"] - bytes_info["bytes_received"]
                        bytes_needed_formatted = format_bytes(bytes_needed)
                        embed.add_field(
                            name="Next Role",
                            value=f"{role.mention} ({bytes_needed_formatted} more needed)",
                            inline=False
                        )
        except Exception as e:
            logger.error(f"Error getting next role: {e}")

        await ctx.respond(embed=embed)

    except Exception as e:
        logger.error(f"Error checking bytes: {e}")
        await ctx.respond("An error occurred while checking bytes. Please try again later.", flags=hikari.MessageFlag.EPHEMERAL)


@bytes_group.child
@lightbulb.option("user", "User to check roles for", type=hikari.User, required=False)
@lightbulb.command("awards", "Check if a user has earned any roles based on their bytes cache")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def bytes_check_roles(ctx: context.SlashContext) -> None:
    """
    Check if a user has earned any roles based on their bytes balance.
    """
    # Get API client
    client = ctx.bot.d.api_client

    # Get user to check
    target_user = ctx.options.user or ctx.author

    # Get guild ID
    guild_id = ctx.guild_id
    if not guild_id:
        await ctx.respond("This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        # Get user from API
        logger.info(f"Looking up user with discord_id: {target_user.id}")
        user_response = await client._request("GET", f"/api/users?discord_id={target_user.id}")
        user_data = await client._get_json(user_response)
        logger.info(f"API response for user lookup: {user_data}")

        if not user_data.get("users"):
            await ctx.respond(f"User {target_user.username} was not found in the database.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        user_id = user_data["users"][0]["id"]
        logger.info(f"Found user with ID: {user_id}")

        # Check for earned roles
        # Pass the current channel ID so the award message is sent in the same channel
        await check_for_earned_roles(client, ctx.bot, target_user.id, guild_id, ctx.channel_id)

        # Respond with success message
        await ctx.respond(f"Checked roles for {target_user.mention}. Any earned roles have been assigned.", flags=hikari.MessageFlag.EPHEMERAL)

    except Exception as e:
        logger.error(f"Error checking roles: {e}")
        await ctx.respond("An error occurred while checking roles. Please try again later.", flags=hikari.MessageFlag.EPHEMERAL)


@bytes_group.child
@lightbulb.option("limit", "Number of users to show", type=int, min_value=1, max_value=25, default=10, required=False)
@lightbulb.command("heap", "Show the user's with the largest bytes cache")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def bytes_leaderboard(ctx: context.SlashContext) -> None:
    """
    Show the bytes leaderboard.
    """
    # Get API client
    client = ctx.bot.d.api_client

    # Get guild ID
    guild_id = ctx.guild_id
    if not guild_id:
        await ctx.respond("This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Get limit
    limit = ctx.options.limit or 10

    try:
        # Get leaderboard
        leaderboard_data = await get_leaderboard(client, guild_id, limit)

        if not leaderboard_data.get("leaderboard"):
            await ctx.respond("No bytes have been given in this server yet!", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Create embed
        embed = hikari.Embed(
            title="Bytes Heap",
            description=f"Top {len(leaderboard_data['leaderboard'])} users by bytes cache",
            color=hikari.Color.from_rgb(87, 242, 135)  # Green color
        )

        # Add leaderboard entries
        leaderboard_text = ""
        for i, entry in enumerate(leaderboard_data["leaderboard"]):
            medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i+1}."
            user_id = entry["user_id"]
            username = entry["username"]
            balance = entry["bytes_balance"]

            # Format the balance
            balance_formatted = format_bytes(balance)

            leaderboard_text += f"{medal} **{username}**: {balance_formatted}\n"

        embed.description = leaderboard_text

        # Add server icon
        guild = ctx.get_guild()
        if guild and guild.icon_url:
            embed.set_thumbnail(guild.icon_url)

        await ctx.respond(embed=embed)

    except Exception as e:
        logger.error(f"Error getting leaderboard: {e}")
        await ctx.respond(
            "An error occurred while getting the bytes heap. Please try again later.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )


def load(bot: lightbulb.BotApp) -> None:
    """Load the bytes plugin."""
    bot.add_plugin(bytes_plugin)


async def get_cached_user(client: APIClient, user_id: int) -> Optional[Dict[str, Any]]:
    """
    Get a user from cache or API.

    Args:
        client: API client
        user_id: Discord user ID

    Returns:
        User data or None if not found
    """
    now = datetime.now().timestamp()

    # Check cache first
    if user_id in user_cache:
        user, timestamp = user_cache[user_id]
        if now - timestamp < CACHE_TIMEOUT:
            logger.info(f"Using cached user data for user {user_id}")
            return user

    # Get from API
    try:
        logger.info(f"Looking up user with discord_id: {user_id}")
        user_response = await client._request("GET", f"/api/users?discord_id={user_id}")
        user_data = await client._get_json(user_response)

        if not user_data.get("users"):
            logger.error(f"User {user_id} not found in database")
            return None

        # Get the user's data
        user = user_data["users"][0]

        # Update cache
        user_cache[user_id] = (user, now)

        return user
    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}")
        return None


async def get_cached_guild_member(client: APIClient, user_id: int, guild_id: int) -> Optional[GuildMember]:
    """
    Get a guild member from cache or API.

    Args:
        client: API client
        user_id: Discord user ID
        guild_id: Guild ID

    Returns:
        GuildMember or None if not found
    """
    now = datetime.now().timestamp()
    cache_key = (user_id, guild_id)

    # Check cache first
    if cache_key in guild_member_cache:
        member, timestamp = guild_member_cache[cache_key]
        if now - timestamp < CACHE_TIMEOUT:
            logger.info(f"Using cached guild member data for user {user_id} in guild {guild_id}")
            return member

    # Get from API
    try:
        logger.info(f"Getting guild member for user {user_id} in guild {guild_id}")
        guild_member = await client.get_guild_member(user_id, guild_id)

        if guild_member:
            # Update cache
            guild_member_cache[cache_key] = (guild_member, now)

        return guild_member
    except Exception as e:
        logger.error(f"Error getting guild member for user {user_id} in guild {guild_id}: {e}")
        return None


async def check_daily_bytes_eligibility(client: APIClient, user_id: int, guild_id: int) -> Tuple[bool, Optional[datetime]]:
    """
    Check if a user is eligible for daily bytes in a guild, with caching.

    This function checks if a user is eligible to receive daily bytes and returns
    a tuple with (is_eligible, next_eligible_time). If the user is eligible now,
    next_eligible_time will be None.

    Args:
        client: API client
        user_id: Discord user ID
        guild_id: Guild ID

    Returns:
        Tuple of (is_eligible, next_eligible_time)
    """
    now = datetime.now(UTC)
    now_ts = now.timestamp()
    cache_key = (user_id, guild_id)

    # Check cache first to avoid API calls
    if cache_key in daily_bytes_eligibility_cache:
        next_eligible_ts, cache_ts = daily_bytes_eligibility_cache[cache_key]

        # If next eligible time is in the future, user is not eligible yet
        # We use a much longer timeout for negative results (24 hours) since we know exactly when they'll be eligible
        if next_eligible_ts > now_ts:
            next_eligible_time = datetime.fromtimestamp(next_eligible_ts, UTC)
            logger.info(f"User {user_id} in guild {guild_id} is not eligible for daily bytes yet (cached). Next eligible: {next_eligible_time}")
            return False, next_eligible_time
        elif now_ts - cache_ts < CACHE_TIMEOUT:
            # Cache says they're eligible and the cache is still valid
            logger.info(f"User {user_id} in guild {guild_id} is eligible for daily bytes (cached)")
            return True, None
        else:
            # Cache says they're eligible but it's expired, we need to verify with the API
            logger.info(f"User {user_id} in guild {guild_id} might be eligible for daily bytes (cache expired)")
            # Let the function continue to check with the API

    # Cache miss or expired, check with the API
    try:
        # Get guild member to check last_daily_bytes
        guild_member = await get_cached_guild_member(client, user_id, guild_id)

        if not guild_member:
            # If no guild member record, user is eligible (new user)
            logger.info(f"No guild member record for user {user_id} in guild {guild_id}, assuming eligible for daily bytes")
            return True, None

        # Check last_daily_bytes
        last_daily_bytes = getattr(guild_member, "last_daily_bytes", None)

        # For debugging
        logger.debug(f"last_daily_bytes from guild_member: {last_daily_bytes}")

        if last_daily_bytes is None:
            # If no last_daily_bytes, user is eligible
            logger.info(f"No last_daily_bytes for user {user_id} in guild {guild_id}, eligible for daily bytes")
            return True, None

        # Parse last_daily_bytes if it's a string
        if isinstance(last_daily_bytes, str):
            last_daily_bytes = datetime.fromisoformat(last_daily_bytes)

        # Ensure both datetimes have timezone info
        if last_daily_bytes.tzinfo is None:
            last_daily_bytes = last_daily_bytes.replace(tzinfo=UTC)

        # Check if it's been at least 24 hours
        time_since_last = (now - last_daily_bytes).total_seconds()

        # For debugging (using logger.debug so it doesn't clutter logs in production)
        logger.debug(f"time_since_last: {time_since_last} seconds, {time_since_last/3600:.2f} hours")
        logger.debug(f"now: {now}, last_daily_bytes: {last_daily_bytes}")
        logger.debug(f"24 hours in seconds: {24 * 60 * 60}")

        # Check if it's been at least 24 hours
        if time_since_last >= 24 * 60 * 60:  # 24 hours or more
            # User is eligible - it's been more than 24 hours
            logger.info(f"User {user_id} in guild {guild_id} is eligible for daily bytes (last received {time_since_last/3600:.2f} hours ago)")
            return True, None
        else:
            # Calculate next eligible time
            next_eligible_time = last_daily_bytes + timedelta(hours=24)
            next_eligible_ts = next_eligible_time.timestamp()

            # Update cache
            daily_bytes_eligibility_cache[cache_key] = (next_eligible_ts, now_ts)

            logger.info(f"User {user_id} in guild {guild_id} is not eligible for daily bytes yet. Next eligible: {next_eligible_time}")
            return False, next_eligible_time
    except Exception as e:
        logger.error(f"Error checking daily bytes eligibility for user {user_id} in guild {guild_id}: {e}")
        # Default to eligible in case of error to avoid blocking users
        return True, None


async def update_user_streak(client: APIClient, user_id: int, guild_id: int) -> Dict[str, Any]:
    """
    Update a user's messaging streak in a specific guild.

    Args:
        client: API client
        user_id: Discord user ID
        guild_id: Guild ID

    Returns:
        Dictionary with updated guild member information
    """
    try:
        # Get the current UTC day in format YYYY-MM-DD
        now = datetime.now(UTC)
        current_day = now.strftime("%Y-%m-%d")

        # Get user from cache or API to ensure they exist
        user = await get_cached_user(client, user_id)
        if not user:
            logger.error(f"User {user_id} not found in database")
            return None

        user_id_internal = user["id"]

        # Get guild member from cache or API
        guild_member = await get_cached_guild_member(client, user_id, guild_id)

        # If guild member doesn't exist, create it
        if not guild_member:
            logger.info(f"Guild member not found for user {user_id} in guild {guild_id}, creating new record")
            # Create a new guild member record
            guild_member_data = {
                "user_id": user_id_internal,
                "guild_id": guild_id,
                "is_active": True
            }
            guild_member_response = await client._request(
                "POST",
                f"/api/users/{user_id}/guilds",
                data=guild_member_data
            )
            guild_member = await client._get_json(guild_member_response)

        # Get the guild member's last active day and streak count
        last_active_day = guild_member.get("last_active_day") if hasattr(guild_member, "get") else getattr(guild_member, "last_active_day", None)
        streak_count = guild_member.get("streak_count", 0) if hasattr(guild_member, "get") else getattr(guild_member, "streak_count", 0)

        logger.info(f"Guild member {user_id} in guild {guild_id} last_active_day: {last_active_day}, current_day: {current_day}")

        # Calculate previous day
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        logger.info(f"Yesterday was: {yesterday}")

        # Update streak count
        if last_active_day == yesterday:
            # User was active yesterday in this guild, increment streak
            streak_count += 1
            logger.info(f"User {user_id} was active in guild {guild_id} yesterday, incrementing streak to {streak_count}")
        elif last_active_day != current_day:
            # User was not active yesterday and not already active today in this guild, reset streak
            streak_count = 1
            logger.info(f"User {user_id} was not active in guild {guild_id} recently, resetting streak to 1")
        else:
            logger.info(f"User {user_id} was already active in guild {guild_id} today, keeping streak at {streak_count}")

        # Update guild member's last active day and streak count
        guild_member_update = {
            "last_active_day": current_day,
            "streak_count": streak_count
        }

        # Update guild member in API
        update_response = await client._request(
            "PUT",
            f"/api/users/{user_id}/guilds/{guild_id}",
            data=guild_member_update
        )
        updated_guild_member = await client._get_json(update_response)

        # Update the cache with the new guild member data
        cache_key = (user_id, guild_id)
        guild_member_cache[cache_key] = (updated_guild_member, datetime.now().timestamp())

        # Determine if this is a new day for the user in this guild
        # If last_active_day is the current day, then it's not a new day
        # This prevents users from getting daily bytes multiple times on the same day
        is_new_day = last_active_day != current_day

        # Log the determination
        logger.info(f"User {user_id} in guild {guild_id} is_new_day determined as: {is_new_day} (last_active_day: {last_active_day}, current_day: {current_day})")

        # CRITICAL CHECK: Has the user already received daily bytes today in this guild?
        # This is essential to prevent duplicate awards
        last_daily_bytes = updated_guild_member.get("last_daily_bytes") if hasattr(updated_guild_member, "get") else getattr(updated_guild_member, "last_daily_bytes", None)

        if is_new_day and last_daily_bytes:
            try:
                if isinstance(last_daily_bytes, str):
                    last_daily_bytes = datetime.fromisoformat(last_daily_bytes)

                last_daily_bytes_day = last_daily_bytes.strftime("%Y-%m-%d")

                # If last_daily_bytes is from today, user has already received daily bytes today in this guild
                # Override is_new_day to False
                if last_daily_bytes_day == current_day:
                    logger.info(f"User {user_id} already received daily bytes in guild {guild_id} today, overriding is_new_day to False")
                    is_new_day = False

                # Double-check: If it's been less than 24 hours, also set is_new_day to False
                time_since_last = (now - last_daily_bytes).total_seconds()
                if time_since_last < 24 * 60 * 60:  # Less than 24 hours
                    logger.info(f"User {user_id} received daily bytes in guild {guild_id} less than 24 hours ago, overriding is_new_day to False")
                    is_new_day = False
            except (ValueError, TypeError, AttributeError) as e:
                logger.error(f"Error checking last_daily_bytes for user {user_id} in guild {guild_id}: {e}")

        # Log the final determination
        if is_new_day:
            logger.info(f"User {user_id} is active for the first time today in guild {guild_id} and has not received daily bytes today, setting is_new_day to True")
        else:
            logger.info(f"User {user_id} was already active today in guild {guild_id} or has already received daily bytes today, setting is_new_day to False")

        return {
            "guild_member": updated_guild_member,
            "user": user,
            "streak_count": streak_count,
            "is_new_day": is_new_day
        }
    except Exception as e:
        logger.error(f"Error updating user streak: {e}")
        return None



@bytes_plugin.listener(hikari.GuildMessageCreateEvent)
async def on_message(event: hikari.GuildMessageCreateEvent) -> None:
    """
    Listen for messages and update user streaks and award daily bytes.

    Rules:
    1. Every consecutive day (UTC) a user sends a message increases their streak by 1
    2. Missing a day resets the streak to 1
    3. Users receive daily bytes for their first message in a 24-hour period
    4. Streak multipliers are applied based on streak milestones (8, 16, 32, 64)
    """
    # Ignore bot messages
    if event.is_bot:
        return

    # Get API client
    client = event.app.d.api_client

    # Get user and guild IDs
    user_id = event.author_id
    guild_id = event.guild_id

    try:
        # STEP 1: Check if user is eligible for daily bytes using our cached eligibility function
        is_eligible, next_eligible_time = await check_daily_bytes_eligibility(client, user_id, guild_id)

        if not is_eligible:
            # User is not eligible yet, log when they will be eligible
            if next_eligible_time:
                time_until_eligible = (next_eligible_time - datetime.now(UTC)).total_seconds() / 3600
                logger.info(f"on_message: User {user_id} is not eligible for daily bytes in guild {guild_id} yet. "
                           f"Next eligible in {time_until_eligible:.2f} hours")

            # Skip updating streak - we know they're not eligible for daily bytes
            # This is the key optimization to avoid unnecessary API calls
            return

        # STEP 2: If we get here, the user might be eligible, so update their streak
        streak_info = await update_user_streak(client, user_id, guild_id)
        if not streak_info:
            logger.error(f"on_message: Failed to update streak for user {user_id}")
            return

        # STEP 3: Only award daily bytes for the first message of the day
        if not streak_info["is_new_day"]:
            logger.info(f"on_message: User {user_id} has already been active today, not eligible for daily bytes")
            return

        # STEP 4: User is eligible for daily bytes - award them
        logger.info(f"on_message: User {user_id} is eligible for daily bytes with streak {streak_info['streak_count']}")

        # Get bytes config
        config = await get_bytes_config(client, guild_id)
        daily_amount = config.daily_earning

        # Calculate multiplier based on streak
        streak_count = streak_info["streak_count"]
        multiplier = 1
        multiplier_text = ""

        if streak_count % 64 == 0:  # long (64-bit)
            multiplier = 256
            multiplier_text = f"🔥 **LONG STREAK (x{multiplier})** 🔥"
        elif streak_count % 32 == 0:  # int (32-bit)
            multiplier = 16
            multiplier_text = f"🔥 **INT STREAK (x{multiplier})** 🔥"
        elif streak_count % 16 == 0:  # short (16-bit)
            multiplier = 4
            multiplier_text = f"🔥 **SHORT STREAK (x{multiplier})** 🔥"
        elif streak_count % 8 == 0:  # char (8-bit)
            multiplier = 2
            multiplier_text = f"🔥 **CHAR STREAK (x{multiplier})** 🔥"

        # Calculate final amount
        amount = daily_amount * multiplier

        # Create bytes transaction from system user to user
        # Use cached system user ID if available
        if system_user_id is None:
            # First get or create system user (discord_id=0)
            system_response = await client._request("GET", "/api/users?discord_id=0")
            system_data = await client._get_json(system_response)

            if not system_data.get("users"):
                # Create system user
                system_user = {
                    "discord_id": 0,
                    "username": "System"
                }
                system_create = await client._request("POST", "/api/users", data=system_user)
                system_data = await client._get_json(system_create)
                system_user_id = system_data["id"]
            else:
                system_user_id = system_data["users"][0]["id"]

            # Cache the system user ID globally
            globals()["system_user_id"] = system_user_id

        # Create bytes transaction
        bytes_obj = Bytes(
            giver_id=0,  # System user
            receiver_id=user_id,
            guild_id=int(guild_id),
            amount=amount,
            reason=f"Daily bytes for {streak_count} day streak"
        )

        # Send transaction to API
        response = await client._request("POST", "/api/bytes", data=client._dict_from_model(bytes_obj))
        result = await client._get_json(response)

        # CRITICAL: Update the guild member's last_daily_bytes timestamp
        # This is essential to prevent duplicate awards
        now_iso = now.isoformat()

        # Update the guild member's last_daily_bytes field
        guild_member_update = {
            "last_daily_bytes": now_iso
        }

        update_response = await client._request(
            "PUT",
            f"/api/users/{user_id}/guilds/{guild_id}",
            data=guild_member_update
        )

        # Verify the update was successful
        if hasattr(update_response, 'status_code') and update_response.status_code >= 400:
            logger.error(f"on_message: Failed to update guild member {user_id} in guild {guild_id} last_daily_bytes: {update_response.status_code}")
            return

        # Update the cache with the new guild member data
        updated_guild_member = await client._get_json(update_response)
        cache_key = (user_id, guild_id)
        guild_member_cache[cache_key] = (updated_guild_member, datetime.now().timestamp())

        # Update the daily bytes eligibility cache with the next eligible time (24 hours from now)
        # This is the critical part that prevents unnecessary API calls for the next 24 hours
        next_eligible_time = now + timedelta(hours=24)
        next_eligible_ts = next_eligible_time.timestamp()
        daily_bytes_eligibility_cache[cache_key] = (next_eligible_ts, now.timestamp())

        logger.info(f"on_message: Successfully updated guild member {user_id} in guild {guild_id} last_daily_bytes to {now_iso}. "
                   f"Next eligible at {next_eligible_time}")

        # Send award message
        try:
            # Get the user from Discord API
            discord_user = await event.app.rest.fetch_user(user_id)
            if not discord_user:
                logger.error(f"on_message: Could not fetch Discord user {user_id}")
                return

            # Get the guild from Discord API
            guild = await event.app.rest.fetch_guild(guild_id)
            if not guild:
                logger.error(f"on_message: Could not fetch guild {guild_id}")
                return

            # Format the bytes amount in a human-readable way
            amount_formatted = format_bytes(amount)
            streak_text = f"{streak_count} day{'s' if streak_count != 1 else ''}"

            # Create embed for daily bytes award
            daily_embed = hikari.Embed(
                title="🎁 Daily Bytes Awarded!",
                color=hikari.Color.from_rgb(87, 242, 135)  # Green color
            )

            if multiplier > 1:
                daily_embed.description = f"{discord_user.mention} received {amount_formatted} for maintaining a **{streak_text}** streak!\n\n{multiplier_text}"
            else:
                daily_embed.description = f"{discord_user.mention} received {amount_formatted} for maintaining a **{streak_text}** streak!"

            # Add streak info
            daily_embed.add_field(
                name="Current Streak",
                value=f"{streak_count} day{'s' if streak_count != 1 else ''}",
                inline=True
            )

            # Add next milestone info
            next_milestone = 0
            if streak_count < 8:
                next_milestone = 8
                milestone_type = "CHAR"
                milestone_multiplier = 2
            elif streak_count < 16:
                next_milestone = 16
                milestone_type = "SHORT"
                milestone_multiplier = 4
            elif streak_count < 32:
                next_milestone = 32
                milestone_type = "INT"
                milestone_multiplier = 16
            elif streak_count < 64:
                next_milestone = 64
                milestone_type = "LONG"
                milestone_multiplier = 256
            else:
                next_milestone = ((streak_count // 64) + 1) * 64
                milestone_type = "LONG"
                milestone_multiplier = 256

            days_to_milestone = next_milestone - streak_count
            daily_embed.add_field(
                name="Next Milestone",
                value=f"{days_to_milestone} day{'s' if days_to_milestone != 1 else ''} to {milestone_type} (x{milestone_multiplier})",
                inline=True
            )

            # Add footer
            daily_embed.set_footer(text="Keep your streak going by sending a message every day!")

            # Determine which channel to send the message to
            target_channel_id = event.channel_id or guild.system_channel_id
            if not target_channel_id:
                logger.error(f"on_message: No target channel found for guild {guild_id}")
                return

            # Send the message
            await event.app.rest.create_message(target_channel_id, embed=daily_embed)
            logger.info(f"on_message: Sent daily bytes award message to channel {target_channel_id}")

            # Check for earned roles and add them to the user
            await check_for_earned_roles(client, event.app, user_id, guild_id)
        except Exception as e:
            logger.error(f"on_message: Error sending award message: {e}")
    except Exception as e:
        logger.error(f"Error in on_message handler: {e}")
        # If there's an error, don't check daily bytes to avoid potential duplicate awards
        return


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the bytes plugin."""
    bot.remove_plugin(bytes_plugin)
