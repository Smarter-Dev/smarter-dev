"""Discord bot client setup and configuration."""

from __future__ import annotations

import logging
from typing import Optional

import hikari
import lightbulb

from smarter_dev.shared.config import Settings
from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)


def create_bot(settings: Optional[Settings] = None) -> tuple[lightbulb.BotApp, lightbulb.Client]:
    """Create and configure the Discord bot with Lightbulb v3.0.1 syntax.
    
    Returns:
        Tuple of (bot, client) for v3.0.1 compatibility
    """
    if settings is None:
        settings = get_settings()
    
    # Configure bot intents
    intents = (
        hikari.Intents.GUILDS
        | hikari.Intents.GUILD_MEMBERS  # For member tracking
        | hikari.Intents.GUILD_MESSAGES  # For activity tracking
        | hikari.Intents.MESSAGE_CONTENT  # For message content
    )
    
    # Create bot instance using Hikari directly
    bot = hikari.GatewayBot(
        token=settings.discord_bot_token,
        intents=intents,
        logs={
            "version": 1,
            "incremental": True,
            "loggers": {
                "hikari": {"level": "INFO"},
                "hikari.ratelimits": {"level": "TRACE"},
                "lightbulb": {"level": "INFO"},
            },
        },
        banner=None,  # Disable banner for cleaner logs
    )
    
    # Create Lightbulb client from bot (v3 syntax)
    client = lightbulb.client_from_app(bot)
    
    # Store settings in bot data
    bot.d.settings = settings
    
    # Initialize service placeholders
    bot.d.bytes_service = None
    bot.d.squads_service = None
    bot.d.api_client = None
    bot.d.cache_manager = None
    
    # Subscribe client to bot events (v3 syntax)
    bot.subscribe(hikari.StartingEvent, client.start)
    
    return bot, client


async def setup_bot_services(bot: hikari.GatewayBot) -> None:
    """Set up bot services and dependencies."""
    settings = bot.d.settings
    
    logger.info("Setting up bot services...")
    
    try:
        # Initialize API client for backend communication
        from smarter_dev.bot.services.api_client import APIClient
        bot.d.api_client = APIClient(
            base_url=settings.api_base_url or "http://localhost:8000/api",
            bot_token=settings.api_secret_key
        )
        
        # Initialize cache manager (Redis client)
        from smarter_dev.shared.redis_client import get_redis_client
        bot.d.cache_manager = await get_redis_client()
        
        # Initialize services using existing Session 4 implementations
        from smarter_dev.bot.services.bytes_service import BytesService
        from smarter_dev.bot.services.squads_service import SquadsService
        
        bot.d.bytes_service = BytesService(
            api_client=bot.d.api_client,
            cache_manager=bot.d.cache_manager
        )
        
        bot.d.squads_service = SquadsService(
            api_client=bot.d.api_client,
            cache_manager=bot.d.cache_manager
        )
        
        logger.info("Bot services initialized successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize bot services: {e}")
        # Services will remain None, commands will handle this gracefully
        bot.d.bytes_service = None
        bot.d.squads_service = None
    
    logger.info("Bot services setup complete")


async def cleanup_bot_services(bot: hikari.GatewayBot) -> None:
    """Clean up bot services and connections."""
    logger.info("Cleaning up bot services...")
    
    try:
        # Close API client connections
        if hasattr(bot.d, 'api_client') and bot.d.api_client:
            await bot.d.api_client.close()
        
        # Close Redis connections
        if hasattr(bot.d, 'cache_manager') and bot.d.cache_manager:
            await bot.d.cache_manager.close()
    
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
    
    logger.info("Bot services cleanup complete")


def load_plugins(bot: hikari.GatewayBot, client: lightbulb.Client) -> None:
    """Load bot plugins using Lightbulb v3.0.1 syntax."""
    try:
        # Load bytes commands plugin
        from smarter_dev.bot.plugins import bytes as bytes_plugin
        bytes_plugin.load(bot)
        logger.info("Loaded bytes plugin")
        
        # Load squads commands plugin
        from smarter_dev.bot.plugins import squads as squads_plugin
        squads_plugin.load(bot)
        logger.info("Loaded squads plugin")
        
        logger.info("All plugins loaded successfully")
    except Exception as e:
        logger.error(f"Failed to load plugins: {e}")
        raise


async def run_bot() -> None:
    """Run the Discord bot with Lightbulb v3.0.1 syntax."""
    settings = get_settings()
    
    if not settings.discord_bot_token:
        logger.error("Discord bot token not provided")
        return
    
    if not settings.discord_application_id:
        logger.error("Discord application ID not provided")
        return
    
    # Create bot and client
    bot, client = create_bot(settings)
    
    # Set up event handlers
    @bot.listen(hikari.StartingEvent)
    async def on_starting(event: hikari.StartingEvent) -> None:
        """Handle bot starting event."""
        logger.info("Bot is starting...")
        await setup_bot_services(event.app)
    
    @bot.listen(hikari.StartedEvent)
    async def on_started(event: hikari.StartedEvent) -> None:
        """Handle bot started event."""
        bot_user = event.app.get_me()
        if bot_user:
            logger.info(f"Bot started as {bot_user.username}#{bot_user.discriminator}")
        else:
            logger.info("Bot started")
        
        # Load plugins after bot is started and services are initialized
        load_plugins(event.app, client)
        
        # Set up interaction handlers
        from smarter_dev.bot.plugins import events as events_plugin
        events_plugin.load(event.app)
    
    @bot.listen(hikari.StoppingEvent)
    async def on_stopping(event: hikari.StoppingEvent) -> None:
        """Handle bot stopping event."""
        logger.info("Bot is stopping...")
        await cleanup_bot_services(event.app)
    
    @bot.listen(hikari.ShardReadyEvent)
    async def on_ready(event: hikari.ShardReadyEvent) -> None:
        """Handle shard ready event."""
        logger.info(f"Shard {event.shard.id} is ready")
    
    # Run bot
    await bot.start()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_bot())