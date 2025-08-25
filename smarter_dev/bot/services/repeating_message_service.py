"""Repeating message service for Discord bot.

This service handles repeating messages for guild channels, sending messages
at specified intervals with optional role mentions. Operates independently
from the campaign system for simpler, more focused functionality.
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


class RepeatingMessageService(BaseService):
    """Service for managing repeating message sending."""
    
    def __init__(self, api_client: APIClient, cache_manager: Optional[CacheManager], bot: hikari.BotApp):
        """Initialize the repeating message service.
        
        Args:
            api_client: HTTP API client for web service communication
            cache_manager: Cache manager for caching operations (optional)
            bot: Discord bot instance for sending messages
        """
        super().__init__(api_client, cache_manager)
        self._bot = bot
        self._message_task: Optional[asyncio.Task] = None
        self._running = False
        self._processing_messages: set = set()  # Track messages currently being processed
    
    async def initialize(self) -> None:
        """Initialize the repeating message service and start the scheduler."""
        await super().initialize()
        await self.start_message_scheduler()
        logger.info("Repeating message service initialized with message scheduler")
    
    async def cleanup(self) -> None:
        """Clean up resources and stop the message scheduler."""
        await self.stop_message_scheduler()
        await super().cleanup()
        logger.info("Repeating message service cleaned up")
    
    async def health_check(self) -> ServiceHealth:
        """Check the health of the repeating message service.
        
        Returns:
            ServiceHealth object with status and details
        """
        try:
            # Check if the scheduler is running
            scheduler_status = "running" if self._running and self._message_task else "stopped"
            
            return ServiceHealth(
                service_name="RepeatingMessageService",
                is_healthy=True,
                details={
                    "scheduler_status": scheduler_status,
                    "bot_connected": self._bot.is_alive if hasattr(self._bot, 'is_alive') else True,
                    "processing_messages": len(self._processing_messages)
                }
            )
        except Exception as e:
            logger.error(f"Repeating message service health check failed: {e}")
            return ServiceHealth(
                service_name="RepeatingMessageService",
                is_healthy=False,
                details={"error": str(e)}
            )
    
    async def start_message_scheduler(self) -> None:
        """Start the background task for checking and sending repeating messages."""
        if self._running:
            return
        
        self._running = True
        self._message_task = asyncio.create_task(self._message_loop())
        logger.info("Started repeating message scheduler")
    
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
        
        logger.info("Stopped repeating message scheduler")
    
    async def _message_loop(self) -> None:
        """Main loop for checking and sending repeating messages."""
        while self._running:
            try:
                # Check for due messages
                await self._check_and_send_due_messages()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in repeating message loop: {e}")
            
            # Wait until the next minute boundary
            try:
                await self._wait_until_next_minute()
            except asyncio.CancelledError:
                break
    
    async def _wait_until_next_minute(self) -> None:
        """Wait until the next minute boundary (xx:xx:00)."""
        now = datetime.now(timezone.utc)
        # Calculate seconds until next minute
        seconds_until_next_minute = 60 - now.second - (now.microsecond / 1_000_000)
        
        # Add a small buffer (100ms) to ensure we're past the minute boundary
        wait_time = seconds_until_next_minute + 0.1
        
        logger.debug(f"Waiting {wait_time:.2f} seconds until next minute boundary")
        await asyncio.sleep(wait_time)
    
    async def _check_and_send_due_messages(self) -> None:
        """Check for due repeating messages and send them."""
        try:
            now = datetime.now(timezone.utc)
            logger.debug(f"Checking for due messages at {now}")
            
            # Get messages that are due to be sent
            due_messages = await self._get_due_repeating_messages()
            
            if not due_messages:
                logger.debug(f"No due repeating messages found at {now}")
                return
            
            logger.info(f"Found {len(due_messages)} due repeating messages at {now}")
            
            # Process only the most recent due message per repeating message series
            # If multiple messages are due (catch-up scenario), only send the latest one
            processed_message_series = set()
            
            for message_data in due_messages:
                message_id = message_data.get("id")
                next_send_time = message_data.get("next_send_time")
                
                # Skip if this message series is already being processed
                if message_id in processed_message_series or message_id in self._processing_messages:
                    logger.warning(f"Message {message_id} already processed or processing, skipping")
                    continue
                
                logger.info(f"Processing due message {message_id}: next_send_time={next_send_time}, current_time={now}")
                
                self._processing_messages.add(message_id)
                processed_message_series.add(message_id)
                
                # Process message synchronously to avoid race conditions
                await self._process_repeating_message(message_data)
            
        except Exception as e:
            logger.error(f"Error checking for due repeating messages: {e}")
    
    async def _process_repeating_message(self, message_data: Dict[str, Any]) -> None:
        """Process a single repeating message."""
        message_id = message_data.get("id")
        try:
            channel_id = message_data.get("channel_id")
            message_content = message_data.get("message_content", "")
            guild_id = message_data.get("guild_id")
            
            if not channel_id or not message_content:
                logger.warning(f"Repeating message {message_id} missing required fields")
                return
            
            logger.info(f"Processing repeating message {message_id} for channel {channel_id}")
            
            # Send the message
            success = await self._send_message_with_retry(channel_id, message_content, message_id)
            
            if success:
                # Mark the message as sent and update next send time
                await self._mark_repeating_message_sent(message_id)
                logger.info(f"Successfully sent repeating message {message_id}")
            else:
                logger.error(f"Failed to send repeating message {message_id}")
            
        except Exception as e:
            logger.error(f"Failed to process repeating message {message_id}: {e}")
        finally:
            # Remove from processing set
            if message_id in self._processing_messages:
                self._processing_messages.remove(message_id)
    
    async def _get_due_repeating_messages(self) -> List[Dict[str, Any]]:
        """Get repeating messages that are due to be sent.
        
        Returns:
            List of repeating message data dictionaries
        """
        try:
            response = await self._api_client.get("/repeating-messages/due")
            data = response.json()
            return data.get("repeating_messages", [])
        except Exception as e:
            logger.error(f"Failed to get due repeating messages: {e}")
            return []
    
    async def _send_message_with_retry(self, channel_id: str, message_content: str, message_id: str, max_retries: int = 3) -> bool:
        """Send a message to a Discord channel with retry logic.
        
        Args:
            channel_id: Discord channel ID
            message_content: Message text to send (already formatted with role mentions)
            message_id: Message ID for logging
            max_retries: Maximum number of retry attempts
            
        Returns:
            True if message was sent successfully, False otherwise
        """
        for attempt in range(max_retries + 1):
            try:
                await self._send_message_to_channel(channel_id, message_content)
                logger.info(f"Successfully sent repeating message {message_id} to channel {channel_id}")
                return True
            except ServiceError as e:
                if "Channel not found" in str(e) or "Invalid channel ID" in str(e):
                    logger.error(f"Channel {channel_id} is invalid or not found, skipping message {message_id}")
                    return False
                elif "No permission" in str(e):
                    logger.error(f"No permission to send to channel {channel_id}, skipping message {message_id}")
                    return False
                else:
                    if attempt < max_retries:
                        wait_time = (2 ** attempt) * 2  # Exponential backoff: 2s, 4s, 8s
                        logger.warning(f"Failed to send message {message_id} to channel {channel_id}, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"Failed to send repeating message {message_id} to channel {channel_id} after {max_retries} retries: {e}")
                        return False
            except Exception as e:
                if attempt < max_retries:
                    wait_time = (2 ** attempt) * 2
                    logger.warning(f"Unexpected error sending message {message_id} to channel {channel_id}, retrying in {wait_time}s: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Failed to send repeating message {message_id} to channel {channel_id} after {max_retries} retries: {e}")
                    return False
        return False
    
    async def _send_message_to_channel(self, channel_id: str, message_content: str) -> None:
        """Send a message to a Discord channel.
        
        Args:
            channel_id: Discord channel ID
            message_content: Message text to send (formatted with role mentions)
        """
        try:
            channel_id_int = int(channel_id)
            
            # Send message with role mentions enabled
            await self._bot.rest.create_message(
                channel_id_int,
                content=message_content,
                role_mentions=True  # Allow role mentions to ping users
            )
            
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
    
    async def _mark_repeating_message_sent(self, message_id: str) -> None:
        """Mark a repeating message as sent and update next send time.
        
        Args:
            message_id: Repeating message UUID
        """
        try:
            await self._api_client.post(f"/repeating-messages/{message_id}/mark-sent")
        except Exception as e:
            logger.error(f"Failed to mark repeating message as sent for message {message_id}: {e}")
            raise ServiceError(f"Failed to mark repeating message as sent: {str(e)}") from e