# Session 6: Discord Bot Core

## Objective
Create the Discord bot using Hikari and Lightbulb with a clean plugin architecture. Implement API client for web communication, Redis subscriber for real-time updates, and proper error handling.

## Prerequisites
- Completed Session 5 (web application running)
- Understanding of Hikari/Lightbulb framework
- Bot token configured

## Task 1: Bot Core Setup

### bot/bot.py

Create the main bot application:

```python
import os
import asyncio
import hikari
import lightbulb
from contextlib import asynccontextmanager
import structlog

from bot.config import BotConfig
from bot.services.api_client import APIClient
from bot.services.redis_subscriber import RedisSubscriber
from shared.logging import setup_logging

logger = structlog.get_logger()

class SmarterBot(lightbulb.BotApp):
    """Extended bot class with custom functionality."""
    
    def __init__(self, config: BotConfig):
        self.config = config
        
        # Initialize Hikari bot
        intents = (
            hikari.Intents.GUILDS |
            hikari.Intents.GUILD_MEMBERS |
            hikari.Intents.GUILD_MESSAGES |
            hikari.Intents.MESSAGE_CONTENT
        )
        
        super().__init__(
            token=config.bot_token,
            intents=intents,
            banner=None,
            default_enabled_guilds=config.default_guild_ids,
        )
        
        # Services
        self.api: APIClient = None
        self.redis_sub: RedisSubscriber = None
        
        # Setup logging
        setup_logging("bot", config.log_level, config.dev_mode)
    
    async def on_starting(self, event: hikari.StartingEvent) -> None:
        """Called when bot is starting."""
        logger.info("Bot starting...")
        
        # Initialize API client
        self.api = APIClient(self.config)
        await self.api.start()
        
        # Initialize Redis subscriber
        self.redis_sub = RedisSubscriber(self.config, self)
        await self.redis_sub.start()
        
        # Load plugins
        self.load_extensions_from("bot.plugins")
        
        logger.info("Bot initialization complete")
    
    async def on_started(self, event: hikari.StartedEvent) -> None:
        """Called when bot has started."""
        logger.info(
            "Bot started successfully",
            user=self.get_me().username,
            guilds=len(self.cache.get_guilds_view())
        )
    
    async def on_stopping(self, event: hikari.StoppingEvent) -> None:
        """Called when bot is stopping."""
        logger.info("Bot stopping...")
        
        # Cleanup
        if self.api:
            await self.api.stop()
        if self.redis_sub:
            await self.redis_sub.stop()
        
        logger.info("Bot stopped")

def create_bot(config: BotConfig = None) -> SmarterBot:
    """Create and configure the bot."""
    if config is None:
        config = BotConfig()
    
    bot = SmarterBot(config)
    
    # Register event listeners
    bot.subscribe(hikari.StartingEvent, bot.on_starting)
    bot.subscribe(hikari.StartedEvent, bot.on_started)
    bot.subscribe(hikari.StoppingEvent, bot.on_stopping)
    
    # Error handler
    @bot.listen(lightbulb.CommandErrorEvent)
    async def on_error(event: lightbulb.CommandErrorEvent) -> None:
        """Handle command errors."""
        exception = event.exception.__cause__ or event.exception
        
        if isinstance(exception, lightbulb.CommandNotFound):
            return  # Ignore
        
        logger.error(
            "Command error",
            command=event.context.command.name if event.context.command else "unknown",
            error=str(exception),
            exc_info=exception
        )
        
        # User-friendly error message
        embed = hikari.Embed(
            title="❌ Error",
            description="An error occurred while processing your command.",
            color=0xEF4444
        )
        
        if isinstance(exception, lightbulb.NotOwner):
            embed.description = "This command requires bot owner permissions."
        elif isinstance(exception, lightbulb.MissingRequiredPermission):
            embed.description = f"You need the `{exception.missing_perms}` permission."
        elif isinstance(exception, lightbulb.CommandIsOnCooldown):
            embed.description = f"Command on cooldown. Try again in {exception.retry_after:.1f}s."
        
        try:
            await event.context.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        except:
            pass  # Response failed, ignore
    
    return bot

if __name__ == "__main__":
    bot = create_bot()
    bot.run()
```

