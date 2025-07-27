"""Bytes economy commands for the Discord bot.

This module implements all bytes-related slash commands using the service layer
for business logic. Commands include balance checking, transfers, leaderboards,
and transaction history.
"""

from __future__ import annotations

import hikari
import lightbulb
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

# Import only the Discord embed function needed for large history lists
from smarter_dev.bot.utils.embeds import create_transaction_history_embed
from smarter_dev.bot.utils.image_embeds import get_generator
from smarter_dev.bot.services.exceptions import (
    AlreadyClaimedError,
    InsufficientBalanceError,
    ServiceError,
    ValidationError
)

if TYPE_CHECKING:
    from smarter_dev.bot.services.bytes_service import BytesService

logger = logging.getLogger(__name__)

# Create plugin
plugin = lightbulb.Plugin("bytes")


@plugin.command
@lightbulb.command("bytes", "Bytes economy commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def bytes_group(ctx: lightbulb.Context) -> None:
    """Base bytes command group."""
    pass


@bytes_group.child
@lightbulb.command("balance", "Check your current bytes balance")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def balance_command(ctx: lightbulb.Context) -> None:
    """Handle balance command - shows current balance without auto-claiming."""
    
    service: BytesService = getattr(ctx.bot, 'd', {}).get('bytes_service')
    if not service:
        # Fallback to _services dict
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('bytes_service')
    
    if not service:
        generator = get_generator()
        image_file = generator.create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    try:
        # Get current balance
        balance = await service.get_balance(str(ctx.guild_id), str(ctx.user.id))
        
        # Create image embed for balance
        generator = get_generator()
        description = f"{balance.balance:,} bytes"
        if balance.streak_count > 0:
            description += f"\nStreak: {balance.streak_count} days"
        if balance.last_daily:
            # Format last daily as readable date
            description += f"\nLast Daily: {balance.last_daily.strftime('%B %d, %Y')}"
        
        image_file = generator.create_simple_embed("YOUR BYTES BALANCE", description, "info")
        await ctx.respond(attachment=image_file)
        return
            
    except ServiceError as e:
        logger.error(f"Service error in balance command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("Failed to retrieve balance. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    except Exception as e:
        logger.exception(f"Unexpected error in balance command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return



@bytes_group.child
@lightbulb.option("reason", "Reason for sending bytes", required=False)
@lightbulb.option("amount", "Amount to send", type=int)
@lightbulb.option("user", "User to send bytes to", type=hikari.User)
@lightbulb.command("send", "Send bytes to another user")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def send_command(ctx: lightbulb.Context) -> None:
    """Handle send command - transfer bytes between users."""
    service: BytesService = getattr(ctx.bot, 'd', {}).get('bytes_service')
    if not service:
        # Fallback to _services dict
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('bytes_service')
    
    if not service:
        generator = get_generator()
        image_file = generator.create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    user = ctx.options.user
    amount = ctx.options.amount
    reason = ctx.options.reason
    
    # Validate amount
    if amount < 1 or amount > 10000:
        generator = get_generator()
        image_file = generator.create_error_embed("Amount must be between 1 and 10,000 bytes.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    # Validate receiver is in guild
    try:
        member = ctx.get_guild().get_member(user.id)
        if not member:
            generator = get_generator()
            image_file = generator.create_error_embed("That user is not in this server!")
            await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Prevent self-transfer (additional validation)
        if user.id == ctx.user.id:
            generator = get_generator()
            image_file = generator.create_error_embed("You can't send bytes to yourself!")
            await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Process transfer using service
        result = await service.transfer_bytes(
            str(ctx.guild_id),
            ctx.user,  # giver (UserProtocol)
            user,  # receiver (UserProtocol) 
            amount,
            reason
        )
        
        if not result.success:
            logger.info(f"Transfer failed: {result.reason}, is_cooldown_error: {result.is_cooldown_error}")
            generator = get_generator()
            # Use special cooldown embed for cooldown errors
            if result.is_cooldown_error:
                logger.info("Creating cooldown image embed")
                image_file = generator.create_cooldown_embed(result.reason, result.cooldown_end_timestamp)
            else:
                # Use error embed for transfer limit and other errors
                logger.info("Creating error image embed")
                image_file = generator.create_error_embed(result.reason)
            logger.info(f"Created image file: {type(image_file)}")
            await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Success image embed
        generator = get_generator()
        
        # Get user's display name (nickname if set, otherwise username)
        try:
            member = ctx.get_guild().get_member(user.id)
            display_name = member.display_name if member else user.username
        except:
            display_name = user.username
        
        description = f"Successfully sent {amount:,} bytes to {display_name}"
        
        # Add additional details
        if reason:
            description += f"\nReason: {reason}"
        
        if result.new_giver_balance is not None:
            description += f"\nYour New Balance: {result.new_giver_balance:,} bytes"
        
        image_file = generator.create_success_embed("BYTES SENT", description)
        await ctx.respond(attachment=image_file)
        
    except InsufficientBalanceError as e:
        generator = get_generator()
        image_file = generator.create_error_embed(f"Insufficient balance! You need {e.required:,} bytes but only have {e.available:,}.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    except ValidationError as e:
        generator = get_generator()
        image_file = generator.create_error_embed(f"Invalid input: {e.message}")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    except ServiceError as e:
        logger.error(f"Service error in send command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("Transfer failed. Please try again later.")
        try:
            await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        except hikari.BadRequestError as discord_err:
            if "already been acknowledged" in str(discord_err):
                logger.warning("Interaction already acknowledged, skipping response")
            else:
                logger.error(f"Discord API error: {discord_err}")
        return
    except hikari.BadRequestError as e:
        if "already been acknowledged" in str(e):
            logger.warning("Interaction already acknowledged during transfer, skipping error response")
        else:
            logger.error(f"Discord interaction error in send command: {e}")
        return
    except Exception as e:
        logger.exception(f"Unexpected error in send command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
        try:
            await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        except hikari.BadRequestError as discord_err:
            if "already been acknowledged" in str(discord_err):
                logger.warning("Interaction already acknowledged, skipping error response")
            else:
                logger.error(f"Discord API error during error handling: {discord_err}")
        return


@bytes_group.child
@lightbulb.option("limit", "Number of users to show (1-25)", type=int, required=False)
@lightbulb.command("leaderboard", "View the guild bytes leaderboard")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def leaderboard_command(ctx: lightbulb.Context) -> None:
    """Handle leaderboard command - show top users by balance."""
    service: BytesService = getattr(ctx.bot, 'd', {}).get('bytes_service')
    if not service:
        # Fallback to _services dict
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('bytes_service')
    
    if not service:
        generator = get_generator()
        image_file = generator.create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    limit = ctx.options.limit or 10
    if limit < 1 or limit > 25:
        limit = 10
    
    try:
        entries = await service.get_leaderboard(str(ctx.guild_id), limit)
        
        # Create user display names mapping
        user_display_names = {}
        for entry in entries:
            try:
                member = ctx.get_guild().get_member(int(entry.user_id))
                user_display_names[entry.user_id] = member.display_name if member else f"User {entry.user_id[:8]}"
            except:
                user_display_names[entry.user_id] = f"User {entry.user_id[:8]}"
        
        generator = get_generator()
        image_file = generator.create_leaderboard_embed(entries, ctx.get_guild().name, user_display_names)
        await ctx.respond(attachment=image_file)
        
    except ServiceError as e:
        logger.error(f"Service error in leaderboard command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("Failed to get leaderboard. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.exception(f"Unexpected error in leaderboard command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)


@bytes_group.child
@lightbulb.option("limit", "Number of transactions to show (1-20)", type=int, required=False)
@lightbulb.command("history", "View your recent bytes transactions")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def history_command(ctx: lightbulb.Context) -> None:
    """Handle history command - show user's transaction history."""
    service: BytesService = getattr(ctx.bot, 'd', {}).get('bytes_service')
    if not service:
        # Fallback to _services dict
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('bytes_service')
    
    if not service:
        generator = get_generator()
        image_file = generator.create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    limit = ctx.options.limit or 10
    if limit < 1 or limit > 20:
        limit = 10
    
    try:
        transactions = await service.get_transaction_history(
            str(ctx.guild_id),
            user_id=str(ctx.user.id),
            limit=limit
        )
        
        # Use image embed for 10 or fewer transactions, Discord embed for more
        if limit <= 10:
            generator = get_generator()
            image_file = generator.create_history_embed(transactions, str(ctx.user.id))
            await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        else:
            # Use standard Discord embed for larger lists
            embed = create_transaction_history_embed(transactions, str(ctx.user.id))
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        
    except ServiceError as e:
        logger.error(f"Service error in history command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("Failed to get transaction history. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.exception(f"Unexpected error in history command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)


@bytes_group.child
@lightbulb.command("config", "View the current bytes economy configuration for this server")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def config_command(ctx: lightbulb.Context) -> None:
    """Handle config command - show guild bytes configuration."""
    service: BytesService = getattr(ctx.bot, 'd', {}).get('bytes_service')
    if not service:
        # Fallback to _services dict
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('bytes_service')
    
    if not service:
        generator = get_generator()
        image_file = generator.create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    try:
        config = await service.get_config(str(ctx.guild_id))
        
        generator = get_generator()
        image_file = generator.create_config_embed(config, ctx.get_guild().name)
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        
    except ServiceError as e:
        logger.error(f"Service error in config command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("Failed to get configuration. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.exception(f"Unexpected error in config command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)


def load(bot: lightbulb.BotApp) -> None:
    """Load the bytes plugin."""
    bot.add_plugin(plugin)
    logger.info("Bytes plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the bytes plugin."""
    bot.remove_plugin(plugin)
    logger.info("Bytes plugin unloaded")