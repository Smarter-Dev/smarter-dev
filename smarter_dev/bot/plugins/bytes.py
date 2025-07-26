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

from smarter_dev.bot.utils.embeds import (
    create_balance_embed, 
    create_cooldown_embed,
    create_error_embed,
    create_success_embed,
    create_leaderboard_embed,
    create_transaction_history_embed
)
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
        embed = create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    try:
        # Get current balance
        balance = await service.get_balance(str(ctx.guild_id), str(ctx.user.id))
        
        # Show balance without attempting daily claim
        embed = create_balance_embed(balance)
        embed.title = "💰 Your Bytes Balance"
            
    except ServiceError as e:
        logger.error(f"Service error in balance command: {e}")
        embed = create_error_embed("Failed to retrieve balance. Please try again later.")
    except Exception as e:
        logger.exception(f"Unexpected error in balance command: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        
    await ctx.respond(embed=embed)



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
        embed = create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    user = ctx.options.user
    amount = ctx.options.amount
    reason = ctx.options.reason
    
    # Validate amount
    if amount < 1 or amount > 10000:
        embed = create_error_embed("Amount must be between 1 and 10,000 bytes.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    # Validate receiver is in guild
    try:
        member = ctx.get_guild().get_member(user.id)
        if not member:
            embed = create_error_embed("That user is not in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Prevent self-transfer (additional validation)
        if user.id == ctx.user.id:
            embed = create_error_embed("You can't send bytes to yourself!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
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
            # Use special cooldown embed for cooldown errors
            if result.is_cooldown_error:
                embed = create_cooldown_embed(result.reason, result.cooldown_end_timestamp)
            else:
                # Check for transfer limit error and provide specific title
                if "exceeds maximum limit" in result.reason.lower():
                    embed = hikari.Embed(
                        title="💰 Transfer Limit Exceeded",
                        description=result.reason,
                        color=hikari.Color(0xef4444),
                        timestamp=datetime.now(timezone.utc)
                    )
                else:
                    embed = create_error_embed(result.reason)
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Success embed
        embed = create_success_embed(
            title="✅ Bytes Sent!",
            description=f"Successfully sent **{amount:,}** bytes to {user.mention}"
        )
        
        if reason:
            embed.add_field("Reason", reason, inline=False)
            
        embed.add_field("Transaction ID", str(result.transaction.id), inline=True)
        if result.new_giver_balance is not None:
            embed.add_field("Your New Balance", f"{result.new_giver_balance:,} bytes", inline=True)
        
        await ctx.respond(embed=embed)
        
    except InsufficientBalanceError as e:
        embed = create_error_embed(f"Insufficient balance! You need {e.required:,} bytes but only have {e.available:,}.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    except ValidationError as e:
        embed = create_error_embed(f"Invalid input: {e.message}")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    except ServiceError as e:
        logger.error(f"Service error in send command: {e}")
        embed = create_error_embed("Transfer failed. Please try again later.")
        try:
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
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
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        try:
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
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
        embed = create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    limit = ctx.options.limit or 10
    if limit < 1 or limit > 25:
        limit = 10
    
    try:
        entries = await service.get_leaderboard(str(ctx.guild_id), limit)
        
        if not entries:
            embed = create_error_embed("No leaderboard data available yet!")
            await ctx.respond(embed=embed)
            return
        
        # Create user display names mapping
        user_display_names = {}
        for entry in entries:
            try:
                member = ctx.get_guild().get_member(int(entry.user_id))
                user_display_names[entry.user_id] = member.display_name if member else f"User {entry.user_id[:8]}"
            except:
                user_display_names[entry.user_id] = f"User {entry.user_id[:8]}"
        
        embed = create_leaderboard_embed(entries, ctx.get_guild().name, user_display_names)
        
    except ServiceError as e:
        logger.error(f"Service error in leaderboard command: {e}")
        embed = create_error_embed("Failed to get leaderboard. Please try again later.")
    except Exception as e:
        logger.exception(f"Unexpected error in leaderboard command: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        
    await ctx.respond(embed=embed)


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
        embed = create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
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
        
        embed = create_transaction_history_embed(transactions, str(ctx.user.id))
        
    except ServiceError as e:
        logger.error(f"Service error in history command: {e}")
        embed = create_error_embed("Failed to get transaction history. Please try again later.")
    except Exception as e:
        logger.exception(f"Unexpected error in history command: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        
    await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


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
        embed = create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    try:
        config = await service.get_config(str(ctx.guild_id))
        
        embed = hikari.Embed(
            title="⚙️ Bytes Economy Configuration",
            color=hikari.Color(0x3b82f6),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field("Daily Amount", f"{config.daily_amount:,} bytes", inline=True)
        embed.add_field("Starting Balance", f"{config.starting_balance:,} bytes", inline=True)
        embed.add_field("Max Transfer", f"{config.max_transfer:,} bytes", inline=True)
        
        # Streak bonuses
        if config.streak_bonuses:
            bonus_lines = []
            for days, multiplier in sorted(config.streak_bonuses.items()):
                bonus_lines.append(f"{days} days: **{multiplier}x**")
            embed.add_field("Streak Bonuses", "\n".join(bonus_lines), inline=True)
        
        embed.set_footer(f"Configuration for {ctx.get_guild().name}")
        
    except ServiceError as e:
        logger.error(f"Service error in config command: {e}")
        embed = create_error_embed("Failed to get configuration. Please try again later.")
    except Exception as e:
        logger.exception(f"Unexpected error in config command: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        
    await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


def load(bot: lightbulb.BotApp) -> None:
    """Load the bytes plugin."""
    bot.add_plugin(plugin)
    logger.info("Bytes plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the bytes plugin."""
    bot.remove_plugin(plugin)
    logger.info("Bytes plugin unloaded")