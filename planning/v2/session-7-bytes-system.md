# Session 7: Bytes System Implementation

## Objective
Implement the complete bytes economy system including daily rewards with streaks, transfers between users, role rewards, and leaderboards. Focus on engaging user experience and abuse prevention.

## Prerequisites
- Completed Session 6 (bot core exists)
- Understanding of the bytes economy design
- API client configured

## Task 1: Bytes Service Layer

### bot/services/bytes_service.py

Create the bytes business logic service:

```python
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, date, timedelta
import structlog

from bot.config import BotConfig
from bot.errors import InsufficientBytesError, CooldownError, ConfigurationError
from shared.types import StreakMultiplier
from shared.constants import DEFAULT_STARTING_BALANCE, DEFAULT_DAILY_AMOUNT
from shared.utils import utctoday

logger = structlog.get_logger()

class BytesService:
    """Service for bytes economy operations."""
    
    def __init__(self, api_client):
        self.api = api_client
        self._config_cache: Dict[str, Dict[str, Any]] = {}
        self._config_cache_time: Dict[str, datetime] = {}
    
    async def get_config(self, guild_id: str) -> Dict[str, Any]:
        """Get guild bytes configuration with caching."""
        cache_key = f"config:{guild_id}"
        now = datetime.utcnow()
        
        # Check cache
        if cache_key in self._config_cache:
            cache_time = self._config_cache_time.get(cache_key)
            if cache_time and (now - cache_time).total_seconds() < 300:  # 5 min cache
                return self._config_cache[cache_key]
        
        try:
            # Fetch from API
            config = await self.api.get_bytes_config(guild_id)
            
            # Cache it
            self._config_cache[cache_key] = config
            self._config_cache_time[cache_key] = now
            
            return config
            
        except Exception as e:
            logger.error(
                "Failed to fetch bytes config",
                guild_id=guild_id,
                error=str(e)
            )
            
            # Return defaults
            return {
                "starting_balance": DEFAULT_STARTING_BALANCE,
                "daily_amount": DEFAULT_DAILY_AMOUNT,
                "max_transfer": 1000,
                "cooldown_hours": 24,
                "role_rewards": {}
            }
    
    async def check_balance(
        self,
        guild_id: str,
        user_id: str,
        username: str
    ) -> Dict[str, Any]:
        """Check user balance and award daily if eligible."""
        try:
            # Get current balance
            balance_data = await self.api.get_bytes_balance(guild_id, user_id)
            
            # Check if daily is available
            last_daily = balance_data.get("last_daily")
            today = utctoday()
            daily_available = False
            
            if last_daily:
                last_daily_date = datetime.fromisoformat(last_daily).date()
                daily_available = last_daily_date < today
            else:
                daily_available = True
            
            return {
                **balance_data,
                "daily_available": daily_available
            }
            
        except Exception as e:
            logger.error(
                "Failed to check balance",
                guild_id=guild_id,
                user_id=user_id,
                error=str(e)
            )
            
            # Return new user data
            config = await self.get_config(guild_id)
            return {
                "balance": config["starting_balance"],
                "total_received": 0,
                "total_sent": 0,
                "streak_count": 0,
                "daily_available": True
            }
    
    async def award_daily(
        self,
        guild_id: str,
        user_id: str,
        username: str
    ) -> Tuple[int, int, StreakMultiplier]:
        """Award daily bytes and return amount, new streak, and multiplier."""
        try:
            # Award daily via API
            result = await self.api.award_daily_bytes(guild_id, user_id, username)
            
            # Parse result
            amount_awarded = result["amount_awarded"]
            new_streak = result["streak_count"]
            multiplier = StreakMultiplier.from_streak(new_streak)
            
            logger.info(
                "Daily bytes awarded",
                guild_id=guild_id,
                user_id=user_id,
                amount=amount_awarded,
                streak=new_streak
            )
            
            return amount_awarded, new_streak, multiplier
            
        except Exception as e:
            logger.error(
                "Failed to award daily bytes",
                guild_id=guild_id,
                user_id=user_id,
                error=str(e)
            )
            raise
    
    async def transfer(
        self,
        guild_id: str,
        giver_id: str,
        giver_username: str,
        receiver_id: str,
        receiver_username: str,
        amount: int,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Transfer bytes between users."""
        # Validation
        if giver_id == receiver_id:
            raise ValueError("Cannot transfer bytes to yourself!")
        
        if amount <= 0:
            raise ValueError("Amount must be positive!")
        
        # Get config
        config = await self.get_config(guild_id)
        max_transfer = config.get("max_transfer", 1000)
        
        if amount > max_transfer:
            raise ValueError(f"Maximum transfer amount is **{max_transfer:,}** bytes!")
        
        # Check balance
        giver_balance = await self.check_balance(guild_id, giver_id, giver_username)
        
        if giver_balance["balance"] < amount:
            raise InsufficientBytesError(
                current=giver_balance["balance"],
                required=amount
            )
        
        try:
            # Execute transfer
            result = await self.api.transfer_bytes(
                guild_id=guild_id,
                giver_id=giver_id,
                giver_username=giver_username,
                receiver_id=receiver_id,
                receiver_username=receiver_username,
                amount=amount,
                reason=reason
            )
            
            logger.info(
                "Bytes transferred",
                guild_id=guild_id,
                giver_id=giver_id,
                receiver_id=receiver_id,
                amount=amount
            )
            
            return result
            
        except Exception as e:
            logger.error(
                "Failed to transfer bytes",
                guild_id=guild_id,
                error=str(e)
            )
            raise
    
    async def get_leaderboard(
        self,
        guild_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get guild leaderboard."""
        try:
            return await self.api.get_bytes_leaderboard(guild_id, limit)
        except Exception as e:
            logger.error(
                "Failed to get leaderboard",
                guild_id=guild_id,
                error=str(e)
            )
            return []
    
    async def check_role_rewards(
        self,
        guild_id: str,
        user_id: str,
        total_received: int
    ) -> List[str]:
        """Check which role rewards user has earned."""
        config = await self.get_config(guild_id)
        role_rewards = config.get("role_rewards", {})
        
        earned_roles = []
        
        for role_id, threshold in role_rewards.items():
            if total_received >= threshold:
                earned_roles.append(role_id)
        
        return earned_roles
    
    def calculate_daily_amount(
        self,
        base_amount: int,
        streak: int
    ) -> Tuple[int, StreakMultiplier]:
        """Calculate daily amount with multiplier."""
        multiplier = StreakMultiplier.from_streak(streak)
        total_amount = base_amount * multiplier.multiplier
        
        return total_amount, multiplier
```