## Task 2: API Client Service

### bot/services/api_client.py

Create API client for web communication:

```python
import httpx
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import asyncio
import structlog

from bot.config import BotConfig
from shared.exceptions import APIError

logger = structlog.get_logger()

class APIClient:
    """Client for communicating with the web API."""
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.base_url = config.api_base_url
        self.api_key = config.api_key
        self._client: Optional[httpx.AsyncClient] = None
        self._token_refresh_task: Optional[asyncio.Task] = None
    
    async def start(self):
        """Initialize the API client."""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "SmarterBot/2.0"
            },
            timeout=30.0
        )
        logger.info("API client initialized", base_url=self.base_url)
    
    async def stop(self):
        """Close the API client."""
        if self._client:
            await self._client.aclose()
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs
    ) -> Dict[str, Any]:
        """Make an API request with error handling."""
        if not self._client:
            raise APIError("API client not initialized")
        
        try:
            response = await self._client.request(
                method,
                endpoint,
                **kwargs
            )
            
            response.raise_for_status()
            
            return response.json()
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                # Rate limited
                retry_after = int(e.response.headers.get("Retry-After", 60))
                logger.warning(
                    "API rate limited",
                    retry_after=retry_after
                )
                raise APIError(f"Rate limited. Retry after {retry_after}s")
            
            logger.error(
                "API request failed",
                status=e.response.status_code,
                endpoint=endpoint
            )
            
            # Try to parse error response
            try:
                error_data = e.response.json()
                raise APIError(
                    error_data.get("error", {}).get("message", "API request failed"),
                    code=error_data.get("error", {}).get("code")
                )
            except:
                raise APIError(f"API request failed: {e.response.status_code}")
                
        except Exception as e:
            logger.error(
                "API request error",
                endpoint=endpoint,
                error=str(e)
            )
            raise APIError(f"Request failed: {str(e)}")
    
    # Bytes endpoints
    async def get_bytes_balance(
        self,
        guild_id: str,
        user_id: str
    ) -> Dict[str, Any]:
        """Get user's bytes balance."""
        return await self._request(
            "GET",
            f"/v1/guilds/{guild_id}/bytes/balance/{user_id}"
        )
    
    async def award_daily_bytes(
        self,
        guild_id: str,
        user_id: str,
        username: str
    ) -> Dict[str, Any]:
        """Award daily bytes to user."""
        return await self._request(
            "POST",
            f"/v1/guilds/{guild_id}/bytes/daily",
            json={
                "user_id": user_id,
                "username": username
            }
        )
    
    async def transfer_bytes(
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
        return await self._request(
            "POST",
            f"/v1/guilds/{guild_id}/bytes/transfer",
            json={
                "giver_id": giver_id,
                "giver_username": giver_username,
                "receiver_id": receiver_id,
                "receiver_username": receiver_username,
                "amount": amount,
                "reason": reason
            }
        )
    
    async def get_bytes_leaderboard(
        self,
        guild_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get bytes leaderboard."""
        result = await self._request(
            "GET",
            f"/v1/guilds/{guild_id}/bytes/leaderboard",
            params={"limit": limit}
        )
        return result.get("leaderboard", [])
    
    async def get_bytes_config(self, guild_id: str) -> Dict[str, Any]:
        """Get guild bytes configuration."""
        return await self._request(
            "GET",
            f"/v1/guilds/{guild_id}/config/bytes"
        )
    
    # Squad endpoints
    async def get_squads(self, guild_id: str) -> List[Dict[str, Any]]:
        """Get all squads in guild."""
        result = await self._request(
            "GET",
            f"/v1/guilds/{guild_id}/squads"
        )
        return result.get("squads", [])
    
    async def join_squad(
        self,
        guild_id: str,
        user_id: str,
        squad_id: str
    ) -> Dict[str, Any]:
        """Join a squad."""
        return await self._request(
            "POST",
            f"/v1/guilds/{guild_id}/squads/{squad_id}/join",
            json={"user_id": user_id}
        )
    
    async def leave_squad(
        self,
        guild_id: str,
        user_id: str
    ) -> Dict[str, Any]:
        """Leave current squad."""
        return await self._request(
            "POST",
            f"/v1/guilds/{guild_id}/squads/leave",
            json={"user_id": user_id}
        )
    
    # Moderation endpoints
    async def create_moderation_case(
        self,
        guild_id: str,
        user_id: str,
        user_tag: str,
        moderator_id: str,
        moderator_tag: str,
        action: str,
        reason: str,
        expires_at: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Create moderation case."""
        data = {
            "user_id": user_id,
            "user_tag": user_tag,
            "moderator_id": moderator_id,
            "moderator_tag": moderator_tag,
            "action": action,
            "reason": reason
        }
        
        if expires_at:
            data["expires_at"] = expires_at.isoformat()
        
        return await self._request(
            "POST",
            f"/v1/guilds/{guild_id}/moderation/cases",
            json=data
        )
    
    async def get_automod_rules(self, guild_id: str) -> List[Dict[str, Any]]:
        """Get auto-moderation rules."""
        result = await self._request(
            "GET",
            f"/v1/guilds/{guild_id}/automod/rules"
        )
        return result.get("rules", [])
```

