"""Advent of Code daily thread service for Discord bot.

This service handles automatic creation of daily discussion threads for
Advent of Code challenges. Threads are created at midnight EST (slightly early
to account for timing variance) in configured forum channels.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import hikari

from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.cache_manager import CacheManager
from smarter_dev.bot.services.exceptions import ServiceError
from smarter_dev.bot.services.models import ServiceHealth

logger = logging.getLogger(__name__)

# Eastern Standard Time timezone
EST = ZoneInfo("America/New_York")

# Advent of Code runs December 1-25
AOC_START_DAY = 1
AOC_END_DAY = 25
AOC_MONTH = 12

# Post threads 2 seconds early to ensure they appear right at midnight
EARLY_POST_SECONDS = 2


class AdventOfCodeService(BaseService):
    """Service for managing Advent of Code daily threads."""

    def __init__(self, api_client: APIClient, cache_manager: CacheManager | None, bot: hikari.BotApp):
        """Initialize the Advent of Code service.

        Args:
            api_client: HTTP API client for web service communication
            cache_manager: Cache manager for caching operations (optional)
            bot: Discord bot instance for creating threads
        """
        super().__init__(api_client, cache_manager)
        self._bot = bot
        self._scheduler_task: asyncio.Task | None = None
        self._running = False

    async def initialize(self) -> None:
        """Initialize the Advent of Code service and start the scheduler."""
        await super().initialize()
        await self.start_scheduler()
        logger.info("Advent of Code service initialized with scheduler")

    async def cleanup(self) -> None:
        """Clean up resources and stop the scheduler."""
        await self.stop_scheduler()
        await super().cleanup()
        logger.info("Advent of Code service cleaned up")

    async def health_check(self) -> ServiceHealth:
        """Check the health of the Advent of Code service.

        Returns:
            ServiceHealth object with status and details
        """
        try:
            scheduler_status = "running" if self._running and self._scheduler_task else "stopped"
            now_est = datetime.now(EST)

            # Check if we're in the AoC active period (December)
            is_aoc_month = now_est.month == AOC_MONTH
            current_day = now_est.day if is_aoc_month else None

            return ServiceHealth(
                service_name="AdventOfCodeService",
                is_healthy=True,
                details={
                    "scheduler_status": scheduler_status,
                    "bot_connected": self._bot.is_alive if hasattr(self._bot, "is_alive") else True,
                    "is_aoc_month": is_aoc_month,
                    "current_est_time": now_est.isoformat(),
                    "current_aoc_day": current_day
                }
            )
        except Exception as e:
            logger.error(f"Advent of Code service health check failed: {e}")
            return ServiceHealth(
                service_name="AdventOfCodeService",
                is_healthy=False,
                details={"error": str(e)}
            )

    async def start_scheduler(self) -> None:
        """Start the background task for scheduling thread creation."""
        if self._running:
            return

        self._running = True
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("Started Advent of Code scheduler")

    async def stop_scheduler(self) -> None:
        """Stop the background scheduler."""
        self._running = False

        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None

        logger.info("Stopped Advent of Code scheduler")

    async def _scheduler_loop(self) -> None:
        """Main loop for scheduling Advent of Code thread creation."""
        while self._running:
            try:
                await self._check_and_create_threads()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Advent of Code scheduler loop: {e}")

            # Wait until the next check time
            try:
                await self._wait_until_next_check()
            except asyncio.CancelledError:
                break

    async def _wait_until_next_check(self) -> None:
        """Wait until the next midnight EST (minus early offset) or check interval."""
        now_est = datetime.now(EST)

        # If we're not in December, just check once per hour
        if now_est.month != AOC_MONTH:
            logger.debug("Not December, waiting 1 hour before next check")
            await asyncio.sleep(3600)  # 1 hour
            return

        # If we're past day 25, wait until next December (or just check hourly)
        if now_est.day > AOC_END_DAY:
            logger.debug("Past December 25, waiting 1 hour before next check")
            await asyncio.sleep(3600)
            return

        # Calculate time until next midnight EST (minus early offset)
        next_midnight = now_est.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)

        # Subtract the early posting offset
        target_time = next_midnight - timedelta(seconds=EARLY_POST_SECONDS)

        wait_seconds = (target_time - now_est).total_seconds()

        # If wait time is negative (we're very close to or past target),
        # wait a short time and recheck
        if wait_seconds <= 0:
            wait_seconds = 10  # Brief wait then recheck

        # Cap maximum wait at 1 hour for responsiveness
        wait_seconds = min(wait_seconds, 3600)

        logger.debug(f"Waiting {wait_seconds:.1f} seconds until next AoC check (target: {target_time})")
        await asyncio.sleep(wait_seconds)

    async def _check_and_create_threads(self) -> None:
        """Check if threads need to be created and create them."""
        now_est = datetime.now(EST)

        # Only proceed if we're in December, days 1-25
        if now_est.month != AOC_MONTH:
            logger.debug(f"Not December (month={now_est.month}), skipping AoC check")
            return

        if now_est.day < AOC_START_DAY or now_est.day > AOC_END_DAY:
            logger.debug(f"Outside AoC days 1-25 (day={now_est.day}), skipping")
            return

        current_day = now_est.day
        current_year = now_est.year

        logger.info(f"Checking for AoC threads for {current_year} Day {current_day}")

        # Get all active configurations
        try:
            active_configs = await self._get_active_configs()
        except Exception as e:
            logger.error(f"Failed to get active AoC configs: {e}")
            return

        if not active_configs:
            logger.debug("No active AoC configurations found")
            return

        logger.info(f"Found {len(active_configs)} active AoC configurations")

        # Process each configuration
        for config in active_configs:
            try:
                await self._process_config(config, current_year, current_day)
            except Exception as e:
                logger.error(f"Failed to process AoC config for guild {config.get('guild_id')}: {e}")

    async def _process_config(self, config: dict[str, Any], year: int, day: int) -> None:
        """Process a single guild's AoC configuration.

        Args:
            config: Configuration dictionary
            year: Current AoC year
            day: Current AoC day
        """
        guild_id = config.get("guild_id")
        forum_channel_id = config.get("forum_channel_id")
        config_year = config.get("year", year)

        # Only process if the config year matches current year
        if config_year != year:
            logger.debug(f"Config year {config_year} doesn't match current year {year} for guild {guild_id}")
            return

        if not forum_channel_id:
            logger.warning(f"No forum channel configured for guild {guild_id}")
            return

        # Check if thread already exists for this day
        try:
            existing_thread = await self._get_posted_thread(guild_id, year, day)
            if existing_thread:
                logger.debug(f"Thread already exists for guild {guild_id}, year {year}, day {day}")
                return
        except Exception as e:
            logger.error(f"Failed to check existing thread: {e}")
            return

        # Create the thread
        logger.info(f"Creating AoC thread for guild {guild_id}, day {day}")
        await self._create_aoc_thread(guild_id, forum_channel_id, year, day)

    async def _create_aoc_thread(
        self,
        guild_id: str,
        forum_channel_id: str,
        year: int,
        day: int
    ) -> None:
        """Create an Advent of Code discussion thread.

        Args:
            guild_id: Discord guild ID
            forum_channel_id: Discord forum channel ID
            year: AoC year
            day: AoC day (1-25)
        """
        thread_title = f"Day {day} - Advent of Code"
        aoc_url = f"https://adventofcode.com/{year}/day/{day}"

        # Create the thread content
        thread_content = (
            f"**Advent of Code {year} - Day {day}**\n\n"
            f"Today's challenge is live!\n\n"
            f"{aoc_url}\n\n"
            f"Share your solutions, discuss approaches, and help each other out. "
            f"Please use spoiler tags (`||spoiler||`) when discussing solutions!"
        )

        try:
            channel_id_int = int(forum_channel_id)

            # Create a forum post (thread) in the forum channel
            thread = await self._bot.rest.create_forum_post(
                channel=channel_id_int,
                name=thread_title,
                content=thread_content,
            )

            thread_id = str(thread.id)
            logger.info(f"Created AoC thread {thread_id} in guild {guild_id} for day {day}")

            # Record the thread creation
            await self._record_posted_thread(guild_id, year, day, thread_id, thread_title)

        except hikari.NotFoundError as e:
            logger.error(f"Forum channel {forum_channel_id} not found in guild {guild_id}: {e}")
            raise ServiceError(f"Forum channel not found: {forum_channel_id}") from e
        except hikari.ForbiddenError as e:
            logger.error(f"No permission to create thread in channel {forum_channel_id}: {e}")
            raise ServiceError(f"No permission to create thread in channel: {forum_channel_id}") from e
        except ValueError as e:
            logger.error(f"Invalid forum channel ID format: {forum_channel_id}")
            raise ServiceError(f"Invalid forum channel ID: {forum_channel_id}") from e
        except Exception as e:
            logger.error(f"Failed to create AoC thread: {e}")
            raise ServiceError(f"Failed to create AoC thread: {str(e)}") from e

    async def _get_active_configs(self) -> list[dict[str, Any]]:
        """Get all active Advent of Code configurations.

        Returns:
            List of active configuration dictionaries
        """
        try:
            response = await self._api_client.get("/advent-of-code/active-configs")
            data = response.json()
            return data.get("configs", [])
        except Exception as e:
            logger.error(f"Failed to get active AoC configs: {e}")
            return []

    async def _get_posted_thread(
        self,
        guild_id: str,
        year: int,
        day: int
    ) -> dict[str, Any] | None:
        """Check if a thread has been posted for a specific day.

        Args:
            guild_id: Discord guild ID
            year: AoC year
            day: AoC day

        Returns:
            Thread data if exists, None otherwise
        """
        try:
            response = await self._api_client.get(
                f"/advent-of-code/{guild_id}/threads/{year}/{day}"
            )
            if response.status_code == 404:
                return None
            data = response.json()
            return data.get("thread")
        except Exception as e:
            logger.error(f"Failed to check posted thread: {e}")
            return None

    async def _record_posted_thread(
        self,
        guild_id: str,
        year: int,
        day: int,
        thread_id: str,
        thread_title: str
    ) -> None:
        """Record that a thread has been posted.

        Args:
            guild_id: Discord guild ID
            year: AoC year
            day: AoC day
            thread_id: Created thread ID
            thread_title: Thread title
        """
        try:
            await self._api_client.post(
                f"/advent-of-code/{guild_id}/threads",
                json_data={
                    "year": year,
                    "day": day,
                    "thread_id": thread_id,
                    "thread_title": thread_title
                }
            )
            logger.info(f"Recorded AoC thread for guild {guild_id}, year {year}, day {day}")
        except Exception as e:
            logger.error(f"Failed to record posted thread: {e}")
            # Don't raise - thread was created, just recording failed