## Task 2: Bytes Plugin

### bot/plugins/bytes.py

Create the bytes commands plugin:

```python
import hikari
import lightbulb
from typing import Optional
import structlog

from bot.plugins.base import BasePlugin
from bot.services.bytes_service import BytesService
from bot.utils.embeds import EmbedBuilder
from bot.utils.checks import guild_only, cooldown
from bot.errors import InsufficientBytesError, APIError
from bot.views.bytes_views import TransferConfirmView
from shared.utils import utctoday

logger = structlog.get_logger()

class BytesPlugin(BasePlugin):
    """Plugin for bytes economy commands."""
    
    def __init__(self):
        super().__init__("bytes", "Bytes economy system")
        self.service: Optional[BytesService] = None
    
    def load(self, bot: lightbulb.BotApp) -> None:
        """Load the plugin."""
        super().load(bot)
        self.service = BytesService(bot.api)
        
        # Listen for message events to award daily bytes
        bot.subscribe(hikari.GuildMessageCreateEvent, self.on_message)
    
    async def on_message(self, event: hikari.GuildMessageCreateEvent) -> None:
        """Award daily bytes on first message of the day."""
        # Ignore bots
        if event.is_bot:
            return
        
        # Check if daily is available (cached check)
        cache_key = self.cache_key(str(event.guild_id), str(event.author_id), "daily_check")
        last_check = await self.get_cached(cache_key, ttl=60)  # 1 minute cache
        
        if last_check is not None:
            return  # Recently checked
        
        self.set_cache(cache_key, True)
        
        try:
            # Check balance
            balance_data = await self.service.check_balance(
                str(event.guild_id),
                str(event.author_id),
                event.author.username
            )
            
            if balance_data.get("daily_available"):
                # Award daily bytes
                config = await self.service.get_config(str(event.guild_id))
                base_amount = config.get("daily_amount", 10)
                
                amount, streak, multiplier = await self.service.award_daily(
                    str(event.guild_id),
                    str(event.author_id),
                    event.author.username
                )
                
                # Notify user (silently)
                logger.info(
                    "Daily bytes awarded automatically",
                    guild_id=event.guild_id,
                    user_id=event.author_id,
                    amount=amount,
                    streak=streak
                )
                
        except Exception as e:
            logger.error(
                "Failed to check/award daily bytes",
                guild_id=event.guild_id,
                user_id=event.author_id,
                error=str(e)
            )

bytes_plugin = BytesPlugin()

@bytes_plugin.command
@lightbulb.app_command_permissions(dm_enabled=False)
@lightbulb.command("bytes", "Bytes economy commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def bytes_group(ctx: lightbulb.SlashContext) -> None:
    """Bytes command group."""
    pass

@bytes_group.child
@lightbulb.command("balance", "Check your bytes balance", aliases=["bal"])
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def balance_command(ctx: lightbulb.SlashContext) -> None:
    """Check bytes balance."""
    await ctx.respond(hikari.ResponseType.DEFERRED)
    
    try:
        # Get balance
        balance_data = await bytes_plugin.service.check_balance(
            str(ctx.guild_id),
            str(ctx.author.id),
            ctx.author.username
        )
        
        # Create embed
        embed = EmbedBuilder.bytes_balance(
            user=ctx.author,
            balance=balance_data["balance"],
            total_received=balance_data["total_received"],
            total_sent=balance_data["total_sent"],
            streak=balance_data.get("streak_count", 0),
            daily_available=balance_data.get("daily_available", False)
        )
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error("Balance check failed", error=str(e))
        await bytes_plugin.send_error(
            ctx,
            "Failed to check balance. Please try again later."
        )

@bytes_group.child
@lightbulb.option(
    "reason",
    "Reason for the transfer",
    type=str,
    required=False,
    max_length=100
)
@lightbulb.option(
    "amount",
    "Amount of bytes to send",
    type=int,
    required=True,
    min_value=1
)
@lightbulb.option(
    "user",
    "User to send bytes to",
    type=hikari.User,
    required=True
)
@lightbulb.command("send", "Send bytes to another user", aliases=["give", "transfer"])
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
@cooldown(seconds=60, bucket=lightbulb.buckets.UserBucket)
async def send_command(ctx: lightbulb.SlashContext) -> None:
    """Send bytes to another user."""
    receiver = ctx.options.user
    amount = ctx.options.amount
    reason = ctx.options.reason
    
    # Check if receiver is a bot
    if receiver.is_bot:
        await bytes_plugin.send_error(ctx, "You cannot send bytes to bots!")
        return
    
    # For large amounts, require confirmation
    if amount >= 100:
        view = TransferConfirmView(
            giver=ctx.author,
            receiver=receiver,
            amount=amount,
            reason=reason,
            timeout=60
        )
        
        embed = EmbedBuilder.info(
            "Confirm Transfer",
            f"Send **{amount:,}** bytes to {receiver.mention}?",
            fields=[
                ("Reason", reason or "No reason provided", False)
            ]
        )
        
        resp = await ctx.respond(embed=embed, components=view)
        await view.start(await resp.message())
        await view.wait()
        
        if not view.confirmed:
            return
    else:
        await ctx.respond(hikari.ResponseType.DEFERRED)
    
    try:
        # Execute transfer
        result = await bytes_plugin.service.transfer(
            guild_id=str(ctx.guild_id),
            giver_id=str(ctx.author.id),
            giver_username=ctx.author.username,
            receiver_id=str(receiver.id),
            receiver_username=receiver.username,
            amount=amount,
            reason=reason
        )
        
        # Success embed
        embed = EmbedBuilder.success(
            "Transfer Complete",
            f"Successfully sent **{amount:,}** bytes to {receiver.mention}",
            fields=[
                ("New Balance", f"{result['giver_new_balance']:,} bytes", True),
                ("Total Sent", f"{result['giver_total_sent']:,} bytes", True)
            ]
        )
        
        if reason:
            embed.add_field("Reason", reason, inline=False)
        
        await ctx.respond(embed=embed)
        
    except InsufficientBytesError as e:
        await bytes_plugin.send_error(ctx, e.user_message)
    except ValueError as e:
        await bytes_plugin.send_error(ctx, str(e))
    except Exception as e:
        logger.error("Transfer failed", error=str(e))
        await bytes_plugin.send_error(
            ctx,
            "Failed to transfer bytes. Please try again later."
        )

@bytes_group.child
@lightbulb.option(
    "limit",
    "Number of users to show",
    type=int,
    required=False,
    default=10,
    min_value=5,
    max_value=25
)
@lightbulb.command("leaderboard", "View the bytes leaderboard", aliases=["top", "lb"])
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def leaderboard_command(ctx: lightbulb.SlashContext) -> None:
    """View bytes leaderboard."""
    await ctx.respond(hikari.ResponseType.DEFERRED)
    
    limit = ctx.options.limit
    
    try:
        # Get leaderboard
        leaderboard = await bytes_plugin.service.get_leaderboard(
            str(ctx.guild_id),
            limit
        )
        
        if not leaderboard:
            await bytes_plugin.send_error(
                ctx,
                "No users with bytes found in this server!"
            )
            return
        
        # Format entries
        entries = []
        for i, entry in enumerate(leaderboard, 1):
            # Try to get user from cache
            user = ctx.bot.cache.get_user(int(entry["user_id"]))
            username = user.username if user else f"User {entry['user_id']}"
            
            entries.append((username, entry["balance"], i))
        
        # Create embed
        embed = EmbedBuilder.leaderboard(
            f"Top {len(entries)} Richest Users",
            entries,
            footer=f"Requested by {ctx.author.username}"
        )
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error("Leaderboard failed", error=str(e))
        await bytes_plugin.send_error(
            ctx,
            "Failed to fetch leaderboard. Please try again later."
        )

@bytes_group.child
@lightbulb.option(
    "user",
    "User to check (defaults to yourself)",
    type=hikari.User,
    required=False
)
@lightbulb.command("check", "Check someone's bytes balance")
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def check_command(ctx: lightbulb.SlashContext) -> None:
    """Check another user's balance."""
    user = ctx.options.user or ctx.author
    
    # Don't check bots
    if user.is_bot:
        await bytes_plugin.send_error(ctx, "Bots don't have bytes!")
        return
    
    await ctx.respond(hikari.ResponseType.DEFERRED)
    
    try:
        # Get balance
        balance_data = await bytes_plugin.service.check_balance(
            str(ctx.guild_id),
            str(user.id),
            user.username
        )
        
        # Create embed
        embed = EmbedBuilder.bytes_balance(
            user=user,
            balance=balance_data["balance"],
            total_received=balance_data["total_received"],
            total_sent=balance_data["total_sent"],
            streak=balance_data.get("streak_count", 0),
            daily_available=False  # Don't show for other users
        )
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error("Check balance failed", error=str(e))
        await bytes_plugin.send_error(
            ctx,
            "Failed to check balance. Please try again later."
        )

def load(bot: lightbulb.BotApp) -> None:
    """Load the plugin."""
    bot.add_plugin(bytes_plugin)

def unload(bot: lightbulb.BotApp) -> None:
    """Unload the plugin."""
    bot.remove_plugin(bytes_plugin)
```

