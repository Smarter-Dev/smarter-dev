"""Watch loop for multi-agent mention pipeline.

This module contains the background task that:
- Routes pending messages to watcher queues
- Evaluates watchers on their update schedule
- Invokes response agent when evaluation triggers
- Cleans up expired watchers
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import hikari

from smarter_dev.bot.agents.evaluation_agent import get_evaluation_agent
from smarter_dev.bot.agents.response_agent import get_response_agent
from smarter_dev.bot.agents.watcher import Watcher
from smarter_dev.bot.services.watch_manager import get_watch_manager

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# How often the watch loop runs (seconds)
WATCH_LOOP_INTERVAL = 5

# Maximum time a watch loop can run before stopping (prevents runaway loops)
MAX_LOOP_RUNTIME_SECONDS = 3600  # 1 hour


class WatchLoop:
    """Background task that manages watchers for a channel."""

    def __init__(self, bot: hikari.GatewayBot, channel_id: int, guild_id: int):
        """Initialize the watch loop.

        Args:
            bot: Discord bot instance
            channel_id: Channel this loop manages
            guild_id: Guild ID for context
        """
        self.bot = bot
        self.channel_id = channel_id
        self.guild_id = guild_id
        self._task: asyncio.Task | None = None
        self._running = False
        self._started_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        """Check if the loop is running."""
        return self._running and self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the watch loop."""
        if self.is_running:
            logger.debug(f"Watch loop for channel {self.channel_id} already running")
            return

        self._running = True
        self._started_at = datetime.now(UTC)
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Started watch loop for channel {self.channel_id}")

    def stop(self) -> None:
        """Stop the watch loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info(f"Stopped watch loop for channel {self.channel_id}")

    async def _run_loop(self) -> None:
        """Main watch loop that runs in the background."""
        try:
            while self._running:
                # Safety check: stop if running too long
                if self._started_at:
                    runtime = (datetime.now(UTC) - self._started_at).total_seconds()
                    if runtime > MAX_LOOP_RUNTIME_SECONDS:
                        logger.warning(
                            f"Watch loop for channel {self.channel_id} exceeded max runtime "
                            f"({runtime:.0f}s), stopping"
                        )
                        break

                # Get watch manager and channel state
                watch_manager = get_watch_manager()
                channel_state = await watch_manager.get_or_create_channel(self.channel_id)

                # Check if there are any watchers
                if not await channel_state.has_active_watchers():
                    logger.info(
                        f"No active watchers in channel {self.channel_id}, stopping loop"
                    )
                    break

                # Route pending messages to watcher queues
                await self._route_pending_messages(channel_state)

                # Cleanup expired watchers
                expired = await channel_state.cleanup_expired_watchers()
                for watcher in expired:
                    logger.info(
                        f"Watcher {watcher.id} expired (was watching for: "
                        f"'{watcher.context.watching_for[:50]}...')"
                    )

                # Evaluate each watcher that is due
                watchers = await channel_state.get_all_watchers()
                for watcher in watchers:
                    if watcher.should_evaluate():
                        await self._evaluate_watcher(watcher, channel_state)

                # Sleep before next iteration
                await asyncio.sleep(WATCH_LOOP_INTERVAL)

        except asyncio.CancelledError:
            logger.debug(f"Watch loop for channel {self.channel_id} cancelled")
        except Exception as e:
            logger.error(f"Watch loop error for channel {self.channel_id}: {e}", exc_info=True)
        finally:
            self._running = False
            logger.debug(f"Watch loop for channel {self.channel_id} ended")

    async def _route_pending_messages(self, channel_state) -> None:
        """Route pending messages from channel queue to all watcher queues.

        Args:
            channel_state: The ChannelWatchState to process
        """
        pending = await channel_state.get_pending_messages()
        if not pending:
            return

        watchers = await channel_state.get_all_watchers()
        logger.debug(
            f"Routing {len(pending)} messages to {len(watchers)} watchers "
            f"in channel {self.channel_id}"
        )

        for msg in pending:
            for watcher in watchers:
                # Add message to watcher's queue
                watcher.queued_messages.append(msg)

    async def _evaluate_watcher(self, watcher: Watcher, channel_state) -> None:
        """Evaluate a watcher to see if it should respond.

        Args:
            watcher: The watcher to evaluate
            channel_state: The ChannelWatchState
        """
        # Don't evaluate if already responding
        if watcher.is_responding:
            logger.debug(f"Watcher {watcher.id} is already responding, skipping evaluation")
            return

        # Get queued messages
        if not watcher.queued_messages:
            return

        # Format messages for evaluation
        new_messages = self._format_messages_for_evaluation(watcher.queued_messages)

        # Clear queued messages (we've consumed them for evaluation)
        evaluated_messages = watcher.queued_messages.copy()
        watcher.queued_messages = []

        # Update last evaluation time
        watcher.last_evaluation_at = datetime.now(UTC)

        # Get bot ID
        bot_user = self.bot.get_me()
        bot_id = str(bot_user.id) if bot_user else ""

        # Run evaluation
        evaluation_agent = get_evaluation_agent()
        result = await evaluation_agent.evaluate(
            watching_for=watcher.context.watching_for,
            original_context=watcher.context.relevant_messages_summary,
            new_messages=new_messages,
            bot_id=bot_id
        )

        logger.debug(
            f"Watcher {watcher.id} evaluation: should_respond={result.should_respond}, "
            f"reasoning='{result.reasoning[:50]}...'"
        )

        if result.should_respond:
            # Mark relevant messages as consumed
            for msg_id in result.relevant_message_ids:
                channel_state.mark_message_consumed(msg_id)

            # Invoke response agent with personality hint
            await self._invoke_response_agent(
                watcher,
                channel_state,
                evaluated_messages,
                result.relevant_message_ids,
                result.personality_hint
            )

    async def _invoke_response_agent(
        self,
        watcher: Watcher,
        channel_state,
        messages: list[dict],
        relevant_ids: list[str],
        personality_hint: str = ""
    ) -> None:
        """Invoke the response agent for a watcher.

        Args:
            watcher: The watcher that triggered
            channel_state: The ChannelWatchState
            messages: Messages that triggered this response
            relevant_ids: IDs of relevant messages
            personality_hint: Suggested personality/tone from evaluation agent
        """
        # Generate request ID for this watcher response
        request_id = f"watcher-{str(uuid.uuid4())[:8]}"

        logger.info(
            f"[{request_id}] === WATCHER TRIGGERED === "
            f"watcher={watcher.id[:8]}..., channel={self.channel_id}, "
            f"watching_for='{watcher.context.watching_for[:50]}...'"
        )

        # Acquire lock to prevent concurrent response agents
        if not await watcher.response_lock.acquire():
            logger.warning(f"[{request_id}] Response lock unavailable, skipping")
            return

        try:
            watcher.is_responding = True
            logger.debug(f"[{request_id}] Building context for response agent...")

            # Build context for response agent
            from smarter_dev.bot.utils.messages import ConversationContextBuilder

            context_builder = ConversationContextBuilder(self.bot, self.guild_id)
            context = await context_builder.build_truncated_context(
                self.channel_id,
                trigger_message_id=int(relevant_ids[0]) if relevant_ids else None,
                limit=10
            )

            # For watcher responses, use the FULL recent conversation timeline
            # Don't filter aggressively - the bot needs to see what was actually said
            relevant_messages = context["conversation_timeline"]
            logger.debug(f"[{request_id}] Context built, invoking response agent...")

            # Invoke response agent
            response_agent = get_response_agent()
            success, output = await response_agent.generate_response(
                bot=self.bot,
                channel_id=self.channel_id,
                guild_id=self.guild_id,
                relevant_messages=relevant_messages,
                intent=f"Continue conversation - user said something new",
                context_summary=f"Watching for: {watcher.context.watching_for}",
                channel_info=context["channel"],
                users=context["users"],
                me_info=context["me"],
                request_id=request_id,
                personality_hint=personality_hint
            )

            if success:
                logger.info(
                    f"[{request_id}] Response complete: continue_watching={output.continue_watching}, "
                    f"tokens={output.tokens_used}"
                )

                if output.continue_watching:
                    # Update watcher for continued monitoring
                    watcher.context.watching_for = output.watching_for
                    watcher.wait_duration = output.wait_duration
                    watcher.update_frequency = output.update_frequency
                    watcher.expires_at = datetime.now(UTC) + timedelta(seconds=output.wait_duration)
                    logger.info(
                        f"[{request_id}] Watcher continuing: watching_for='{output.watching_for[:50]}...', "
                        f"expires_at={watcher.expires_at}"
                    )
                else:
                    # Remove watcher
                    await channel_state.remove_watcher(watcher.id)
                    logger.info(f"[{request_id}] Watcher removed (continue_watching=False)")
            else:
                logger.warning(f"[{request_id}] Response agent failed")
                # Remove watcher on failure
                await channel_state.remove_watcher(watcher.id)

        except Exception as e:
            logger.error(f"[{request_id}] Error invoking response agent: {e}", exc_info=True)
            # Remove watcher on error
            await channel_state.remove_watcher(watcher.id)
        finally:
            watcher.is_responding = False
            watcher.response_lock.release()
            logger.debug(f"[{request_id}] === WATCHER COMPLETE ===")

    def _format_messages_for_evaluation(self, messages: list[dict]) -> str:
        """Format messages for the evaluation agent.

        Args:
            messages: List of message dicts

        Returns:
            Formatted string for evaluation
        """
        lines = []
        for msg in messages:
            msg_id = msg.get("id", "?")
            author = msg.get("author_id", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", "")

            # Format timestamp if it's a datetime
            if hasattr(timestamp, "strftime"):
                timestamp = timestamp.strftime("%H:%M:%S")

            lines.append(f"[ID: {msg_id}] [{timestamp}] User {author}: {content}")

        return "\n".join(lines)

    def _filter_relevant_messages(self, timeline: str, relevant_ids: list[str]) -> str:
        """Filter a timeline to only include relevant message IDs.

        Args:
            timeline: Full conversation timeline
            relevant_ids: List of message IDs to keep

        Returns:
            Filtered timeline with only relevant messages
        """
        if not relevant_ids:
            return timeline

        relevant_set = set(relevant_ids)
        filtered_lines = []

        for line in timeline.split("\n"):
            # Check if line contains a relevant message ID
            for msg_id in relevant_set:
                if f"[ID: {msg_id}]" in line:
                    filtered_lines.append(line)
                    break
            else:
                # Keep header/footer lines
                if line.startswith("===") or not line.strip():
                    filtered_lines.append(line)

        return "\n".join(filtered_lines)


# Global registry of watch loops
_watch_loops: dict[int, WatchLoop] = {}
_loops_lock = asyncio.Lock()


async def get_or_create_watch_loop(
    bot: hikari.GatewayBot,
    channel_id: int,
    guild_id: int
) -> WatchLoop:
    """Get or create a watch loop for a channel.

    Args:
        bot: Discord bot instance
        channel_id: Channel ID
        guild_id: Guild ID

    Returns:
        WatchLoop for the channel
    """
    async with _loops_lock:
        if channel_id not in _watch_loops:
            _watch_loops[channel_id] = WatchLoop(bot, channel_id, guild_id)

        loop = _watch_loops[channel_id]

        # Start if not running
        if not loop.is_running:
            loop.start()

        return loop


async def stop_watch_loop(channel_id: int) -> None:
    """Stop the watch loop for a channel.

    Args:
        channel_id: Channel ID
    """
    async with _loops_lock:
        if channel_id in _watch_loops:
            _watch_loops[channel_id].stop()
            del _watch_loops[channel_id]


async def cleanup_all_watch_loops() -> None:
    """Stop all watch loops (for shutdown)."""
    async with _loops_lock:
        for loop in _watch_loops.values():
            loop.stop()
        _watch_loops.clear()
