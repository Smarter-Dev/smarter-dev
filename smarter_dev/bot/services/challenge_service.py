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
        self._queued_challenges: set = set()  # Track challenges already queued to prevent duplicates
    
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
                # Check every 30 seconds for upcoming challenges
                await self._check_and_queue_challenges()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in challenge announcement loop: {e}")
            
            # Wait 30 seconds before checking again for more precise timing
            try:
                await asyncio.sleep(30)  # 30 seconds
            except asyncio.CancelledError:
                break
    
    async def _check_and_queue_challenges(self) -> None:
        """Check for challenges and queue them for precise timing."""
        try:
            # Get challenges scheduled in the next 45 seconds
            upcoming_challenges = await self._get_upcoming_announcements()
            
            if not upcoming_challenges:
                logger.debug("No challenges scheduled in the next 45 seconds")
                return
            
            logger.info(f"Found {len(upcoming_challenges)} challenges scheduled in the next 45 seconds")
            
            # Queue each challenge to be announced at exactly the right time
            for challenge_data in upcoming_challenges:
                challenge_id = challenge_data.get("id")
                if challenge_id not in self._queued_challenges:
                    self._queued_challenges.add(challenge_id)
                    asyncio.create_task(self._queue_and_announce_challenge(challenge_data))
                    logger.debug(f"Queued challenge {challenge_id} for announcement")
                else:
                    logger.debug(f"Challenge {challenge_id} already queued, skipping")
            
        except Exception as e:
            logger.error(f"Error checking for upcoming challenges: {e}")
    
    async def _queue_and_announce_challenge(self, challenge_data: Dict[str, Any]) -> None:
        """Queue a challenge to be announced at exactly the scheduled time."""
        challenge_id = None
        try:
            challenge_id = challenge_data.get("id")
            release_time_str = challenge_data.get("release_time")
            title = challenge_data.get("title", "Challenge")
            
            # Parse release time
            release_time = datetime.fromisoformat(release_time_str.replace('Z', '+00:00'))
            current_time = datetime.now(timezone.utc)
            
            # Calculate delay until exact release time
            delay_seconds = (release_time - current_time).total_seconds()
            
            if delay_seconds > 0:
                logger.info(f"Queuing challenge '{title}' to announce in {delay_seconds:.1f} seconds")
                await asyncio.sleep(delay_seconds)
            
            # Announce the challenge at exactly the scheduled time
            await self._announce_challenge(challenge_data)
            
        except Exception as e:
            logger.error(f"Failed to queue and announce challenge {challenge_data.get('id', 'unknown')}: {e}")
        finally:
            # Remove from queued set after processing
            if challenge_id and challenge_id in self._queued_challenges:
                self._queued_challenges.remove(challenge_id)
    
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
    
    async def _get_upcoming_announcements(self) -> List[Dict[str, Any]]:
        """Get challenges that will be announced in the next 45 seconds.
        
        Returns:
            List of challenge data dictionaries
        """
        try:
            # Get challenges scheduled for the next 45 seconds
            response = await self._api_client.get("/challenges/upcoming-announcements?seconds=45")
            data = response.json()
            return data.get("challenges", [])
        except Exception as e:
            logger.error(f"Failed to get upcoming announcements: {e}")
            return []
    
    async def _get_squad_channels(self, guild_id: str) -> Dict[str, Dict[str, Any]]:
        """Get all announcement channels for active squads in a guild with squad info.
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            Dict mapping channel IDs to squad information (including role_id)
        """
        try:
            response = await self._api_client.get(f"/guilds/{guild_id}/squads/")
            data = response.json()
            
            # Extract announcement channels from active squads with their info
            channels = {}
            for squad in data:
                if squad.get("is_active") and squad.get("announcement_channel"):
                    channels[squad["announcement_channel"]] = {
                        "name": squad.get("name"),
                        "role_id": squad.get("role_id")
                    }
            
            if channels:
                logger.debug(f"Found {len(channels)} squad announcement channels for guild {guild_id}")
            
            return channels
        except Exception as e:
            logger.error(f"Failed to get squad channels for guild {guild_id}: {e}")
            return {}
    
    async def _announce_challenge(self, challenge_data: Dict[str, Any]) -> None:
        """Announce a challenge to squad channels only.
        
        Args:
            challenge_data: Challenge data from the API
        """
        challenge_id = challenge_data.get("id")
        title = challenge_data.get("title", "New Challenge")
        description = challenge_data.get("description", "")
        guild_id = challenge_data.get("guild_id")
        
        # Get squad channels for this guild (challenges only go to squad channels)
        squad_channels = await self._get_squad_channels(guild_id)
        
        if not guild_id or not squad_channels:
            logger.warning(f"Challenge {challenge_id} missing guild_id or no squad channels configured")
            return
        
        logger.info(f"Announcing challenge '{title}' to {len(squad_channels)} squad channels in guild {guild_id}")
        
        # Send announcement to each squad channel with retry logic and role mentions
        successful_announcements = 0
        failed_channels = []
        for channel_id, squad_info in squad_channels.items():
            # Format message with squad role mention
            announcement_text = self._format_challenge_announcement(title, description, squad_info.get("role_id"))
            success = await self._send_challenge_with_retry(channel_id, announcement_text, challenge_id, title)
            if success:
                successful_announcements += 1
            else:
                failed_channels.append((channel_id, squad_info))
        
        # If some channels failed, retry them with longer backoff
        if failed_channels:
            logger.warning(f"Retrying {len(failed_channels)} failed channels with extended backoff")
            await asyncio.sleep(30)  # Wait 30 seconds before retrying failed channels
            for channel_id, squad_info in failed_channels:
                announcement_text = self._format_challenge_announcement(title, description, squad_info.get("role_id"))
                success = await self._send_challenge_with_retry(channel_id, announcement_text, challenge_id, title, max_retries=5)
                if success:
                    successful_announcements += 1
        
        if successful_announcements > 0:
            # Mark the challenge as announced and released in the database
            try:
                await self._mark_challenge_announced(challenge_id)
                await self._mark_challenge_released(challenge_id)
                logger.info(f"Marked challenge '{title}' as announced and released ({successful_announcements}/{len(squad_channels)} squad channels)")
            except Exception as e:
                logger.error(f"Failed to mark challenge {challenge_id} as announced/released: {e}")
        else:
            logger.error(f"Failed to announce challenge '{title}' to any channels")
    
    async def _send_challenge_with_retry(self, channel_id: str, message: str, challenge_id: str, title: str, max_retries: int = 3) -> bool:
        """Send a challenge announcement with retry logic.
        
        Args:
            channel_id: Discord channel ID
            message: Message text to send
            challenge_id: Challenge UUID for button interactions
            title: Challenge title for logging
            max_retries: Maximum number of retry attempts
            
        Returns:
            True if message was sent successfully, False otherwise
        """
        for attempt in range(max_retries + 1):
            try:
                await self._send_challenge_message(channel_id, message, challenge_id)
                logger.info(f"Successfully announced challenge '{title}' to channel {channel_id}")
                return True
            except ServiceError as e:
                if "Channel not found" in str(e) or "Invalid channel ID" in str(e):
                    logger.error(f"Channel {channel_id} is invalid or not found, skipping")
                    return False
                elif "No permission" in str(e):
                    logger.error(f"No permission to send to channel {channel_id}, skipping")
                    return False
                else:
                    if attempt < max_retries:
                        wait_time = (2 ** attempt) * 1.5  # Exponential backoff: 1.5s, 3s, 6s
                        logger.warning(f"Failed to announce challenge to channel {channel_id}, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"Failed to announce challenge '{title}' to channel {channel_id} after {max_retries} retries: {e}")
                        return False
            except Exception as e:
                if attempt < max_retries:
                    wait_time = (2 ** attempt) * 1.5
                    logger.warning(f"Unexpected error announcing to channel {channel_id}, retrying in {wait_time}s: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Failed to announce to channel {channel_id} after {max_retries} retries: {e}")
                    return False
        return False
    
    def _format_challenge_announcement(self, title: str, description: str, role_id: Optional[str] = None) -> str:
        """Format the challenge announcement message with squad role mention.
        
        Args:
            title: Challenge title
            description: Challenge description
            role_id: Discord role ID to mention (optional)
            
        Returns:
            Formatted announcement text with role mention
        """
        # Add role mention at the beginning if role_id is provided
        mention = f"<@&{role_id}>\n\n" if role_id else ""
        
        # Create announcement with role mention and h1 markdown header
        announcement = f"{mention}# {title}\n{description}"
        
        # Limit message length to Discord's limit (2000 characters)
        if len(announcement) > 2000:
            # Account for mention length when truncating
            mention_length = len(mention)
            max_desc_length = 2000 - len(f"# {title}\n") - mention_length - 3  # 3 for "..."
            truncated_desc = description[:max_desc_length] + "..."
            announcement = f"{mention}# {title}\n{truncated_desc}"
        
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
            
            # Send message using the bot's REST API with buttons, role mentions allowed
            sent_message = await self._bot.rest.create_message(
                channel_id_int,
                content=message,
                components=[action_row],
                role_mentions=True  # Allow role mentions to ping users
            )
            
            # Pin the message with retry logic
            await self._pin_message_with_retry(channel_id_int, sent_message.id)
            
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
    
    async def _pin_message_with_retry(self, channel_id: int, message_id: int, max_retries: int = 3) -> None:
        """Pin a message with retry logic.
        
        Args:
            channel_id: Discord channel ID
            message_id: Message ID to pin
            max_retries: Maximum number of retry attempts
        """
        for attempt in range(max_retries + 1):
            try:
                await self._bot.rest.pin_message(channel_id, message_id)
                logger.info(f"Successfully pinned challenge message {message_id} in channel {channel_id}")
                return
            except hikari.ForbiddenError:
                logger.warning(f"No permission to pin message in channel {channel_id}")
                return  # No point retrying permission errors
            except hikari.RateLimitTooLongError as e:
                logger.warning(f"Rate limit too long for pinning in channel {channel_id}: {e}")
                return  # Don't retry if rate limit is too long
            except (hikari.RateLimitedError, hikari.InternalServerError) as e:
                if attempt < max_retries:
                    wait_time = (2 ** attempt) * 2  # Exponential backoff: 2s, 4s, 8s
                    logger.warning(f"Failed to pin challenge message, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries}): {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Failed to pin challenge message {message_id} after {max_retries} retries: {e}")
                    return
            except Exception as e:
                if attempt < max_retries:
                    wait_time = (2 ** attempt) * 2
                    logger.warning(f"Unexpected error pinning challenge message, retrying in {wait_time}s: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Failed to pin challenge message {message_id} after {max_retries} retries: {e}")
                    return
    
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