## Task 3: Transfer Confirmation View

### bot/views/bytes_views.py

Create interactive views for bytes:

```python
import hikari
import asyncio
from typing import Optional
import structlog

logger = structlog.get_logger()

class TransferConfirmView(hikari.api.InteractionResponseBuilder):
    """Confirmation view for bytes transfers."""
    
    def __init__(
        self,
        giver: hikari.User,
        receiver: hikari.User,
        amount: int,
        reason: Optional[str],
        *,
        timeout: float = 60.0
    ):
        self.giver = giver
        self.receiver = receiver
        self.amount = amount
        self.reason = reason
        self.timeout = timeout
        self.confirmed = False
        self._message: Optional[hikari.Message] = None
        self._task: Optional[asyncio.Task] = None
    
    def build(self) -> hikari.api.MessageActionRowBuilder:
        """Build the action row."""
        row = hikari.api.MessageActionRowBuilder()
        
        row.add_interactive_button(
            hikari.ButtonStyle.SUCCESS,
            "confirm_transfer",
            label="Confirm",
            emoji="✅"
        )
        
        row.add_interactive_button(
            hikari.ButtonStyle.DANGER,
            "cancel_transfer",
            label="Cancel",
            emoji="❌"
        )
        
        return row
    
    async def start(self, message: hikari.Message) -> None:
        """Start listening for interactions."""
        self._message = message
        self._task = asyncio.create_task(self._wait_for_interaction())
    
    async def wait(self) -> None:
        """Wait for the view to finish."""
        if self._task:
            await self._task
    
    async def _wait_for_interaction(self) -> None:
        """Wait for button interactions."""
        try:
            with message.app.stream(
                hikari.InteractionCreateEvent,
                timeout=self.timeout
            ).filter(
                lambda e: (
                    isinstance(e.interaction, hikari.ComponentInteraction) and
                    e.interaction.message.id == self._message.id and
                    e.interaction.user.id == self.giver.id
                )
            ) as stream:
                async for event in stream:
                    interaction = event.interaction
                    
                    if interaction.custom_id == "confirm_transfer":
                        self.confirmed = True
                        await self._handle_confirm(interaction)
                        return
                    elif interaction.custom_id == "cancel_transfer":
                        self.confirmed = False
                        await self._handle_cancel(interaction)
                        return
                        
        except asyncio.TimeoutError:
            # Timeout - disable buttons
            await self._disable_buttons()
    
    async def _handle_confirm(self, interaction: hikari.ComponentInteraction) -> None:
        """Handle confirmation."""
        # Update message to show pending
        embed = hikari.Embed(
            title="⏳ Processing Transfer",
            description=f"Sending **{self.amount:,}** bytes to {self.receiver.mention}...",
            color=0xF59E0B
        )
        
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            embed=embed,
            components=[]
        )
    
    async def _handle_cancel(self, interaction: hikari.ComponentInteraction) -> None:
        """Handle cancellation."""
        embed = hikari.Embed(
            title="❌ Transfer Cancelled",
            description="The bytes transfer was cancelled.",
            color=0xEF4444
        )
        
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            embed=embed,
            components=[]
        )
    
    async def _disable_buttons(self) -> None:
        """Disable buttons after timeout."""
        if self._message:
            embed = hikari.Embed(
                title="⏰ Transfer Expired",
                description="The transfer confirmation timed out.",
                color=0x6B7280
            )
            
            try:
                await self._message.edit(
                    embed=embed,
                    components=[]
                )
            except:
                pass  # Message might be deleted
```