## Task 3: Redis Subscriber Service

### bot/services/redis_subscriber.py

Handle real-time configuration updates:

```python
import asyncio
import json
import redis.asyncio as redis
from typing import Optional, Callable, Dict, Any
import structlog

from bot.config import BotConfig

logger = structlog.get_logger()

class RedisSubscriber:
    """Subscribe to Redis pub/sub for real-time updates."""
    
    def __init__(self, config: BotConfig, bot):
        self.config = config
        self.bot = bot
        self._redis: Optional[redis.Redis] = None
        self._pubsub: Optional[redis.client.PubSub] = None
        self._task: Optional[asyncio.Task] = None
        
        # Handlers for different message types
        self._handlers: Dict[str, Callable] = {
            "config_update": self._handle_config_update,
            "squad_update": self._handle_squad_update,
            "role_sync": self._handle_role_sync
        }
    
    async def start(self):
        """Start the Redis subscriber."""
        self._redis = redis.from_url(
            self.config.redis_url,
            decode_responses=True
        )
        self._pubsub = self._redis.pubsub()
        
        # Subscribe to patterns
        await self._pubsub.psubscribe(
            "config_update:*",
            "squad_update:*", 
            "role_sync:*"
        )
        
        # Start listening task
        self._task = asyncio.create_task(self._listen())
        
        logger.info("Redis subscriber started")
    
    async def stop(self):
        """Stop the Redis subscriber."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
        
        if self._redis:
            await self._redis.close()
        
        logger.info("Redis subscriber stopped")
    
    async def _listen(self):
        """Listen for Redis messages."""
        try:
            async for message in self._pubsub.listen():
                if message["type"] in ("pmessage", "message"):
                    await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Redis listener error", error=str(e))
    
    async def _handle_message(self, message: Dict[str, Any]):
        """Handle incoming Redis message."""
        try:
            channel = message["channel"]
            data = message["data"]
            
            # Parse channel to get message type and guild ID
            parts = channel.split(":")
            if len(parts) != 2:
                return
            
            message_type, guild_id = parts
            
            # Get handler
            handler = self._handlers.get(message_type)
            if handler:
                await handler(guild_id, data)
            
        except Exception as e:
            logger.error(
                "Failed to handle Redis message",
                channel=message.get("channel"),
                error=str(e)
            )
    
    async def _handle_config_update(self, guild_id: str, data: str):
        """Handle configuration update."""
        logger.info(
            "Configuration updated",
            guild_id=guild_id,
            config_type=data
        )
        
        # Clear caches for this guild
        for plugin in self.bot.plugins.values():
            if hasattr(plugin.plugin, "clear_cache"):
                await plugin.plugin.clear_cache(guild_id)
    
    async def _handle_squad_update(self, guild_id: str, data: str):
        """Handle squad update."""
        try:
            update_data = json.loads(data)
            logger.info(
                "Squad updated",
                guild_id=guild_id,
                squad_id=update_data.get("squad_id")
            )
            
            # Notify squad plugin
            squad_plugin = self.bot.get_plugin("squads")
            if squad_plugin and hasattr(squad_plugin, "handle_update"):
                await squad_plugin.handle_update(guild_id, update_data)
                
        except Exception as e:
            logger.error("Failed to handle squad update", error=str(e))
    
    async def _handle_role_sync(self, guild_id: str, data: str):
        """Handle role sync request."""
        try:
            sync_data = json.loads(data)
            user_id = sync_data.get("user_id")
            
            logger.info(
                "Role sync requested",
                guild_id=guild_id,
                user_id=user_id
            )
            
            # Get guild and member
            guild = self.bot.cache.get_guild(int(guild_id))
            if not guild:
                return
            
            member = guild.get_member(int(user_id))
            if not member:
                return
            
            # Sync roles
            bytes_plugin = self.bot.get_plugin("bytes")
            if bytes_plugin and hasattr(bytes_plugin, "sync_user_roles"):
                await bytes_plugin.sync_user_roles(guild_id, user_id)
                
        except Exception as e:
            logger.error("Failed to handle role sync", error=str(e))
```

