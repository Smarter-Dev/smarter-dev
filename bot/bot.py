"""
Smarter Dev Discord Bot implementation using Hikari and Hikari Lightbulb.

This module contains the main bot implementation with command handlers.
"""

import os
import logging
import hikari
import lightbulb

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
    bot = lightbulb.BotApp(
        token=token,
        prefix="!",  # Default command prefix
        intents=hikari.Intents.ALL_UNPRIVILEGED | hikari.Intents.MESSAGE_CONTENT,
        logs={
            "version": 1,
            "incremental": True,
            "loggers": {
                "hikari": {"level": "INFO"},
                "lightbulb": {"level": "INFO"},
            },
        },
    )
    
    # Register event listeners
    @bot.listen(hikari.StartedEvent)
    async def on_started(event: hikari.StartedEvent) -> None:
        """
        Event fired when the bot starts.
        """
        logger.info("Bot has started!")
    
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
    
    return bot
