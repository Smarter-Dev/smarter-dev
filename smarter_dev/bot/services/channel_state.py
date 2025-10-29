"""Channel state manager for tracking conversation participation windows.

This service manages per-channel state to implement natural conversation participation:
- Tracks whether the agent is currently running in a channel
- Manages debounced response timers with rolling 15-second delays and 1-minute caps
- Prevents concurrent agent executions in the same channel
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import hikari

logger = logging.getLogger(__name__)


class ChannelState:
    """Manages state for a single channel."""

    def __init__(self):
        """Initialize channel state."""
        self.is_running: bool = False  # Is agent currently processing
        self.last_response_time: Optional[datetime] = None  # When bot last responded
        self.watching_until: Optional[datetime] = None  # When watching period ends
        self.last_checked_message_id: Optional[int] = None  # Last message ID seen by background task

        # Debounce timer state for rolling delay logic
        self.first_new_message_time: Optional[datetime] = None  # When first user message arrived
        self.last_user_message_time: Optional[datetime] = None  # When most recent user message arrived
        self.response_timer_task: Optional[asyncio.Task] = None  # Pending delayed response task
        self.response_timer_callback: Optional[callable] = None  # Callback to trigger response

        # Track if messages arrived while agent was running (need debounce after finish)
        self.messages_arrived_during_run: bool = False  # Flag to restart debounce after agent finishes


class ChannelStateManager:
    """Manages per-channel state for natural conversation participation."""

    def __init__(self, watching_duration_minutes: int = 10, check_interval_seconds: int = 60):
        """Initialize the channel state manager.

        Args:
            watching_duration_minutes: How long to watch a channel after bot responds (default 10)
            check_interval_seconds: How often background task checks for new messages (default 60)
        """
        self.states: Dict[int, ChannelState] = {}
        self.watching_duration = timedelta(minutes=watching_duration_minutes)
        self.check_interval = timedelta(seconds=check_interval_seconds)
        logger.info(f"ChannelStateManager initialized with {watching_duration_minutes}min watch windows, {check_interval_seconds}s check intervals")

    def get_state(self, channel_id: int) -> ChannelState:
        """Get or create state for a channel.

        Args:
            channel_id: Discord channel ID

        Returns:
            ChannelState for the channel
        """
        if channel_id not in self.states:
            self.states[channel_id] = ChannelState()
        return self.states[channel_id]

    def start_agent(self, channel_id: int) -> bool:
        """Mark agent as running in a channel.

        Args:
            channel_id: Discord channel ID

        Returns:
            True if successfully marked as running, False if already running
        """
        state = self.get_state(channel_id)
        if state.is_running:
            logger.debug(f"Channel {channel_id}: Agent already running, skipping")
            return False

        state.is_running = True
        logger.debug(f"Channel {channel_id}: Agent started")
        return True

    def finish_agent(self, channel_id: int) -> None:
        """Mark agent as finished and start watching period.

        Args:
            channel_id: Discord channel ID
        """
        state = self.get_state(channel_id)
        state.is_running = False
        state.last_response_time = datetime.now(timezone.utc)
        state.watching_until = state.last_response_time + self.watching_duration
        logger.debug(f"Channel {channel_id}: Agent finished, watching until {state.watching_until}")

    def is_watching(self, channel_id: int) -> bool:
        """Check if a channel is in the watching period.

        Args:
            channel_id: Discord channel ID

        Returns:
            True if channel is actively being watched
        """
        state = self.get_state(channel_id)
        if state.watching_until is None:
            return False

        is_active = datetime.now(timezone.utc) < state.watching_until
        if not is_active:
            # Clean up expired state
            state.watching_until = None
            state.last_response_time = None
            state.last_checked_message_id = None

        return is_active

    def should_check_channel(self, channel_id: int) -> bool:
        """Determine if a channel should be checked by the background task.

        Args:
            channel_id: Discord channel ID

        Returns:
            True if channel is being watched and enough time has passed since last check
        """
        if not self.is_watching(channel_id):
            return False

        state = self.get_state(channel_id)
        if state.last_checked_message_id is None:
            # First check, should always check
            return True

        # Always check when watching (we check every minute whether there are new messages)
        return True

    def is_agent_running(self, channel_id: int) -> bool:
        """Check if agent is currently running in a channel.

        Args:
            channel_id: Discord channel ID

        Returns:
            True if agent is actively processing
        """
        state = self.get_state(channel_id)
        return state.is_running

    def update_last_checked(self, channel_id: int, message_id: int) -> None:
        """Update the last message ID seen by the background task.

        Args:
            channel_id: Discord channel ID
            message_id: The last message ID that was checked
        """
        state = self.get_state(channel_id)
        state.last_checked_message_id = message_id
        logger.debug(f"Channel {channel_id}: Updated last checked message to {message_id}")

    def get_last_checked_message_id(self, channel_id: int) -> Optional[int]:
        """Get the last message ID that was checked by the background task.

        Args:
            channel_id: Discord channel ID

        Returns:
            The last message ID checked, or None if no messages have been checked
        """
        state = self.get_state(channel_id)
        return state.last_checked_message_id

    def schedule_delayed_response(
        self,
        channel_id: int,
        callback: callable,
        initial_delay_seconds: int = 15,
        max_delay_seconds: int = 60
    ) -> None:
        """Schedule or reschedule a delayed response with debouncing.

        Implements rolling 15-second delay with 1-minute maximum cap.
        Each new message resets the timer, but response is guaranteed after 1 minute.

        Args:
            channel_id: Discord channel ID
            callback: Async callable to execute when timer fires (should trigger agent)
            initial_delay_seconds: Base delay between last message and response (default 15)
            max_delay_seconds: Maximum time from first message to response (default 60)
        """
        state = self.get_state(channel_id)
        now = datetime.now(timezone.utc)

        # Cancel any existing timer
        self.cancel_response_timer(channel_id)

        # If this is the first message in a batch, set the first_new_message_time
        if state.first_new_message_time is None:
            state.first_new_message_time = now
            logger.debug(f"Channel {channel_id}: First new message detected, starting debounce timer")

        # Update the last message time
        state.last_user_message_time = now

        # Calculate how much time has passed since the first message
        time_since_first = (now - state.first_new_message_time).total_seconds()
        remaining_time_until_max = max_delay_seconds - time_since_first

        # If we've already exceeded the max delay, respond immediately
        if remaining_time_until_max <= 0:
            delay = 0
            logger.debug(f"Channel {channel_id}: Exceeded max delay, responding immediately")
        else:
            # Otherwise, use the minimum of:
            # 1. The initial delay (15 seconds)
            # 2. The remaining time until max delay is exceeded
            delay = min(initial_delay_seconds, remaining_time_until_max)
            logger.debug(f"Channel {channel_id}: Scheduled delayed response in {delay:.1f}s (time since first: {time_since_first:.1f}s)")

        # Store callback for later use
        state.response_timer_callback = callback

        # Create and store the timer task
        state.response_timer_task = asyncio.create_task(
            self._execute_delayed_response(channel_id, delay)
        )

    async def _execute_delayed_response(self, channel_id: int, delay: float) -> None:
        """Execute the delayed response after the timer fires.

        Args:
            channel_id: Discord channel ID
            delay: How many seconds to wait before executing
        """
        try:
            if delay > 0:
                await asyncio.sleep(delay)

            # Verify channel still exists and has a callback
            state = self.get_state(channel_id)
            if state.response_timer_callback and not state.is_running:
                # Check if a new debounce will be scheduled before clearing state
                messages_will_arrive = state.messages_arrived_during_run

                # Call the callback to trigger the agent
                await state.response_timer_callback()

                # Only reset message tracking if no new debounce was scheduled
                # (i.e., no messages arrived during the agent's execution)
                state = self.get_state(channel_id)
                if not messages_will_arrive or not state.response_timer_task:
                    self.reset_message_tracking(channel_id)

        except asyncio.CancelledError:
            logger.debug(f"Channel {channel_id}: Delayed response timer cancelled")
        except Exception as e:
            logger.error(f"Channel {channel_id}: Error in delayed response execution: {e}", exc_info=True)

    def cancel_response_timer(self, channel_id: int) -> None:
        """Cancel any pending delayed response timer for a channel.

        Args:
            channel_id: Discord channel ID
        """
        state = self.get_state(channel_id)
        if state.response_timer_task:
            if not state.response_timer_task.done():
                state.response_timer_task.cancel()
            state.response_timer_task = None
            logger.debug(f"Channel {channel_id}: Cancelled pending delayed response")

    def reset_message_tracking(self, channel_id: int) -> None:
        """Reset message tracking timestamps when agent responds.

        Args:
            channel_id: Discord channel ID
        """
        state = self.get_state(channel_id)
        state.first_new_message_time = None
        state.last_user_message_time = None
        state.response_timer_callback = None
        state.messages_arrived_during_run = False
        logger.debug(f"Channel {channel_id}: Reset message tracking")

    def mark_messages_arrived_during_run(self, channel_id: int) -> None:
        """Mark that messages arrived while agent was running.

        Args:
            channel_id: Discord channel ID
        """
        state = self.get_state(channel_id)
        if state.is_running:
            state.messages_arrived_during_run = True
            logger.debug(f"Channel {channel_id}: Messages arrived during agent run")

    def cleanup_old_states(self) -> None:
        """Clean up channel states that have expired watching periods."""
        now = datetime.now(timezone.utc)
        expired_channels = []

        for channel_id, state in self.states.items():
            if state.watching_until is not None and now > state.watching_until:
                # Cancel any pending timers before cleanup
                self.cancel_response_timer(channel_id)
                expired_channels.append(channel_id)

        for channel_id in expired_channels:
            del self.states[channel_id]
            logger.debug(f"Channel {channel_id}: Cleaned up expired state")


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


def initialize_channel_state_manager(
    watching_duration_minutes: int = 10,
    check_interval_seconds: int = 60
) -> ChannelStateManager:
    """Initialize the global channel state manager with custom settings.

    Args:
        watching_duration_minutes: How long to watch a channel after bot responds
        check_interval_seconds: How often background task checks for new messages

    Returns:
        The initialized ChannelStateManager instance
    """
    global _channel_state_manager
    _channel_state_manager = ChannelStateManager(watching_duration_minutes, check_interval_seconds)
    return _channel_state_manager