## Task 4: Base Plugin Class

### bot/plugins/base.py

Create base plugin with common functionality:

```python
import lightbulb
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import structlog

from bot.services.api_client import APIClient
from shared.utils import ttl_cache

logger = structlog.get_logger()

class BasePlugin(lightbulb.Plugin):
    """Base plugin with common functionality."""
    
    def __init__(self, name: str, description: str = None):
        super().__init__(name, description)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_times: Dict[str, datetime] = {}
    
    @property
    def bot(self) -> lightbulb.BotApp:
        """Get bot instance."""
        return self.app
    
    @property
    def api(self) -> APIClient:
        """Get API client."""
        return self.bot.api
    
    async def clear_cache(self, guild_id: Optional[str] = None):
        """Clear cache for guild or all."""
        if guild_id:
            # Clear specific guild
            keys_to_remove = [
                key for key in self._cache.keys()
                if key.startswith(f"{guild_id}:")
            ]
            for key in keys_to_remove:
                self._cache.pop(key, None)
                self._cache_times.pop(key, None)
        else:
            # Clear all
            self._cache.clear()
            self._cache_times.clear()
    
    def cache_key(self, guild_id: str, user_id: Optional[str] = None, suffix: Optional[str] = None) -> str:
        """Generate cache key."""
        parts = [guild_id]
        if user_id:
            parts.append(user_id)
        if suffix:
            parts.append(suffix)
        return ":".join(parts)
    
    async def get_cached(
        self,
        key: str,
        ttl: int = 300,
        factory = None
    ) -> Any:
        """Get value from cache or generate it."""
        now = datetime.utcnow()
        
        # Check cache
        if key in self._cache:
            cache_time = self._cache_times.get(key)
            if cache_time and (now - cache_time).total_seconds() < ttl:
                return self._cache[key]
        
        # Generate value if factory provided
        if factory:
            value = await factory()
            self._cache[key] = value
            self._cache_times[key] = now
            return value
        
        return None
    
    def set_cache(self, key: str, value: Any):
        """Set cache value."""
        self._cache[key] = value
        self._cache_times[key] = datetime.utcnow()
    
    async def send_error(
        self,
        ctx: lightbulb.Context,
        message: str,
        *,
        ephemeral: bool = True
    ):
        """Send error message to user."""
        embed = hikari.Embed(
            title="❌ Error",
            description=message,
            color=0xEF4444
        )
        
        flags = hikari.MessageFlag.EPHEMERAL if ephemeral else hikari.MessageFlag.NONE
        await ctx.respond(embed=embed, flags=flags)
    
    async def send_success(
        self,
        ctx: lightbulb.Context,
        message: str,
        *,
        ephemeral: bool = False
    ):
        """Send success message to user."""
        embed = hikari.Embed(
            title="✅ Success",
            description=message,
            color=0x22C55E
        )
        
        flags = hikari.MessageFlag.EPHEMERAL if ephemeral else hikari.MessageFlag.NONE
        await ctx.respond(embed=embed, flags=flags)
    
    def check_bot_permissions(
        self,
        ctx: lightbulb.Context,
        *permissions: hikari.Permissions
    ) -> bool:
        """Check if bot has required permissions."""
        if not ctx.guild_id:
            return True
        
        bot_member = ctx.app.cache.get_member(ctx.guild_id, ctx.app.get_me().id)
        if not bot_member:
            return False
        
        bot_perms = lightbulb.utils.permissions_for(bot_member)
        
        for perm in permissions:
            if perm not in bot_perms:
                return False
        
        return True
```

