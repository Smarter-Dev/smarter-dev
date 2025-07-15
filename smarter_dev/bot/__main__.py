"""Entry point for running the Discord bot."""

import asyncio

from smarter_dev.bot.client import run_bot

if __name__ == "__main__":
    asyncio.run(run_bot())