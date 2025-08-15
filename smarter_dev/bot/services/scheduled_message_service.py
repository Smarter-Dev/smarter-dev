"""Scheduled message service for Discord bot.

This service handles scheduled messages for campaigns, sending messages
at specified times to announcement channels without buttons.
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


class ScheduledMessageService(BaseService):
    """Service for managing scheduled message announcements."""
    
    def __init__(self, api_client: APIClient, cache_manager: Optional[CacheManager], bot: hikari.BotApp):
        """Initialize the scheduled message service.
        
        Args:
            api_client: HTTP API client for web service communication
            cache_manager: Cache manager for caching operations (optional)
            bot: Discord bot instance for sending messages
        """
        super().__init__(api_client, cache_manager)
        self._bot = bot
        self._message_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def initialize(self) -> None:
        """Initialize the scheduled message service and start the scheduler."""
        await super().initialize()
        await self.start_message_scheduler()
        logger.info("Scheduled message service initialized with message scheduler")
    
    async def cleanup(self) -> None:
        """Clean up resources and stop the message scheduler."""
        await self.stop_message_scheduler()
        await super().cleanup()
        logger.info("Scheduled message service cleaned up")
    
    async def health_check(self) -> ServiceHealth:
        """Check the health of the scheduled message service.
        
        Returns:
            ServiceHealth object with status and details
        """
        try:
            # Check if the scheduler is running
            scheduler_status = "running" if self._running and self._message_task else "stopped"
            
            return ServiceHealth(
                service_name="ScheduledMessageService",
                is_healthy=True,
                details={
                    "scheduler_status": scheduler_status,
                    "bot_connected": self._bot.is_alive if hasattr(self._bot, 'is_alive') else True
                }
            )
        except Exception as e:
            logger.error(f"Scheduled message service health check failed: {e}")
            return ServiceHealth(
                service_name="ScheduledMessageService",
                is_healthy=False,
                details={"error": str(e)}
            )
    
    async def start_message_scheduler(self) -> None:
        """Start the background task for checking and sending scheduled messages."""
        if self._running:
            return
        
        self._running = True
        self._message_task = asyncio.create_task(self._message_loop())
        logger.info("Started scheduled message scheduler")
    
    async def stop_message_scheduler(self) -> None:
        """Stop the background message scheduler."""
        self._running = False
        
        if self._message_task:
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass
            self._message_task = None
        
        logger.info("Stopped scheduled message scheduler")
    
    async def _message_loop(self) -> None:
        """Main loop for checking and sending scheduled messages."""
        while self._running:
            try:
                await self._check_and_send_scheduled_messages()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in scheduled message loop: {e}")
            
            # Wait 1 minute before checking again
            try:
                await asyncio.sleep(60)  # 1 minute
            except asyncio.CancelledError:
                break
    
    async def _check_and_send_scheduled_messages(self) -> None:
        """Check for scheduled messages that need to be sent and send them."""
        try:
            # Get all pending scheduled messages
            pending_messages = await self._get_pending_scheduled_messages()
            
            if not pending_messages:
                logger.debug("No scheduled messages pending")
                return
            
            logger.info(f"Found {len(pending_messages)} scheduled messages pending")
            
            for message_data in pending_messages:
                try:
                    await self._send_scheduled_message(message_data)
                except Exception as e:
                    logger.error(f"Failed to send scheduled message {message_data.get('id', 'unknown')}: {e}")
            
        except Exception as e:
            logger.error(f"Error checking for pending scheduled messages: {e}")
    
    async def _get_pending_scheduled_messages(self) -> List[Dict[str, Any]]:
        """Get scheduled messages that should be sent but haven't been yet.
        
        Returns:
            List of scheduled message data dictionaries
        """
        try:
            response = await self._api_client.get("/scheduled-messages/pending")
            data = response.json()
            return data.get("scheduled_messages", [])
        except Exception as e:
            logger.error(f"Failed to get pending scheduled messages: {e}")
            return []
    
    async def _send_scheduled_message(self, message_data: Dict[str, Any]) -> None:
        """Send a scheduled message to a campaign's announcement channels.
        
        Args:
            message_data: Scheduled message data from the API
        """
        message_id = message_data.get("id")
        title = message_data.get("title", "Scheduled Message")
        description = message_data.get("description", "")
        guild_id = message_data.get("guild_id")
        announcement_channels = message_data.get("announcement_channels", [])
        
        if not guild_id or not announcement_channels:
            logger.warning(f"Scheduled message {message_id} missing guild_id or announcement_channels")
            return
        
        logger.info(f"Sending scheduled message '{title}' to {len(announcement_channels)} channels in guild {guild_id}")
        
        # Create the message content
        message_text = self._format_scheduled_message(title, description)
        
        # Send message to each channel with retry logic
        successful_sends = 0
        failed_channels = []
        for channel_id in announcement_channels:
            success = await self._send_message_with_retry(channel_id, message_text, title)
            if success:
                successful_sends += 1
            else:
                failed_channels.append(channel_id)
        
        # If some channels failed, retry them with longer backoff
        if failed_channels:
            logger.warning(f"Retrying {len(failed_channels)} failed channels with extended backoff")
            await asyncio.sleep(30)  # Wait 30 seconds before retrying failed channels
            for channel_id in failed_channels:
                success = await self._send_message_with_retry(channel_id, message_text, title, max_retries=5)
                if success:
                    successful_sends += 1
        
        if successful_sends > 0:
            # Mark the scheduled message as sent in the database
            try:
                await self._mark_scheduled_message_sent(message_id)
                logger.info(f"Marked scheduled message '{message_id}' as sent ({successful_sends}/{len(announcement_channels)} channels)")
            except Exception as e:
                logger.error(f"Failed to mark scheduled message as sent for message {message_id}: {e}")
        else:
            logger.error(f"Failed to send scheduled message '{title}' to any channels")
    
    async def _send_message_with_retry(self, channel_id: str, message: str, title: str, max_retries: int = 3) -> bool:
        """Send a message to a Discord channel with retry logic.
        
        Args:
            channel_id: Discord channel ID
            message: Message text to send
            title: Message title for logging
            max_retries: Maximum number of retry attempts
            
        Returns:
            True if message was sent successfully, False otherwise
        """
        for attempt in range(max_retries + 1):
            try:
                await self._send_message_to_channel(channel_id, message)
                logger.info(f"Successfully sent scheduled message '{title}' to channel {channel_id}")
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
                        logger.warning(f"Failed to send message to channel {channel_id}, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"Failed to send scheduled message '{title}' to channel {channel_id} after {max_retries} retries: {e}")
                        return False
            except Exception as e:
                if attempt < max_retries:
                    wait_time = (2 ** attempt) * 1.5
                    logger.warning(f"Unexpected error sending to channel {channel_id}, retrying in {wait_time}s: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Failed to send to channel {channel_id} after {max_retries} retries: {e}")
                    return False
        return False
    
    def _format_scheduled_message(self, title: str, description: str) -> str:
        """Format the scheduled message content.
        
        Args:
            title: Message title
            description: Message description
            
        Returns:
            Formatted message text
        """
        # Create a message with h1 markdown header
        if description:
            message = f"# {title}\n{description}"
        else:
            message = f"# {title}"
        
        # Limit message length to Discord's limit (2000 characters)
        if len(message) > 2000:
            # Truncate description if needed
            max_desc_length = 2000 - len(f"# {title}\n") - 3  # 3 for "..."
            truncated_desc = description[:max_desc_length] + "..."
            message = f"# {title}\n{truncated_desc}"
        
        return message
    
    async def _send_message_to_channel(self, channel_id: str, message: str) -> None:
        """Send a message to a Discord channel and pin it.
        
        Args:
            channel_id: Discord channel ID
            message: Message text to send
        """
        try:
            channel_id_int = int(channel_id)
            
            # Send message without components (no buttons)
            sent_message = await self._bot.rest.create_message(
                channel_id_int,
                content=message
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
                logger.info(f"Successfully pinned message {message_id} in channel {channel_id}")
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
                    logger.warning(f"Failed to pin message, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries}): {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Failed to pin message {message_id} after {max_retries} retries: {e}")
                    return
            except Exception as e:
                if attempt < max_retries:
                    wait_time = (2 ** attempt) * 2
                    logger.warning(f"Unexpected error pinning message, retrying in {wait_time}s: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Failed to pin message {message_id} after {max_retries} retries: {e}")
                    return
    
    async def _mark_scheduled_message_sent(self, message_id: str) -> None:
        """Mark a scheduled message as sent in the database.
        
        Args:
            message_id: Scheduled message UUID
        """
        try:
            await self._api_client.post(f"/scheduled-messages/{message_id}/mark-sent")
        except Exception as e:
            logger.error(f"Failed to mark scheduled message as sent for message {message_id}: {e}")
            raise ServiceError(f"Failed to mark scheduled message as sent: {str(e)}") from e