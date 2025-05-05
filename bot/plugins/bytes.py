"""
Bytes plugin for the Smarter Dev Discord bot.

This plugin provides commands for the Bytes system, which allows users to give
each other bytes as a form of recognition and earn roles based on their bytes balance.
"""

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

import hikari
import lightbulb
from lightbulb import commands, context

from bot.api_client import APIClient
from bot.api_models import Bytes, BytesConfig, BytesRole, BytesCooldown, DiscordUser

# Create plugin
bytes_plugin = lightbulb.Plugin("Bytes")
logger = logging.getLogger("bot.plugins.bytes")

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

# Cache for bytes configs to avoid frequent API calls
# Format: {guild_id: (config, timestamp)}
config_cache: Dict[int, tuple] = {}
# Cache timeout in seconds
CACHE_TIMEOUT = 300  # 5 minutes


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
    if guild_id in config_cache:
        config, timestamp = config_cache[guild_id]
        if now - timestamp < CACHE_TIMEOUT:
            return config

    # Get from API
    try:
        config = await client.get_bytes_config(guild_id)
        config_cache[guild_id] = (config, now)
        return config
    except Exception as e:
        logger.error(f"Error getting bytes config for guild {guild_id}: {e}")
        # Return default config
        return BytesConfig(guild_id=guild_id)


async def get_user_bytes_info(client: APIClient, user_id: int, guild_id: int) -> Dict[str, Any]:
    """
    Get a user's bytes information.

    Args:
        client: API client
        user_id: User ID
        guild_id: Guild ID

    Returns:
        Dictionary with bytes information
    """
    try:
        logger.info(f"Making API request to /api/bytes/balance/{user_id}?guild_id={guild_id}")
        response = await client._request("GET", f"/api/bytes/balance/{user_id}?guild_id={guild_id}")
        result = await client._get_json(response)
        logger.info(f"API response for bytes balance: {result}")
        return result
    except Exception as e:
        logger.error(f"Error getting bytes info for user {user_id} in guild {guild_id}: {e}")
        return {
            "user_id": user_id,
            "bytes_balance": 0,
            "bytes_received": 0,
            "bytes_given": 0,
            "earned_roles": []
        }


async def check_cooldown(client: APIClient, user_id: int, guild_id: int) -> Dict[str, Any]:
    """
    Check if a user is on cooldown for giving bytes.

    Args:
        client: API client
        user_id: User ID
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
            giver_id=giver["id"],
            receiver_id=receiver_user["id"],
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

        # If user earned new roles, send a separate message
        if earned_roles:
            role_mentions = []
            for role_data in earned_roles:
                role_id = role_data["role_id"]
                role = ctx.get_guild().get_role(role_id)
                if role:
                    role_mentions.append(role.mention)
                    # Add role to user
                    try:
                        await ctx.get_guild().add_role_to_member(receiver.id, role_id, reason="Bytes role reward")
                    except Exception as e:
                        logger.error(f"Error adding role {role_id} to user {receiver.id}: {e}")

            if role_mentions:
                roles_str = ", ".join(role_mentions)
                congrats_embed = hikari.Embed(
                    title="🎉 New Role Earned!",
                    description=f"{receiver.mention} has earned new role{'s' if len(role_mentions) > 1 else ''}: {roles_str}",
                    color=hikari.Color.from_rgb(255, 215, 0)  # Gold color
                )
                await ctx.get_channel().send(embed=congrats_embed)

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
        embed.add_field(name="Received", value=received_formatted, inline=False)
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

                # Find next role to earn
                next_role = None
                for role in roles:
                    if role["bytes_required"] > bytes_info["bytes_balance"]:
                        next_role = role
                        break

                if next_role:
                    role_id = next_role["role_id"]
                    role = ctx.get_guild().get_role(role_id)
                    if role:
                        bytes_needed = next_role["bytes_required"] - bytes_info["bytes_balance"]
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


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the bytes plugin."""
    bot.remove_plugin(bytes_plugin)