## Task 4: API Endpoints

### web/api/routers/bytes.py

Create API endpoints for bytes system:

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from datetime import date
from sqlalchemy import select, update, and_, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from web.api.dependencies import CurrentAPIKey, DatabaseSession
from web.api.schemas import (
    BytesBalanceResponse,
    BytesTransferRequest,
    BytesTransferResponse,
    BytesDailyRequest,
    BytesDailyResponse,
    BytesLeaderboardResponse
)
from web.models.bytes import BytesBalance, BytesTransaction, BytesConfig
from web.crud.bytes import bytes_crud
from shared.types import StreakMultiplier
from shared.utils import utctoday, utcnow
import structlog

logger = structlog.get_logger()
router = APIRouter()

@router.get("/guilds/{guild_id}/bytes/balance/{user_id}", response_model=BytesBalanceResponse)
async def get_user_balance(
    guild_id: str,
    user_id: str,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> BytesBalanceResponse:
    """Get user's bytes balance."""
    balance = await bytes_crud.get(
        db,
        guild_id=guild_id,
        user_id=user_id
    )
    
    if not balance:
        # Return default balance
        config = await db.execute(
            select(BytesConfig).where(BytesConfig.guild_id == guild_id)
        )
        config_data = config.scalar_one_or_none()
        
        starting_balance = config_data.starting_balance if config_data else 100
        
        return BytesBalanceResponse(
            balance=starting_balance,
            total_received=0,
            total_sent=0,
            last_daily=None,
            streak_count=0
        )
    
    return BytesBalanceResponse(
        balance=balance.balance,
        total_received=balance.total_received,
        total_sent=balance.total_sent,
        last_daily=balance.last_daily.isoformat() if balance.last_daily else None,
        streak_count=balance.streak_count
    )

@router.post("/guilds/{guild_id}/bytes/daily", response_model=BytesDailyResponse)
async def award_daily_bytes(
    guild_id: str,
    request: BytesDailyRequest,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> BytesDailyResponse:
    """Award daily bytes to user."""
    today = utctoday()
    
    # Get config
    config = await db.execute(
        select(BytesConfig).where(BytesConfig.guild_id == guild_id)
    )
    config_data = config.scalar_one_or_none()
    
    if not config_data:
        raise HTTPException(404, "Guild configuration not found")
    
    # Get or create balance
    balance = await bytes_crud.get_or_create_balance(
        db,
        guild_id=guild_id,
        user_id=request.user_id,
        starting_balance=config_data.starting_balance
    )
    
    # Check if already claimed today
    if balance.last_daily and balance.last_daily >= today:
        raise HTTPException(400, "Daily bytes already claimed today")
    
    # Calculate streak
    old_streak = balance.streak_count
    if balance.last_daily:
        days_diff = (today - balance.last_daily).days
        if days_diff == 1:
            balance.streak_count += 1
        elif days_diff > 1:
            balance.streak_count = 1
    else:
        balance.streak_count = 1
    
    # Calculate amount with multiplier
    multiplier = StreakMultiplier.from_streak(balance.streak_count)
    amount = config_data.daily_amount * multiplier.multiplier
    
    # Update balance
    balance.balance += amount
    balance.total_received += amount
    balance.last_daily = today
    
    await db.commit()
    
    # Log transaction
    transaction = BytesTransaction(
        guild_id=guild_id,
        giver_id="system",
        giver_username="Daily Reward",
        receiver_id=request.user_id,
        receiver_username=request.username,
        amount=amount,
        reason=f"Daily reward ({multiplier.display})"
    )
    db.add(transaction)
    await db.commit()
    
    logger.info(
        "Daily bytes awarded",
        guild_id=guild_id,
        user_id=request.user_id,
        amount=amount,
        streak=balance.streak_count
    )
    
    return BytesDailyResponse(
        amount_awarded=amount,
        new_balance=balance.balance,
        streak_count=balance.streak_count,
        multiplier=multiplier.multiplier
    )

@router.post("/guilds/{guild_id}/bytes/transfer", response_model=BytesTransferResponse)
async def transfer_bytes(
    guild_id: str,
    request: BytesTransferRequest,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> BytesTransferResponse:
    """Transfer bytes between users."""
    # Get giver balance
    giver = await bytes_crud.get(
        db,
        guild_id=guild_id,
        user_id=request.giver_id
    )
    
    if not giver or giver.balance < request.amount:
        raise HTTPException(400, "Insufficient balance")
    
    # Get or create receiver
    receiver = await bytes_crud.get_or_create_balance(
        db,
        guild_id=guild_id,
        user_id=request.receiver_id,
        starting_balance=0  # Don't give starting balance on receive
    )
    
    # Update balances
    giver.balance -= request.amount
    giver.total_sent += request.amount
    
    receiver.balance += request.amount
    receiver.total_received += request.amount
    
    # Log transaction
    transaction = BytesTransaction(
        guild_id=guild_id,
        giver_id=request.giver_id,
        giver_username=request.giver_username,
        receiver_id=request.receiver_id,
        receiver_username=request.receiver_username,
        amount=request.amount,
        reason=request.reason
    )
    db.add(transaction)
    
    await db.commit()
    
    logger.info(
        "Bytes transferred",
        guild_id=guild_id,
        giver_id=request.giver_id,
        receiver_id=request.receiver_id,
        amount=request.amount
    )
    
    return BytesTransferResponse(
        transaction_id=str(transaction.id),
        giver_new_balance=giver.balance,
        receiver_new_balance=receiver.balance,
        giver_total_sent=giver.total_sent,
        receiver_total_received=receiver.total_received
    )

@router.get("/guilds/{guild_id}/bytes/leaderboard", response_model=BytesLeaderboardResponse)
async def get_leaderboard(
    guild_id: str,
    limit: int = Query(10, ge=1, le=100),
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> BytesLeaderboardResponse:
    """Get bytes leaderboard for guild."""
    result = await db.execute(
        select(BytesBalance)
        .where(BytesBalance.guild_id == guild_id)
        .order_by(desc(BytesBalance.balance))
        .limit(limit)
    )
    
    entries = result.scalars().all()
    
    leaderboard = [
        {
            "user_id": entry.user_id,
            "balance": entry.balance,
            "total_received": entry.total_received,
            "total_sent": entry.total_sent
        }
        for entry in entries
    ]
    
    return BytesLeaderboardResponse(leaderboard=leaderboard)

@router.get("/guilds/{guild_id}/config/bytes")
async def get_bytes_config(
    guild_id: str,
    api_key: CurrentAPIKey,
    db: DatabaseSession
):
    """Get guild bytes configuration."""
    config = await db.execute(
        select(BytesConfig).where(BytesConfig.guild_id == guild_id)
    )
    config_data = config.scalar_one_or_none()
    
    if not config_data:
        # Return defaults
        return {
            "starting_balance": 100,
            "daily_amount": 10,
            "max_transfer": 1000,
            "cooldown_hours": 24,
            "role_rewards": {}
        }
    
    return {
        "starting_balance": config_data.starting_balance,
        "daily_amount": config_data.daily_amount,
        "max_transfer": config_data.max_transfer,
        "cooldown_hours": config_data.cooldown_hours,
        "role_rewards": config_data.role_rewards
    }
```

## Task 5: API Schemas

### web/api/schemas.py (bytes section)

Add bytes-related schemas:

```python
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import date
from uuid import UUID

# Bytes schemas
class BytesBalanceResponse(BaseModel):
    balance: int
    total_received: int
    total_sent: int
    last_daily: Optional[str]
    streak_count: int

class BytesTransferRequest(BaseModel):
    giver_id: str
    giver_username: str = Field(..., max_length=100)
    receiver_id: str
    receiver_username: str = Field(..., max_length=100)
    amount: int = Field(..., gt=0)
    reason: Optional[str] = Field(None, max_length=500)

class BytesTransferResponse(BaseModel):
    transaction_id: str
    giver_new_balance: int
    receiver_new_balance: int
    giver_total_sent: int
    receiver_total_received: int

class BytesDailyRequest(BaseModel):
    user_id: str
    username: str = Field(..., max_length=100)

class BytesDailyResponse(BaseModel):
    amount_awarded: int
    new_balance: int
    streak_count: int
    multiplier: int

class BytesLeaderboardEntry(BaseModel):
    user_id: str
    balance: int
    total_received: int
    total_sent: int

class BytesLeaderboardResponse(BaseModel):
    leaderboard: List[BytesLeaderboardEntry]
```

## Task 6: Create Tests

### tests/test_bytes_system.py

Test the bytes system:

```python
import pytest
from datetime import date, timedelta
from unittest.mock import Mock, AsyncMock, patch

from bot.services.bytes_service import BytesService
from bot.errors import InsufficientBytesError
from shared.types import StreakMultiplier

@pytest.fixture
def mock_api():
    """Create mock API client."""
    return AsyncMock()

@pytest.fixture
def bytes_service(mock_api):
    """Create bytes service with mock API."""
    return BytesService(mock_api)

@pytest.mark.asyncio
async def test_check_balance_new_user(bytes_service, mock_api):
    """Test checking balance for new user."""
    mock_api.get_bytes_balance.side_effect = Exception("User not found")
    mock_api.get_bytes_config.return_value = {
        "starting_balance": 150,
        "daily_amount": 10,
        "max_transfer": 1000,
        "cooldown_hours": 24,
        "role_rewards": {}
    }
    
    balance = await bytes_service.check_balance("guild1", "user1", "TestUser")
    
    assert balance["balance"] == 150
    assert balance["total_received"] == 0
    assert balance["daily_available"] is True

@pytest.mark.asyncio
async def test_calculate_daily_amount():
    """Test daily amount calculation with multipliers."""
    service = BytesService(None)
    
    # No streak
    amount, multiplier = service.calculate_daily_amount(10, 0)
    assert amount == 10
    assert multiplier == StreakMultiplier.NONE
    
    # CHAR streak (8 days)
    amount, multiplier = service.calculate_daily_amount(10, 8)
    assert amount == 20
    assert multiplier == StreakMultiplier.CHAR
    
    # LONG streak (64+ days)
    amount, multiplier = service.calculate_daily_amount(10, 100)
    assert amount == 2560
    assert multiplier == StreakMultiplier.LONG

@pytest.mark.asyncio
async def test_transfer_validation(bytes_service, mock_api):
    """Test transfer validation."""
    # Self transfer
    with pytest.raises(ValueError, match="Cannot transfer bytes to yourself"):
        await bytes_service.transfer(
            "guild1", "user1", "User1", "user1", "User1", 100
        )
    
    # Negative amount
    with pytest.raises(ValueError, match="Amount must be positive"):
        await bytes_service.transfer(
            "guild1", "user1", "User1", "user2", "User2", -10
        )
    
    # Exceeds max
    mock_api.get_bytes_config.return_value = {"max_transfer": 100}
    with pytest.raises(ValueError, match="Maximum transfer amount"):
        await bytes_service.transfer(
            "guild1", "user1", "User1", "user2", "User2", 200
        )

@pytest.mark.asyncio
async def test_transfer_insufficient_balance(bytes_service, mock_api):
    """Test transfer with insufficient balance."""
    mock_api.get_bytes_config.return_value = {"max_transfer": 1000}
    mock_api.get_bytes_balance.return_value = {
        "balance": 50,
        "total_received": 100,
        "total_sent": 50
    }
    
    with pytest.raises(InsufficientBytesError) as exc_info:
        await bytes_service.transfer(
            "guild1", "user1", "User1", "user2", "User2", 100
        )
    
    assert exc_info.value.current == 50
    assert exc_info.value.required == 100

@pytest.mark.asyncio
async def test_check_role_rewards(bytes_service):
    """Test role reward checking."""
    mock_api.get_bytes_config.return_value = {
        "role_rewards": {
            "role1": 100,
            "role2": 500,
            "role3": 1000
        }
    }
    
    # User with 600 bytes received
    earned_roles = await bytes_service.check_role_rewards("guild1", "user1", 600)
    
    assert "role1" in earned_roles
    assert "role2" in earned_roles
    assert "role3" not in earned_roles
```

### tests/test_bytes_api.py

Test the API endpoints:

```python
import pytest
from httpx import AsyncClient
from datetime import date

@pytest.mark.asyncio
async def test_get_balance(auth_api_client: AsyncClient, test_db):
    """Test getting user balance."""
    response = await auth_api_client.get(
        "/api/v1/guilds/123/bytes/balance/456"
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "balance" in data
    assert "total_received" in data
    assert "streak_count" in data

@pytest.mark.asyncio
async def test_award_daily(auth_api_client: AsyncClient, test_db):
    """Test awarding daily bytes."""
    response = await auth_api_client.post(
        "/api/v1/guilds/123/bytes/daily",
        json={
            "user_id": "456",
            "username": "TestUser"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["amount_awarded"] > 0
    assert data["streak_count"] == 1
    assert data["multiplier"] == 1

@pytest.mark.asyncio
async def test_daily_already_claimed(auth_api_client: AsyncClient, test_db):
    """Test claiming daily twice."""
    # First claim
    await auth_api_client.post(
        "/api/v1/guilds/123/bytes/daily",
        json={"user_id": "456", "username": "TestUser"}
    )
    
    # Second claim
    response = await auth_api_client.post(
        "/api/v1/guilds/123/bytes/daily",
        json={"user_id": "456", "username": "TestUser"}
    )
    
    assert response.status_code == 400
    assert "already claimed" in response.json()["detail"]

@pytest.mark.asyncio
async def test_transfer_bytes(auth_api_client: AsyncClient, test_db):
    """Test transferring bytes."""
    # Give giver some bytes first
    from web.crud.bytes import bytes_crud
    await bytes_crud.create(
        test_db,
        guild_id="123",
        user_id="111",
        balance=1000,
        total_received=1000
    )
    
    response = await auth_api_client.post(
        "/api/v1/guilds/123/bytes/transfer",
        json={
            "giver_id": "111",
            "giver_username": "Giver",
            "receiver_id": "222",
            "receiver_username": "Receiver",
            "amount": 100,
            "reason": "Test transfer"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["giver_new_balance"] == 900
    assert data["receiver_new_balance"] == 100
```

## Deliverables

1. **Bytes Service Layer**
   - Balance checking with daily eligibility
   - Daily award with streak calculation
   - Transfer validation and execution
   - Leaderboard queries
   - Role reward checking

2. **Discord Commands**
   - `/bytes balance` - Check balance
   - `/bytes send` - Transfer bytes
   - `/bytes leaderboard` - View top users
   - `/bytes check` - Check another user
   - Auto-award on daily message

3. **Interactive Views**
   - Transfer confirmation for large amounts
   - Button-based interactions
   - Timeout handling

4. **API Endpoints**
   - Balance retrieval
   - Daily award with streaks
   - Transfer execution
   - Leaderboard data
   - Configuration fetching

5. **Test Coverage**
   - Service logic tests
   - API endpoint tests
   - Validation tests
   - Streak calculation tests

## Important Notes

1. Daily bytes awarded automatically on first message
2. Streak multipliers make long-term engagement rewarding
3. Transfer confirmations prevent accidents
4. All operations are atomic (database transactions)
5. Comprehensive error handling with user-friendly messages
6. 5-minute cache on configurations for performance

This bytes system creates an engaging economy that rewards active participation while preventing abuse.