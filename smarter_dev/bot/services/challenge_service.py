"""Challenge announcement service for Discord bot.

This service handles the announcement of challenges to Discord channels
when they are released according to campaign schedules.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any

import hikari

from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.cache_manager import CacheManager
from smarter_dev.bot.services.exceptions import ServiceError
from smarter_dev.bot.services.models import ServiceHealth

logger = logging.getLogger(__name__)


class ChallengeService(BaseService):
    """Service for managing challenge announcements and release scheduling."""
    
    def __init__(self, api_client: APIClient, cache_manager: Optional[CacheManager], bot: hikari.BotApp):
        """Initialize the challenge service.
        
        Args:
            api_client: HTTP API client for web service communication
            cache_manager: Cache manager for caching operations (optional)
            bot: Discord bot instance for sending messages
        """
        super().__init__(api_client, cache_manager)
        self._bot = bot
        self._announcement_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def initialize(self) -> None:
        """Initialize the challenge service and start the announcement scheduler."""
        await super().initialize()
        await self.start_announcement_scheduler()
        logger.info("Challenge service initialized with announcement scheduler")
    
    async def cleanup(self) -> None:
        """Clean up resources and stop the announcement scheduler."""
        await self.stop_announcement_scheduler()
        await super().cleanup()
        logger.info("Challenge service cleaned up")
    
    async def health_check(self) -> ServiceHealth:
        """Check the health of the challenge service.
        
        Returns:
            ServiceHealth object with status and details
        """
        try:
            # Check if the scheduler is running
            scheduler_status = "running" if self._running and self._announcement_task else "stopped"
            
            return ServiceHealth(
                service_name="ChallengeService",
                is_healthy=True,
                details={
                    "scheduler_status": scheduler_status,
                    "bot_connected": self._bot.is_alive if hasattr(self._bot, 'is_alive') else True
                }
            )
        except Exception as e:
            logger.error(f"Challenge service health check failed: {e}")
            return ServiceHealth(
                service_name="ChallengeService",
                is_healthy=False,
                details={"error": str(e)}
            )
    
    async def start_announcement_scheduler(self) -> None:
        """Start the background task for checking and announcing challenges."""
        if self._running:
            return
        
        self._running = True
        self._announcement_task = asyncio.create_task(self._announcement_loop())
        logger.info("Started challenge announcement scheduler")
    
    async def stop_announcement_scheduler(self) -> None:
        """Stop the background announcement scheduler."""
        self._running = False
        
        if self._announcement_task:
            self._announcement_task.cancel()
            try:
                await self._announcement_task
            except asyncio.CancelledError:
                pass
            self._announcement_task = None
        
        logger.info("Stopped challenge announcement scheduler")
    
    async def _announcement_loop(self) -> None:
        """Main loop for checking and announcing challenges."""
        while self._running:
            try:
                await self._check_and_announce_challenges()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in challenge announcement loop: {e}")
            
            # Wait 1 minute before checking again  
            try:
                await asyncio.sleep(60)  # 1 minute
            except asyncio.CancelledError:
                break
    
    async def _check_and_announce_challenges(self) -> None:
        """Check for challenges that need to be announced and announce them."""
        try:
            # Get all active campaigns with pending challenges
            pending_challenges = await self._get_pending_announcements()
            
            if not pending_challenges:
                logger.debug("No challenges pending announcement")
                return
            
            logger.info(f"Found {len(pending_challenges)} challenges pending announcement")
            
            for challenge_data in pending_challenges:
                try:
                    await self._announce_challenge(challenge_data)
                except Exception as e:
                    logger.error(f"Failed to announce challenge {challenge_data.get('id', 'unknown')}: {e}")
            
        except Exception as e:
            logger.error(f"Error checking for pending announcements: {e}")
    
    async def _get_pending_announcements(self) -> List[Dict[str, Any]]:
        """Get challenges that should be announced but haven't been yet.
        
        Returns:
            List of challenge data dictionaries
        """
        try:
            response = await self._api_client.get("/challenges/pending-announcements")
            data = response.json()
            return data.get("challenges", [])
        except Exception as e:
            logger.error(f"Failed to get pending announcements: {e}")
            return []
    
    async def _announce_challenge(self, challenge_data: Dict[str, Any]) -> None:
        """Announce a challenge to its campaign's Discord channels.
        
        Args:
            challenge_data: Challenge data from the API
        """
        challenge_id = challenge_data.get("id")
        title = challenge_data.get("title", "New Challenge")
        description = challenge_data.get("description", "")
        guild_id = challenge_data.get("guild_id")
        announcement_channels = challenge_data.get("announcement_channels", [])
        
        if not guild_id or not announcement_channels:
            logger.warning(f"Challenge {challenge_id} missing guild_id or announcement_channels")
            return
        
        logger.info(f"Announcing challenge '{title}' to {len(announcement_channels)} channels in guild {guild_id}")
        
        # Create the announcement message
        announcement_text = self._format_challenge_announcement(title, description)
        
        # Send announcement to each channel
        successful_announcements = 0
        for channel_id in announcement_channels:
            try:
                await self._send_challenge_message(channel_id, announcement_text, challenge_id)
                successful_announcements += 1
                logger.info(f"Announced challenge '{title}' to channel {channel_id}")
            except Exception as e:
                logger.error(f"Failed to announce challenge '{title}' to channel {channel_id}: {e}")
        
        if successful_announcements > 0:
            # Mark the challenge as announced and released in the database
            try:
                await self._mark_challenge_announced(challenge_id)
                await self._mark_challenge_released(challenge_id)
                logger.info(f"Marked challenge '{title}' as announced and released ({successful_announcements}/{len(announcement_channels)} channels)")
            except Exception as e:
                logger.error(f"Failed to mark challenge {challenge_id} as announced/released: {e}")
    
    def _format_challenge_announcement(self, title: str, description: str) -> str:
        """Format the challenge announcement message.
        
        Args:
            title: Challenge title
            description: Challenge description
            
        Returns:
            Formatted announcement text
        """
        # Create announcement with @here mention and h1 markdown header
        announcement = f"@here\n\n# {title}\n{description}"
        
        # Limit message length to Discord's limit (2000 characters)
        if len(announcement) > 2000:
            # Truncate description if needed
            max_desc_length = 2000 - len(f"@here\n\n# {title}\n") - 3  # 3 for "..."
            truncated_desc = description[:max_desc_length] + "..."
            announcement = f"@here\n\n# {title}\n{truncated_desc}"
        
        return announcement
    
    async def _send_challenge_message(self, channel_id: str, message: str, challenge_id: str) -> None:
        """Send a message to a Discord channel.
        
        Args:
            channel_id: Discord channel ID
            message: Message text to send
            challenge_id: Challenge UUID for button interactions
        """
        try:
            channel_id_int = int(channel_id)
            
            # Create buttons using the correct Hikari API with challenge ID in custom_id
            get_input_button = hikari.impl.InteractiveButtonBuilder(
                style=hikari.ButtonStyle.PRIMARY,
                custom_id=f"get_input:{challenge_id}",
                emoji="📥",
                label="Get Input"
            )
            
            submit_solution_button = hikari.impl.InteractiveButtonBuilder(
                style=hikari.ButtonStyle.SUCCESS,
                custom_id=f"submit_solution:{challenge_id}",
                emoji="📤",
                label="Submit Solution"
            )
            
            # Create action row and add buttons
            action_row = hikari.impl.MessageActionRowBuilder()
            action_row.add_component(get_input_button)
            action_row.add_component(submit_solution_button)
            
            # Send message using the bot's REST API with buttons and allowed mentions
            sent_message = await self._bot.rest.create_message(
                channel_id_int,
                content=message,
                components=[action_row],
                mentions_everyone=True
            )
            
            # Pin the message to the channel
            try:
                await self._bot.rest.pin_message(channel_id_int, sent_message.id)
                logger.info(f"Pinned challenge announcement message {sent_message.id} in channel {channel_id}")
            except hikari.ForbiddenError:
                logger.warning(f"No permission to pin message in channel {channel_id}")
            except Exception as pin_error:
                logger.error(f"Failed to pin message in channel {channel_id}: {pin_error}")
            
        except ValueError as e:
            logger.error(f"Invalid channel ID format: {channel_id}")
            raise ServiceError(f"Invalid channel ID: {channel_id}") from e
        except hikari.NotFoundError as e:
            logger.error(f"Channel not found: {channel_id}")
            raise ServiceError(f"Channel not found: {channel_id}") from e
        except hikari.ForbiddenError as e:
            logger.error(f"No permission to send message to channel: {channel_id}")
            raise ServiceError(f"No permission to send message to channel: {channel_id}") from e
        except Exception as e:
            logger.error(f"Failed to send message to channel {channel_id}: {e}")
            raise ServiceError(f"Failed to send message to channel {channel_id}: {str(e)}") from e
    
    async def _mark_challenge_announced(self, challenge_id: str) -> None:
        """Mark a challenge as announced in the database.
        
        Args:
            challenge_id: Challenge UUID
        """
        try:
            await self._api_client.post(f"/challenges/{challenge_id}/mark-announced")
        except Exception as e:
            logger.error(f"Failed to mark challenge {challenge_id} as announced: {e}")
            raise ServiceError(f"Failed to mark challenge as announced: {str(e)}") from e
    
    async def _mark_challenge_released(self, challenge_id: str) -> None:
        """Mark a challenge as released in the database.
        
        Args:
            challenge_id: Challenge UUID
        """
        try:
            await self._api_client.post(f"/challenges/{challenge_id}/mark-released")
        except Exception as e:
            logger.error(f"Failed to mark challenge {challenge_id} as released: {e}")
            raise ServiceError(f"Failed to mark challenge as released: {str(e)}") from e
    
    async def announce_challenge_now(self, challenge_id: str) -> None:
        """Manually announce a specific challenge immediately.
        
        Args:
            challenge_id: Challenge UUID to announce
        """
        try:
            # Get challenge data
            response = await self._api_client.get(f"/challenges/{challenge_id}")
            data = response.json()
            challenge_data = data.get("challenge")
            
            if not challenge_data:
                raise ServiceError(f"Challenge {challenge_id} not found")
            
            await self._announce_challenge(challenge_data)
            logger.info(f"Manually announced challenge {challenge_id}")
            
        except Exception as e:
            logger.error(f"Failed to manually announce challenge {challenge_id}: {e}")
            raise ServiceError(f"Failed to manually announce challenge: {str(e)}") from e