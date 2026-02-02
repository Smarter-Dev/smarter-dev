"""Watch manager for multi-agent mention pipeline.

This module manages the lifecycle of watchers:
- ChannelWatchState: Manages all watchers for a single channel
- WatchManager: Global singleton managing all channels
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from smarter_dev.bot.agents.watcher import UpdateFrequency, Watcher, WatcherContext

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Maximum messages to queue per channel
MAX_PENDING_MESSAGES = 50

# Maximum age of messages to keep (10 minutes)
MAX_MESSAGE_AGE_SECONDS = 600


class ChannelWatchState:
    """Manages all watchers for a single channel."""

    def __init__(self, channel_id: int):
        """Initialize channel watch state.

        Args:
            channel_id: Discord channel ID
        """
        self.channel_id = channel_id
        self.watchers: dict[str, Watcher] = {}
        self.pending_messages: asyncio.Queue = asyncio.Queue()
        self.consumed_message_ids: set[str] = set()
        self.watchers_lock: asyncio.Lock = asyncio.Lock()

    async def add_watcher(self, watcher: Watcher) -> None:
        """Add a watcher to this channel.

        Args:
            watcher: The watcher to add
        """
        async with self.watchers_lock:
            self.watchers[watcher.id] = watcher
            logger.info(
                f"Channel {self.channel_id}: Added watcher {watcher.id} "
                f"watching for '{watcher.context.watching_for[:50]}...'"
            )

    async def remove_watcher(self, watcher_id: str) -> Watcher | None:
        """Remove a watcher from this channel.

        Args:
            watcher_id: The watcher ID to remove

        Returns:
            The removed watcher, or None if not found
        """
        async with self.watchers_lock:
            watcher = self.watchers.pop(watcher_id, None)
            if watcher:
                logger.info(f"Channel {self.channel_id}: Removed watcher {watcher_id}")
            return watcher

    async def get_watcher(self, watcher_id: str) -> Watcher | None:
        """Get a watcher by ID.

        Args:
            watcher_id: The watcher ID to find

        Returns:
            The watcher, or None if not found
        """
        async with self.watchers_lock:
            return self.watchers.get(watcher_id)

    async def get_all_watchers(self) -> list[Watcher]:
        """Get all watchers for this channel.

        Returns:
            List of all active watchers
        """
        async with self.watchers_lock:
            return list(self.watchers.values())

    async def has_active_watchers(self) -> bool:
        """Check if there are any active watchers.

        Returns:
            True if there are active watchers
        """
        async with self.watchers_lock:
            return len(self.watchers) > 0

    async def queue_message(self, message: dict) -> None:
        """Queue a message for watcher evaluation.

        Args:
            message: Message dict with id, author_id, content, timestamp
        """
        # Check if message is already consumed
        msg_id = message.get("id")
        if msg_id and msg_id in self.consumed_message_ids:
            logger.debug(f"Channel {self.channel_id}: Message {msg_id} already consumed, skipping")
            return

        # Check queue size
        if self.pending_messages.qsize() >= MAX_PENDING_MESSAGES:
            # Drop oldest message
            try:
                self.pending_messages.get_nowait()
                logger.debug(f"Channel {self.channel_id}: Dropped oldest message from full queue")
            except asyncio.QueueEmpty:
                pass

        await self.pending_messages.put(message)
        logger.debug(
            f"Channel {self.channel_id}: Queued message {msg_id} "
            f"(queue size: {self.pending_messages.qsize()})"
        )

    async def get_pending_messages(self) -> list[dict]:
        """Get all pending messages and clear the queue.

        Returns:
            List of pending messages
        """
        messages = []
        now = datetime.now(UTC)

        while not self.pending_messages.empty():
            try:
                msg = self.pending_messages.get_nowait()

                # Check message age
                timestamp = msg.get("timestamp")
                if timestamp:
                    if isinstance(timestamp, datetime):
                        age = (now - timestamp).total_seconds()
                    else:
                        # Try to parse timestamp
                        try:
                            msg_time = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                            age = (now - msg_time).total_seconds()
                        except (ValueError, TypeError):
                            age = 0

                    if age > MAX_MESSAGE_AGE_SECONDS:
                        logger.debug(
                            f"Channel {self.channel_id}: Dropping message {msg.get('id')} "
                            f"(age: {age:.0f}s)"
                        )
                        continue

                messages.append(msg)
            except asyncio.QueueEmpty:
                break

        return messages

    def mark_message_consumed(self, message_id: str) -> None:
        """Mark a message as consumed to prevent duplicate responses.

        Args:
            message_id: The message ID to mark
        """
        self.consumed_message_ids.add(message_id)
        logger.debug(f"Channel {self.channel_id}: Marked message {message_id} as consumed")

        # Cleanup old consumed IDs (keep last 1000)
        if len(self.consumed_message_ids) > 1000:
            # Convert to list, sort by ID (older IDs are smaller), keep newest 500
            sorted_ids = sorted(self.consumed_message_ids, key=lambda x: int(x) if x.isdigit() else 0)
            self.consumed_message_ids = set(sorted_ids[-500:])

    async def cleanup_expired_watchers(self) -> list[Watcher]:
        """Remove and return expired watchers.

        Returns:
            List of watchers that were removed due to expiration
        """
        expired = []
        async with self.watchers_lock:
            for watcher_id, watcher in list(self.watchers.items()):
                if watcher.is_expired():
                    del self.watchers[watcher_id]
                    expired.append(watcher)
                    logger.info(f"Channel {self.channel_id}: Watcher {watcher_id} expired")

        return expired


class WatchManager:
    """Global singleton managing all channel watch states."""

    _instance: "WatchManager | None" = None

    def __init__(self):
        """Initialize the watch manager."""
        self.channels: dict[int, ChannelWatchState] = {}
        self._lock = asyncio.Lock()
        logger.info("WatchManager initialized")

    @classmethod
    def get_instance(cls) -> "WatchManager":
        """Get or create the global watch manager instance.

        Returns:
            The global WatchManager instance
        """
        if cls._instance is None:
            cls._instance = WatchManager()
        return cls._instance

    async def get_or_create_channel(self, channel_id: int) -> ChannelWatchState:
        """Get or create a channel watch state.

        Args:
            channel_id: Discord channel ID

        Returns:
            ChannelWatchState for the channel
        """
        async with self._lock:
            if channel_id not in self.channels:
                self.channels[channel_id] = ChannelWatchState(channel_id)
                logger.debug(f"Created channel watch state for {channel_id}")
            return self.channels[channel_id]

    async def has_active_watchers(self, channel_id: int) -> bool:
        """Check if a channel has active watchers.

        Args:
            channel_id: Discord channel ID

        Returns:
            True if the channel has active watchers
        """
        async with self._lock:
            if channel_id not in self.channels:
                return False
            return await self.channels[channel_id].has_active_watchers()

    async def create_watcher(
        self,
        channel_id: int,
        guild_id: int,
        context: WatcherContext,
        wait_duration: int = 60,
        update_frequency: UpdateFrequency = UpdateFrequency.ONE_MINUTE
    ) -> Watcher:
        """Create and register a new watcher.

        Args:
            channel_id: Discord channel ID
            guild_id: Discord guild ID
            context: Watcher context with trigger information
            wait_duration: How long to wait before expiring (30-300 seconds)
            update_frequency: How often to evaluate new messages

        Returns:
            The created watcher
        """
        # Clamp wait_duration
        wait_duration = max(30, min(300, wait_duration))

        now = datetime.now(UTC)
        watcher = Watcher(
            id=str(uuid.uuid4()),
            channel_id=channel_id,
            guild_id=guild_id,
            context=context,
            wait_duration=wait_duration,
            update_frequency=update_frequency,
            created_at=now,
            expires_at=now + timedelta(seconds=wait_duration)
        )

        channel_state = await self.get_or_create_channel(channel_id)
        await channel_state.add_watcher(watcher)

        return watcher

    async def remove_watcher(self, channel_id: int, watcher_id: str) -> Watcher | None:
        """Remove a watcher.

        Args:
            channel_id: Discord channel ID
            watcher_id: Watcher ID to remove

        Returns:
            The removed watcher, or None if not found
        """
        async with self._lock:
            if channel_id not in self.channels:
                return None
            return await self.channels[channel_id].remove_watcher(watcher_id)

    async def cleanup_stale_watchers(self) -> dict[int, list[Watcher]]:
        """Clean up expired watchers across all channels.

        Returns:
            Dict mapping channel IDs to lists of expired watchers
        """
        expired_by_channel: dict[int, list[Watcher]] = {}

        async with self._lock:
            for channel_id, channel_state in list(self.channels.items()):
                expired = await channel_state.cleanup_expired_watchers()
                if expired:
                    expired_by_channel[channel_id] = expired

                # Remove channel state if no watchers remain
                if not await channel_state.has_active_watchers():
                    del self.channels[channel_id]
                    logger.debug(f"Removed empty channel watch state for {channel_id}")

        return expired_by_channel

    async def get_channels_with_watchers(self) -> list[int]:
        """Get list of channel IDs that have active watchers.

        Returns:
            List of channel IDs
        """
        async with self._lock:
            return list(self.channels.keys())


# Global instance getter
def get_watch_manager() -> WatchManager:
    """Get the global watch manager instance.

    Returns:
        The global WatchManager instance
    """
    return WatchManager.get_instance()
