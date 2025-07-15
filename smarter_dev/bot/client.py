"""Discord bot client setup and configuration."""

from __future__ import annotations

import logging
from typing import Optional

import hikari
import lightbulb

from smarter_dev.shared.config import Settings
from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)


def create_bot(settings: Optional[Settings] = None) -> lightbulb.BotApp:
    """Create and configure the Discord bot."""
    if settings is None:
        settings = get_settings()
    
    # Configure bot intents
    intents = (
        hikari.Intents.GUILDS
        | hikari.Intents.GUILD_MEMBERS  # For member tracking
        | hikari.Intents.GUILD_MESSAGES  # For activity tracking
        | hikari.Intents.MESSAGE_CONTENT  # For message content
    )
    
    # Create bot instance
    bot = lightbulb.BotApp(
        token=settings.discord_bot_token,
        default_enabled_guilds=None,  # Global commands
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
    
    # Store settings in bot data
    bot.d.settings = settings
    
    # Initialize services (will be added in later sessions)
    bot.d.bytes_service = None
    bot.d.squads_service = None
    bot.d.db_manager = None
    bot.d.redis_manager = None
    
    return bot


async def setup_bot_services(bot: lightbulb.BotApp) -> None:
    """Set up bot services and dependencies."""
    settings = bot.d.settings
    
    # Initialize database and Redis connections
    # This will be implemented in later sessions
    logger.info("Setting up bot services...")
    
    # TODO: Initialize database manager
    # from smarter_dev.shared.database import DatabaseManager
    # bot.d.db_manager = DatabaseManager(settings)
    # await bot.d.db_manager.init()
    
    # TODO: Initialize Redis manager  
    # from smarter_dev.shared.redis_client import RedisManager
    # bot.d.redis_manager = RedisManager(settings)
    # await bot.d.redis_manager.init()
    
    # TODO: Initialize services
    # from smarter_dev.bot.services.bytes_service import BytesService
    # from smarter_dev.bot.services.squads_service import SquadsService
    # bot.d.bytes_service = BytesService(bot.d.db_manager, bot.d.redis_manager)
    # bot.d.squads_service = SquadsService(bot.d.db_manager, bot.d.redis_manager)
    
    logger.info("Bot services setup complete")


async def cleanup_bot_services(bot: lightbulb.BotApp) -> None:
    """Clean up bot services and connections."""
    logger.info("Cleaning up bot services...")
    
    # Close database connections
    if hasattr(bot.d, 'db_manager') and bot.d.db_manager:
        await bot.d.db_manager.close()
    
    # Close Redis connections
    if hasattr(bot.d, 'redis_manager') and bot.d.redis_manager:
        await bot.d.redis_manager.close()
    
    logger.info("Bot services cleanup complete")


def load_plugins(bot: lightbulb.BotApp) -> None:
    """Load bot plugins."""
    # TODO: Load plugins in later sessions
    # bot.load_extensions_from("smarter_dev.bot.plugins", must_exist=True)
    logger.info("Plugins loaded")


@lightbulb.BotApp.listen()
async def on_starting(event: lightbulb.LightbulbStartingEvent) -> None:
    """Handle bot starting event."""
    logger.info("Bot is starting...")
    await setup_bot_services(event.app)


@lightbulb.BotApp.listen()
async def on_started(event: lightbulb.LightbulbStartedEvent) -> None:
    """Handle bot started event."""
    bot_user = event.app.get_me()
    if bot_user:
        logger.info(f"Bot started as {bot_user.username}#{bot_user.discriminator}")
    else:
        logger.info("Bot started")


@lightbulb.BotApp.listen()
async def on_stopping(event: lightbulb.LightbulbStoppingEvent) -> None:
    """Handle bot stopping event."""
    logger.info("Bot is stopping...")
    await cleanup_bot_services(event.app)


@lightbulb.BotApp.listen()
async def on_ready(event: hikari.ShardReadyEvent) -> None:
    """Handle shard ready event."""
    logger.info(f"Shard {event.shard.id} is ready")


async def run_bot() -> None:
    """Run the Discord bot."""
    settings = get_settings()
    
    if not settings.discord_bot_token:
        logger.error("Discord bot token not provided")
        return
    
    if not settings.discord_application_id:
        logger.error("Discord application ID not provided")
        return
    
    # Create bot
    bot = create_bot(settings)
    
    # Load plugins
    load_plugins(bot)
    
    # Run bot
    await bot.start()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_bot())