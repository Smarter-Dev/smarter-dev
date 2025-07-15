"""Bytes economy commands for the Discord bot.

This module implements all bytes-related slash commands using the service layer
for business logic. Commands include balance checking, transfers, leaderboards,
and transaction history.
"""

from __future__ import annotations

import hikari
import lightbulb
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from smarter_dev.bot.utils.embeds import (
    create_balance_embed, 
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

# Create bytes command group
bytes_group = lightbulb.Group("bytes", "Bytes economy commands")


@bytes_group.register
class BalanceCommand(
    lightbulb.SlashCommand,
    name="balance",
    description="Check your bytes balance and claim daily reward"
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        """Handle balance command - shows balance and attempts daily claim."""
        service: BytesService = ctx.app.d.bytes_service
        
        if not service:
            embed = create_error_embed("Bot services are not initialized. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        try:
            # Get current balance
            balance = await service.get_balance(str(ctx.guild_id), str(ctx.user.id))
            
            # Try to claim daily reward
            try:
                daily_result = await service.claim_daily(
                    str(ctx.guild_id),
                    str(ctx.user.id), 
                    str(ctx.user)
                )
                
                if daily_result.success:
                    embed = create_balance_embed(
                        balance=daily_result.balance,
                        daily_earned=daily_result.earned,
                        streak=daily_result.streak,
                        multiplier=daily_result.multiplier
                    )
                    embed.title = "💰 Daily Bytes Claimed!"
                    embed.color = hikari.Color(0x22c55e)
                else:
                    # Daily already claimed, just show balance
                    embed = create_balance_embed(balance)
                    embed.title = "💰 Your Bytes Balance"
                    
            except AlreadyClaimedError:
                # Daily already claimed, just show balance
                embed = create_balance_embed(balance)
                embed.title = "💰 Your Bytes Balance"
                embed.set_footer("💡 Daily reward already claimed today!")
                
        except ServiceError as e:
            logger.error(f"Service error in balance command: {e}")
            embed = create_error_embed("Failed to retrieve balance. Please try again later.")
        except Exception as e:
            logger.exception(f"Unexpected error in balance command: {e}")
            embed = create_error_embed("An unexpected error occurred. Please try again later.")
            
        await ctx.respond(embed=embed)


@bytes_group.register  
class SendCommand(
    lightbulb.SlashCommand,
    name="send", 
    description="Send bytes to another user"
):
    user = lightbulb.user("user", "User to send bytes to")
    amount = lightbulb.integer("amount", "Amount to send", min_value=1, max_value=10000)
    reason = lightbulb.string("reason", "Reason for sending bytes", required=False, max_length=200)
    
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        """Handle send command - transfer bytes between users."""
        service: BytesService = ctx.app.d.bytes_service
        
        if not service:
            embed = create_error_embed("Bot services are not initialized. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Validate receiver is in guild
        try:
            member = ctx.get_guild().get_member(self.user.id)
            if not member:
                embed = create_error_embed("That user is not in this server!")
                await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
                return
            
            # Prevent self-transfer (additional validation)
            if self.user.id == ctx.user.id:
                embed = create_error_embed("You can't send bytes to yourself!")
                await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
                return
            
            # Process transfer using service
            result = await service.transfer_bytes(
                str(ctx.guild_id),
                ctx.user,  # giver (UserProtocol)
                self.user,  # receiver (UserProtocol) 
                self.amount,
                self.reason
            )
            
            if not result.success:
                embed = create_error_embed(result.reason)
                await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
                return
            
            # Success embed
            embed = create_success_embed(
                title="✅ Bytes Sent!",
                description=f"Successfully sent **{self.amount:,}** bytes to {self.user.mention}"
            )
            
            if self.reason:
                embed.add_field("Reason", self.reason, inline=False)
                
            embed.add_field("Transaction ID", str(result.transaction.id), inline=True)
            if result.new_giver_balance is not None:
                embed.add_field("Your New Balance", f"{result.new_giver_balance:,} bytes", inline=True)
            
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
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        except Exception as e:
            logger.exception(f"Unexpected error in send command: {e}")
            embed = create_error_embed("An unexpected error occurred. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
            
        await ctx.respond(embed=embed)


@bytes_group.register
class LeaderboardCommand(
    lightbulb.SlashCommand,
    name="leaderboard",
    description="View the guild bytes leaderboard"
):
    limit = lightbulb.integer("limit", "Number of users to show", min_value=1, max_value=25, default=10)
    
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        """Handle leaderboard command - show top users by balance."""
        service: BytesService = ctx.app.d.bytes_service
        
        if not service:
            embed = create_error_embed("Bot services are not initialized. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        try:
            entries = await service.get_leaderboard(str(ctx.guild_id), self.limit)
            
            if not entries:
                embed = create_error_embed("No leaderboard data available yet!")
                await ctx.respond(embed=embed)
                return
            
            # Enhance entries with Discord user data
            enhanced_entries = []
            for entry in entries:
                try:
                    member = ctx.get_guild().get_member(int(entry.user_id))
                    entry.user_display_name = member.display_name if member else f"User {entry.user_id[:8]}"
                except:
                    entry.user_display_name = f"User {entry.user_id[:8]}"
                enhanced_entries.append(entry)
            
            embed = create_leaderboard_embed(enhanced_entries, ctx.get_guild().name)
            
        except ServiceError as e:
            logger.error(f"Service error in leaderboard command: {e}")
            embed = create_error_embed("Failed to get leaderboard. Please try again later.")
        except Exception as e:
            logger.exception(f"Unexpected error in leaderboard command: {e}")
            embed = create_error_embed("An unexpected error occurred. Please try again later.")
            
        await ctx.respond(embed=embed)


@bytes_group.register
class HistoryCommand(
    lightbulb.SlashCommand,
    name="history",
    description="View your recent bytes transactions"
):
    limit = lightbulb.integer("limit", "Number of transactions to show", min_value=1, max_value=20, default=10)
    
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        """Handle history command - show user's transaction history."""
        service: BytesService = ctx.app.d.bytes_service
        
        if not service:
            embed = create_error_embed("Bot services are not initialized. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        try:
            transactions = await service.get_transaction_history(
                str(ctx.guild_id),
                user_id=str(ctx.user.id),
                limit=self.limit
            )
            
            embed = create_transaction_history_embed(transactions, str(ctx.user.id))
            
        except ServiceError as e:
            logger.error(f"Service error in history command: {e}")
            embed = create_error_embed("Failed to get transaction history. Please try again later.")
        except Exception as e:
            logger.exception(f"Unexpected error in history command: {e}")
            embed = create_error_embed("An unexpected error occurred. Please try again later.")
            
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@bytes_group.register
class ConfigCommand(
    lightbulb.SlashCommand,
    name="config",
    description="View the current bytes economy configuration for this server"
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        """Handle config command - show guild bytes configuration."""
        service: BytesService = ctx.app.d.bytes_service
        
        if not service:
            embed = create_error_embed("Bot services are not initialized. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        try:
            config = await service.get_config(str(ctx.guild_id))
            
            embed = hikari.Embed(
                title="⚙️ Bytes Economy Configuration",
                color=hikari.Color(0x3b82f6),
                timestamp=datetime.now()
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
    # Get client from bot (v3 syntax)
    client = lightbulb.client_from_app(bot)
    client.register(bytes_group)
    logger.info("Bytes plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the bytes plugin."""
    client = lightbulb.client_from_app(bot)
    client.unregister(bytes_group)
    logger.info("Bytes plugin unloaded")