## Task 5: Utility Functions

### bot/utils/embeds.py

Create embed builders:

```python
import hikari
from typing import Optional, List, Tuple
from datetime import datetime
from shared.constants import COLOR_SUCCESS, COLOR_ERROR, COLOR_INFO, COLOR_WARNING

class EmbedBuilder:
    """Utility for building consistent embeds."""
    
    @staticmethod
    def success(
        title: str,
        description: Optional[str] = None,
        *,
        fields: Optional[List[Tuple[str, str, bool]]] = None,
        footer: Optional[str] = None
    ) -> hikari.Embed:
        """Build success embed."""
        embed = hikari.Embed(
            title=f"✅ {title}",
            description=description,
            color=COLOR_SUCCESS,
            timestamp=datetime.utcnow()
        )
        
        if fields:
            for name, value, inline in fields:
                embed.add_field(name, value, inline=inline)
        
        if footer:
            embed.set_footer(footer)
        
        return embed
    
    @staticmethod
    def error(
        title: str = "Error",
        description: Optional[str] = None,
        *,
        fields: Optional[List[Tuple[str, str, bool]]] = None
    ) -> hikari.Embed:
        """Build error embed."""
        embed = hikari.Embed(
            title=f"❌ {title}",
            description=description or "An error occurred.",
            color=COLOR_ERROR,
            timestamp=datetime.utcnow()
        )
        
        if fields:
            for name, value, inline in fields:
                embed.add_field(name, value, inline=inline)
        
        return embed
    
    @staticmethod
    def info(
        title: str,
        description: Optional[str] = None,
        *,
        fields: Optional[List[Tuple[str, str, bool]]] = None,
        thumbnail: Optional[str] = None,
        footer: Optional[str] = None
    ) -> hikari.Embed:
        """Build info embed."""
        embed = hikari.Embed(
            title=title,
            description=description,
            color=COLOR_INFO,
            timestamp=datetime.utcnow()
        )
        
        if fields:
            for name, value, inline in fields:
                embed.add_field(name, value, inline=inline)
        
        if thumbnail:
            embed.set_thumbnail(thumbnail)
        
        if footer:
            embed.set_footer(footer)
        
        return embed
    
    @staticmethod
    def bytes_balance(
        user: hikari.User,
        balance: int,
        total_received: int,
        total_sent: int,
        streak: int,
        daily_available: bool = False
    ) -> hikari.Embed:
        """Build bytes balance embed."""
        embed = hikari.Embed(
            title="💰 Bytes Balance",
            color=COLOR_INFO,
            timestamp=datetime.utcnow()
        )
        
        embed.set_author(
            name=f"{user.username}'s Balance",
            icon=user.display_avatar_url
        )
        
        # Balance info
        embed.add_field(
            "Current Balance",
            f"**{balance:,}** bytes",
            inline=True
        )
        
        embed.add_field(
            "Total Received",
            f"{total_received:,} bytes",
            inline=True
        )
        
        embed.add_field(
            "Total Sent",
            f"{total_sent:,} bytes",
            inline=True
        )
        
        # Streak info
        if streak > 0:
            from shared.types import StreakMultiplier
            multiplier = StreakMultiplier.from_streak(streak)
            
            embed.add_field(
                "Current Streak",
                f"🔥 {streak} days ({multiplier.display})",
                inline=False
            )
        
        if daily_available:
            embed.set_footer("💡 Daily bytes available! They'll be awarded on your next message.")
        
        return embed
    
    @staticmethod
    def leaderboard(
        title: str,
        entries: List[Tuple[str, int, int]],  # (username, balance, rank)
        *,
        footer: Optional[str] = None
    ) -> hikari.Embed:
        """Build leaderboard embed."""
        embed = hikari.Embed(
            title=f"🏆 {title}",
            color=COLOR_INFO,
            timestamp=datetime.utcnow()
        )
        
        # Build description
        description_lines = []
        medals = ["🥇", "🥈", "🥉"]
        
        for username, balance, rank in entries:
            medal = medals[rank - 1] if rank <= 3 else f"**{rank}.**"
            description_lines.append(
                f"{medal} {username} • **{balance:,}** bytes"
            )
        
        embed.description = "\n".join(description_lines)
        
        if footer:
            embed.set_footer(footer)
        
        return embed
```

