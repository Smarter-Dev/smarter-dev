"""Channel state manager for agentic message monitoring.

This service manages per-channel state for agent-driven conversation monitoring:
- Tracks typing indicator state
- Manages message queue for wait_for_messages tool
- Tracks whether agent wants to continue monitoring
- Prevents concurrent agent executions in the same channel
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import hikari

logger = logging.getLogger(__name__)


class ChannelMonitorState:
    """Manages state for a single channel during agent monitoring."""

    def __init__(self):
        """Initialize channel state."""
        self.agent_running: bool = False  # Is agent currently processing
        self.typing_active: bool = False  # Is typing indicator currently shown
        self.typing_task: Optional[asyncio.Task] = None  # Background typing indicator task
        self.continue_monitoring: bool = False  # Does agent want to continue monitoring
        self.last_message_id_seen: Optional[str] = None  # Checkpoint for fetch_new_messages
        self.message_queue: asyncio.Queue = asyncio.Queue()  # Messages for wait_for_messages
        self.queue_updated_event: asyncio.Event = asyncio.Event()  # Signals new messages
        self.messages_processed: int = 0  # Total messages processed in this conversation session
        self.recent_messages: Dict[str, float] = {}  # Message content hash -> timestamp for deduplication

    def _hash_message(self, content: str) -> str:
        """Generate a hash for message content.

        Args:
            content: The message content to hash

        Returns:
            SHA256 hash of the content
        """
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def _cleanup_old_messages(self) -> None:
        """Remove message hashes older than 60 seconds from the recent messages tracker."""
        current_time = time.time()
        expired_hashes = [
            msg_hash for msg_hash, timestamp in self.recent_messages.items()
            if current_time - timestamp > 60
        ]
        for msg_hash in expired_hashes:
            del self.recent_messages[msg_hash]
        if expired_hashes:
            logger.debug(f"Cleaned up {len(expired_hashes)} expired message hashes")

    def is_duplicate_message(self, content: str) -> bool:
        """Check if a message was recently sent (within the last 60 seconds).

        Args:
            content: The message content to check

        Returns:
            True if this message was sent within the last 60 seconds
        """
        self._cleanup_old_messages()
        msg_hash = self._hash_message(content)
        return msg_hash in self.recent_messages

    def add_recent_message(self, content: str) -> None:
        """Add a message to the recent messages tracker.

        Args:
            content: The message content that was sent
        """
        self._cleanup_old_messages()
        msg_hash = self._hash_message(content)
        self.recent_messages[msg_hash] = time.time()
        logger.debug(f"Tracked message hash {msg_hash[:8]}... (total tracked: {len(self.recent_messages)})")


class ChannelStateManager:
    """Manages per-channel state for agentic conversation monitoring."""

    def __init__(self):
        """Initialize the channel state manager."""
        self.states: Dict[int, ChannelMonitorState] = {}
        logger.info("ChannelStateManager initialized for agentic monitoring")

    def get_state(self, channel_id: int) -> ChannelMonitorState:
        """Get or create state for a channel.

        Args:
            channel_id: Discord channel ID

        Returns:
            ChannelMonitorState for the channel
        """
        if channel_id not in self.states:
            self.states[channel_id] = ChannelMonitorState()
        return self.states[channel_id]

    def start_agent(self, channel_id: int) -> bool:
        """Mark agent as running in a channel.

        Args:
            channel_id: Discord channel ID

        Returns:
            True if successfully marked as running, False if already running
        """
        state = self.get_state(channel_id)
        if state.agent_running:
            logger.debug(f"Channel {channel_id}: Agent already running, skipping")
            return False

        state.agent_running = True
        state.continue_monitoring = False  # Reset for new invocation
        logger.debug(f"Channel {channel_id}: Agent started")
        return True

    def finish_agent(self, channel_id: int) -> None:
        """Mark agent as finished.

        Args:
            channel_id: Discord channel ID
        """
        state = self.get_state(channel_id)
        state.agent_running = False
        logger.debug(f"Channel {channel_id}: Agent finished")

    def is_agent_running(self, channel_id: int) -> bool:
        """Check if agent is currently running in a channel.

        Args:
            channel_id: Discord channel ID

        Returns:
            True if agent is actively processing
        """
        state = self.get_state(channel_id)
        return state.agent_running

    def set_continue_monitoring(self, channel_id: int, continue_monitoring: bool) -> None:
        """Set whether agent wants to continue monitoring.

        Args:
            channel_id: Discord channel ID
            continue_monitoring: True if agent wants to continue
        """
        state = self.get_state(channel_id)
        state.continue_monitoring = continue_monitoring
        logger.debug(f"Channel {channel_id}: Continue monitoring set to {continue_monitoring}")

    def should_continue_monitoring(self, channel_id: int) -> bool:
        """Check if agent wants to continue monitoring.

        Args:
            channel_id: Discord channel ID

        Returns:
            True if agent wants to continue monitoring
        """
        state = self.get_state(channel_id)
        return state.continue_monitoring

    def set_typing_active(self, channel_id: int, active: bool) -> None:
        """Set typing indicator state.

        Args:
            channel_id: Discord channel ID
            active: True if typing indicator should be active
        """
        state = self.get_state(channel_id)
        state.typing_active = active
        logger.debug(f"Channel {channel_id}: Typing indicator {'started' if active else 'stopped'}")

    def is_typing_active(self, channel_id: int) -> bool:
        """Check if typing indicator is currently active.

        Args:
            channel_id: Discord channel ID

        Returns:
            True if typing indicator is active
        """
        state = self.get_state(channel_id)
        return state.typing_active

    async def start_typing_task(
        self,
        channel_id: int,
        bot_rest_callback
    ) -> None:
        """Start a background typing indicator task.

        Args:
            channel_id: Discord channel ID
            bot_rest_callback: Async callback to call trigger_typing (bot.rest.trigger_typing)
        """
        state = self.get_state(channel_id)

        # Cancel any existing typing task
        if state.typing_task and not state.typing_task.done():
            state.typing_task.cancel()

        # Create new typing task
        async def typing_loop():
            """Keep typing indicator active by retriggering every 9 seconds."""
            try:
                while state.typing_active:
                    await bot_rest_callback(channel_id)
                    await asyncio.sleep(9)  # Trigger before 10-second expiry
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error in typing loop for channel {channel_id}: {e}")

        state.typing_task = asyncio.create_task(typing_loop())
        logger.debug(f"Channel {channel_id}: Started typing indicator task")

    def stop_typing_task(self, channel_id: int) -> None:
        """Stop the background typing indicator task.

        Args:
            channel_id: Discord channel ID
        """
        state = self.get_state(channel_id)

        if state.typing_task and not state.typing_task.done():
            state.typing_task.cancel()
            logger.debug(f"Channel {channel_id}: Cancelled typing indicator task")

        state.typing_task = None

    def set_last_message_id(self, channel_id: int, message_id: str) -> None:
        """Update the last message ID checkpoint.

        Args:
            channel_id: Discord channel ID
            message_id: The message ID to use as checkpoint
        """
        state = self.get_state(channel_id)
        state.last_message_id_seen = message_id
        logger.debug(f"Channel {channel_id}: Updated last message checkpoint to {message_id}")

    def get_last_message_id(self, channel_id: int) -> Optional[str]:
        """Get the last message ID checkpoint.

        Args:
            channel_id: Discord channel ID

        Returns:
            The last message ID seen, or None if not set
        """
        state = self.get_state(channel_id)
        return state.last_message_id_seen

    async def queue_message(self, channel_id: int, message: dict) -> None:
        """Add a message to the channel's message queue.

        Args:
            channel_id: Discord channel ID
            message: Message dict to queue
        """
        state = self.get_state(channel_id)
        await state.message_queue.put(message)
        state.queue_updated_event.set()
        logger.debug(f"Channel {channel_id}: Queued message (queue size: {state.message_queue.qsize()})")

    def get_message_queue(self, channel_id: int) -> asyncio.Queue:
        """Get the message queue for a channel.

        Args:
            channel_id: Discord channel ID

        Returns:
            The asyncio.Queue for the channel
        """
        state = self.get_state(channel_id)
        return state.message_queue

    def get_queue_event(self, channel_id: int) -> asyncio.Event:
        """Get the queue updated event for a channel.

        Args:
            channel_id: Discord channel ID

        Returns:
            The asyncio.Event signaling queue updates
        """
        state = self.get_state(channel_id)
        return state.queue_updated_event

    def clear_queue(self, channel_id: int) -> None:
        """Clear all messages from the queue.

        Args:
            channel_id: Discord channel ID
        """
        state = self.get_state(channel_id)
        while not state.message_queue.empty():
            try:
                state.message_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        state.queue_updated_event.clear()
        logger.debug(f"Channel {channel_id}: Cleared message queue")

    def increment_messages_processed(self, channel_id: int, count: int = 1) -> int:
        """Increment the messages processed counter for a channel.

        Args:
            channel_id: Discord channel ID
            count: Number to increment by (default 1)

        Returns:
            The new total message count
        """
        state = self.get_state(channel_id)
        state.messages_processed += count
        logger.debug(f"Channel {channel_id}: Messages processed incremented to {state.messages_processed}")
        return state.messages_processed

    def get_messages_processed(self, channel_id: int) -> int:
        """Get the total messages processed for a channel.

        Args:
            channel_id: Discord channel ID

        Returns:
            The total message count
        """
        state = self.get_state(channel_id)
        return state.messages_processed

    def reset_messages_processed(self, channel_id: int) -> None:
        """Reset the messages processed counter for a channel.

        Args:
            channel_id: Discord channel ID
        """
        state = self.get_state(channel_id)
        state.messages_processed = 0
        logger.debug(f"Channel {channel_id}: Messages processed reset to 0")

    def cleanup_channel(self, channel_id: int) -> None:
        """Clean up state for a channel.

        Args:
            channel_id: Discord channel ID
        """
        if channel_id in self.states:
            state = self.states[channel_id]
            self.clear_queue(channel_id)
            if state.typing_active:
                # Log that typing indicator should be stopped
                logger.debug(f"Channel {channel_id}: Cleaned up state with active typing indicator")
            del self.states[channel_id]
            logger.debug(f"Channel {channel_id}: Cleaned up channel state")


# Global instance
_channel_state_manager: Optional[ChannelStateManager] = None


def get_channel_state_manager() -> ChannelStateManager:
    """Get or create the global channel state manager.

    Returns:
        The global ChannelStateManager instance
    """
    global _channel_state_manager
    if _channel_state_manager is None:
        _channel_state_manager = ChannelStateManager()
    return _channel_state_manager


def initialize_channel_state_manager() -> ChannelStateManager:
    """Initialize the global channel state manager.

    Returns:
        The initialized ChannelStateManager instance
    """
    global _channel_state_manager
    _channel_state_manager = ChannelStateManager()
    return _channel_state_manager
