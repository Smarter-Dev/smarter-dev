"""
Entry point for running the Smarter Dev Discord Bot.

This module imports the bot from bot.py and runs it.
"""

import os
import sys
import logging

from dotenv import load_dotenv

from bot import bot

load_dotenv()

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("bot.run")

try:
    # Create and run the bot
    logger.info("Starting Smarter Dev Discord Bot...")
    bot_app = bot.create_bot()
    bot_app.run()
except Exception as e:
    logger.error(f"Error running bot: {e}")
    sys.exit(1)