### bot/utils/checks.py

Permission and cooldown checks:

```python
import lightbulb
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import structlog

logger = structlog.get_logger()

# Cooldown storage
_cooldowns: Dict[str, datetime] = {}

def cooldown(seconds: int, bucket: lightbulb.buckets.Bucket = lightbulb.buckets.UserBucket):
    """Custom cooldown decorator with better error messages."""
    def decorator(func):
        # Apply Lightbulb's cooldown
        func = lightbulb.decorators.add_cooldown(
            lightbulb.buckets.Bucket(
                length=seconds,
                bucket=bucket
            )
        )(func)
        
        # Store cooldown info for error messages
        func._cooldown_seconds = seconds
        
        return func
    
    return decorator

def guild_only():
    """Ensure command is used in a guild."""
    def predicate(ctx: lightbulb.Context) -> bool:
        if not ctx.guild_id:
            raise lightbulb.errors.OnlyInGuild(
                "This command can only be used in a server."
            )
        return True
    
    return lightbulb.decorators.add_checks(lightbulb.Check(predicate))

def has_guild_permissions(**perms: bool):
    """Check if user has guild permissions."""
    def predicate(ctx: lightbulb.Context) -> bool:
        if not ctx.guild_id:
            return False
        
        member = ctx.member
        if not member:
            return False
        
        member_perms = lightbulb.utils.permissions_for(member)
        
        for perm, value in perms.items():
            perm_attr = getattr(hikari.Permissions, perm.upper(), None)
            if perm_attr and value:
                if perm_attr not in member_perms:
                    raise lightbulb.MissingRequiredPermission(
                        perms=perm_attr
                    )
        
        return True
    
    return lightbulb.decorators.add_checks(lightbulb.Check(predicate))

async def check_bytes_cooldown(
    user_id: str,
    guild_id: str,
    action: str,
    cooldown_hours: int
) -> Optional[timedelta]:
    """Check if user is on cooldown for bytes action."""
    key = f"{guild_id}:{user_id}:{action}"
    
    now = datetime.utcnow()
    last_action = _cooldowns.get(key)
    
    if last_action:
        time_passed = now - last_action
        cooldown_duration = timedelta(hours=cooldown_hours)
        
        if time_passed < cooldown_duration:
            remaining = cooldown_duration - time_passed
            return remaining
    
    # Update last action time
    _cooldowns[key] = now
    return None
```

## Task 6: Error Handling

### bot/errors.py

Custom error classes:

```python
from typing import Optional

class BotError(Exception):
    """Base exception for bot errors."""
    
    def __init__(self, message: str, *, user_message: Optional[str] = None):
        super().__init__(message)
        self.user_message = user_message or message

class ConfigurationError(BotError):
    """Raised when configuration is missing or invalid."""
    pass

class InsufficientBytesError(BotError):
    """Raised when user doesn't have enough bytes."""
    
    def __init__(self, current: int, required: int):
        super().__init__(
            f"Insufficient bytes: has {current}, needs {required}",
            user_message=f"You need **{required:,}** bytes but only have **{current:,}** bytes."
        )
        self.current = current
        self.required = required

class CooldownError(BotError):
    """Raised when action is on cooldown."""
    
    def __init__(self, remaining: timedelta):
        hours = int(remaining.total_seconds() // 3600)
        minutes = int((remaining.total_seconds() % 3600) // 60)
        
        if hours > 0:
            time_str = f"{hours}h {minutes}m"
        else:
            time_str = f"{minutes}m"
        
        super().__init__(
            f"Action on cooldown for {time_str}",
            user_message=f"You're on cooldown! Try again in **{time_str}**."
        )
        self.remaining = remaining

class SquadError(BotError):
    """Raised for squad-related errors."""
    pass

class APIError(BotError):
    """Raised when API calls fail."""
    
    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(
            message,
            user_message="Failed to communicate with the server. Please try again later."
        )
        self.code = code
```

## Task 7: Main Entry Point

### bot/__main__.py

Create main entry point:

```python
#!/usr/bin/env python
"""Main entry point for the Discord bot."""

import sys
import signal
import asyncio
from bot.bot import create_bot
from bot.config import BotConfig
import structlog

logger = structlog.get_logger()

def signal_handler(sig, frame):
    """Handle shutdown signals."""
    logger.info("Received shutdown signal")
    sys.exit(0)

def main():
    """Run the bot."""
    # Handle shutdown signals
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Load configuration
        config = BotConfig()
        
        # Create and run bot
        bot = create_bot(config)
        bot.run()
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error("Bot crashed", error=str(e), exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

## Task 8: Create Tests

### tests/test_bot_core.py

Test the bot core functionality:

```python
import pytest
from unittest.mock import Mock, AsyncMock, patch
import hikari
import lightbulb

from bot.bot import SmarterBot, create_bot
from bot.config import BotConfig
from bot.services.api_client import APIClient

@pytest.fixture
def bot_config():
    """Create test bot configuration."""
    return BotConfig(
        bot_token="test_token",
        api_key="test_api_key",
        dev_mode=True
    )

@pytest.fixture
def mock_bot(bot_config):
    """Create mock bot instance."""
    with patch("hikari.GatewayBot"):
        bot = SmarterBot(bot_config)
        bot.get_me = Mock(return_value=Mock(username="TestBot"))
        return bot

def test_bot_creation(bot_config):
    """Test bot is created with correct configuration."""
    bot = create_bot(bot_config)
    
    assert isinstance(bot, SmarterBot)
    assert bot.config == bot_config
    assert bot.api is None  # Not initialized until start

@pytest.mark.asyncio
async def test_bot_startup(mock_bot):
    """Test bot startup initializes services."""
    # Mock API client
    with patch("bot.bot.APIClient") as mock_api_class:
        mock_api = AsyncMock()
        mock_api_class.return_value = mock_api
        
        # Mock Redis subscriber
        with patch("bot.bot.RedisSubscriber") as mock_redis_class:
            mock_redis = AsyncMock()
            mock_redis_class.return_value = mock_redis
            
            # Trigger startup
            event = Mock(spec=hikari.StartingEvent)
            await mock_bot.on_starting(event)
            
            # Verify services initialized
            assert mock_bot.api == mock_api
            assert mock_bot.redis_sub == mock_redis
            mock_api.start.assert_called_once()
            mock_redis.start.assert_called_once()

@pytest.mark.asyncio
async def test_api_client_error_handling():
    """Test API client handles errors properly."""
    config = BotConfig(api_key="test_key")
    client = APIClient(config)
    
    # Mock httpx client
    mock_response = Mock()
    mock_response.status_code = 404
    mock_response.json.return_value = {
        "error": {
            "code": "NOT_FOUND",
            "message": "Guild not found"
        }
    }
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not found",
        request=Mock(),
        response=mock_response
    )
    
    with patch.object(client, "_client") as mock_client:
        mock_client.request = AsyncMock(return_value=mock_response)
        
        with pytest.raises(APIError) as exc_info:
            await client.get_bytes_balance("123", "456")
        
        assert "Guild not found" in str(exc_info.value)
```

### tests/test_bot_utils.py

Test bot utilities:

```python
import pytest
from datetime import datetime, timedelta
import hikari

from bot.utils.embeds import EmbedBuilder
from bot.utils.checks import check_bytes_cooldown

def test_embed_builder_success():
    """Test success embed builder."""
    embed = EmbedBuilder.success(
        "Test Success",
        "This is a test",
        fields=[("Field 1", "Value 1", True)]
    )
    
    assert embed.title == "✅ Test Success"
    assert embed.description == "This is a test"
    assert embed.color == 0x22C55E
    assert len(embed.fields) == 1

def test_embed_builder_bytes_balance():
    """Test bytes balance embed."""
    user = Mock(
        username="TestUser",
        display_avatar_url="https://example.com/avatar.png"
    )
    
    embed = EmbedBuilder.bytes_balance(
        user=user,
        balance=1000,
        total_received=2000,
        total_sent=1000,
        streak=10,
        daily_available=True
    )
    
    assert "💰 Bytes Balance" in embed.title
    assert "1,000" in str(embed.fields[0].value)
    assert "🔥 10 days" in str(embed.fields[3].value)
    assert "Daily bytes available!" in embed.footer.text

@pytest.mark.asyncio
async def test_cooldown_check():
    """Test cooldown checking."""
    # First check - no cooldown
    remaining = await check_bytes_cooldown("user1", "guild1", "transfer", 24)
    assert remaining is None
    
    # Second check - on cooldown
    remaining = await check_bytes_cooldown("user1", "guild1", "transfer", 24)
    assert remaining is not None
    assert remaining.total_seconds() > 0
    
    # Different action - no cooldown
    remaining = await check_bytes_cooldown("user1", "guild1", "daily", 24)
    assert remaining is None
```

## Deliverables

1. **Bot Core**
   - Hikari + Lightbulb setup
   - Clean startup/shutdown
   - Error handling
   - Event listeners

2. **API Client Service**
   - Full API coverage
   - Error handling
   - Retry logic
   - Type safety

3. **Redis Subscriber**
   - Real-time config updates
   - Message routing
   - Cache invalidation

4. **Base Plugin Class**
   - Common functionality
   - Caching helpers
   - Error responses
   - Permission checks

5. **Utilities**
   - Embed builders
   - Permission checks
   - Cooldown system
   - Error classes

6. **Test Coverage**
   - Bot lifecycle tests
   - Service tests
   - Utility tests

## Important Notes

1. Use service layer pattern for testability
2. All business logic in services, not commands
3. Comprehensive error handling with user-friendly messages
4. Cache Discord data appropriately
5. Handle API failures gracefully
6. Clean shutdown on SIGTERM for containers

This bot architecture provides a solid foundation for implementing features while maintaining testability.