"""Mention handler plugin using multi-agent pipeline.

This module provides @mention handling that uses a pipeline of specialized agents:
1. Classification Agent - Determines if bot should respond, extracts intent/context
2. Response Agent - Generates response using tools, decides on continued watching
3. Evaluation Agent - (via watch loop) Evaluates new messages for watcher triggers

Flow:
    @mention → Classification Agent → Response Agent → [Watcher created if needed]
                                            ↑
    Watch Loop → Evaluation Agent ─────────┘ (when relevant messages found)
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import hikari
import lightbulb

from smarter_dev.bot.agents.classification_agent import get_classification_agent
from smarter_dev.bot.agents.response_agent import get_response_agent
from smarter_dev.bot.agents.watcher import WatcherContext
from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.rate_limiter import rate_limiter
from smarter_dev.bot.services.watch_loop import get_or_create_watch_loop
from smarter_dev.bot.services.watch_manager import get_watch_manager
from smarter_dev.shared.config import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Create plugin
plugin = lightbulb.Plugin("mention")

# Message limit for context building
CONTEXT_MESSAGE_LIMIT = 10


async def store_conversation(
    guild_id: str,
    channel_id: str,
    user_id: str,
    user_username: str,
    interaction_type: str,
    user_question: str,
    bot_response: str,
    tokens_used: int = 0,
    response_time_ms: int | None = None
) -> bool:
    """Store a mention conversation in the database for auditing and analytics.

    Args:
        guild_id: Discord guild ID
        channel_id: Discord channel ID
        user_id: Discord user ID
        user_username: Username at time of conversation
        interaction_type: 'mention' for @mentions
        user_question: User's question/mention
        bot_response: Bot's response
        tokens_used: AI tokens consumed
        response_time_ms: Response generation time

    Returns:
        bool: True if stored successfully, False otherwise
    """
    try:
        settings = get_settings()

        # Generate session ID
        session_id = str(uuid.uuid4())

        # Prepare conversation data
        conversation_data = {
            "session_id": session_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "user_username": user_username,
            "interaction_type": interaction_type,
            "context_messages": [],
            "user_question": user_question[:2000],
            "bot_response": bot_response[:4000],
            "tokens_used": tokens_used,
            "response_time_ms": response_time_ms,
            "retention_policy": "standard",
            "is_sensitive": False,
        }

        if settings.api_base_url:
            try:
                api_client = APIClient(
                    base_url=settings.api_base_url,
                    api_key=settings.bot_api_key
                )
                response = await api_client.post("/admin/conversations", json_data=conversation_data)
                if response.status_code in (200, 201):
                    logger.debug(f"Stored conversation {session_id}")
                    return True
                else:
                    logger.warning(f"API returned failure storing conversation: HTTP {response.status_code}")
                    return False
            except Exception as e:
                logger.error(f"Failed to store conversation with API: {e}")
                return False
        else:
            logger.debug("No API endpoint configured, skipping conversation storage")
            return True

    except Exception as e:
        logger.error(f"Error preparing conversation storage: {e}", exc_info=True)
        return False


async def handle_mention(event: hikari.MessageCreateEvent) -> None:
    """Handle a direct @mention of the bot.

    Args:
        event: The message create event
    """
    # Generate unique request ID for tracing
    request_id = str(uuid.uuid4())[:8]

    logger.info(
        f"[{request_id}] === MENTION RECEIVED === "
        f"channel={event.channel_id} message={event.message.id} "
        f"author={event.author.username} content='{(event.content or '')[:50]}...'"
    )

    # Extract the question (remove bot mention)
    bot_user = plugin.bot.get_me()
    user_question = event.content
    for user_id in event.message.user_mentions_ids:
        if user_id == bot_user.id:
            user_question = user_question.replace(f"<@{user_id}>", "").replace(f"<@!{user_id}>", "")
    user_question = user_question.strip()

    # Check rate limiting
    if not rate_limiter.check_token_limit():
        logger.warning(f"[{request_id}] Rate limited, rejecting mention")
        error_msg = (
            "⚠️ **Mention System at Capacity**\n\n"
            "I'm currently handling a lot of requests. Please try again in a few minutes!"
        )
        await plugin.bot.rest.create_message(event.channel_id, error_msg, reply=event.message)
        return

    start_time = datetime.now(UTC)
    total_tokens = 0

    try:
        # Build conversation context
        from smarter_dev.bot.utils.messages import ConversationContextBuilder

        logger.debug(f"[{request_id}] Building conversation context...")
        context_builder = ConversationContextBuilder(plugin.bot, event.guild_id)
        context = await context_builder.build_truncated_context(
            event.channel_id,
            trigger_message_id=event.message.id,
            limit=CONTEXT_MESSAGE_LIMIT
        )
        logger.debug(f"[{request_id}] Context built: {len(context['conversation_timeline'])} chars timeline")

        # Step 1: Run classification agent
        logger.info(f"[{request_id}] Step 1: Running classification agent...")
        classification_agent = get_classification_agent()

        # Get active watchers for this channel (for potential matching)
        watch_manager = get_watch_manager()
        active_watchers = []
        if await watch_manager.has_active_watchers(event.channel_id):
            channel_state = await watch_manager.get_or_create_channel(event.channel_id)
            watchers = await channel_state.get_all_watchers()
            active_watchers = [
                {"id": w.id, "watching_for": w.context.watching_for}
                for w in watchers
            ]
            logger.debug(f"[{request_id}] Found {len(active_watchers)} active watchers")

        classification = await classification_agent.classify(
            conversation_timeline=context["conversation_timeline"],
            trigger_message_id=str(event.message.id),
            bot_id=context["me"]["bot_id"],
            active_watchers=active_watchers if active_watchers else None
        )

        total_tokens += classification.tokens_used
        logger.info(
            f"[{request_id}] Classification result: should_respond={classification.should_respond}, "
            f"intent='{classification.intent[:80]}...', "
            f"matched_watcher={classification.matched_watcher_id}"
        )

        # Check if we should respond
        if not classification.should_respond:
            logger.info(
                f"[{request_id}] Classification decided NOT to respond - exiting pipeline"
            )
            return

        # Check if this matches an existing watcher
        if classification.matched_watcher_id:
            logger.info(f"[{request_id}] Mention matched existing watcher {classification.matched_watcher_id} - triggering watcher")
            await trigger_watcher_immediately(
                event.channel_id,
                event.guild_id,
                classification.matched_watcher_id,
                event.message.id,
                classification
            )
            return

        # Step 2: Run response agent for new topic
        logger.info(f"[{request_id}] Step 2: Running response agent...")
        response_agent = get_response_agent()

        # Filter timeline to relevant messages
        relevant_messages = filter_relevant_messages(
            context["conversation_timeline"],
            classification.relevant_message_ids + [str(event.message.id)]
        )
        logger.debug(f"[{request_id}] Filtered to {len(relevant_messages)} chars of relevant messages")

        success, output = await response_agent.generate_response(
            bot=plugin.bot,
            channel_id=event.channel_id,
            guild_id=event.guild_id,
            relevant_messages=relevant_messages,
            intent=classification.intent,
            context_summary=classification.context_summary,
            channel_info=context["channel"],
            users=context["users"],
            me_info=context["me"],
            request_id=request_id
        )

        total_tokens += output.tokens_used
        logger.info(
            f"[{request_id}] Response agent result: success={success}, "
            f"continue_watching={output.continue_watching}, tokens={output.tokens_used}"
        )

        if not success:
            logger.warning(f"[{request_id}] Response agent failed - exiting pipeline")
            return

        # Step 3: Create watcher if continuing
        if output.continue_watching:
            watcher_context = WatcherContext(
                relevant_message_ids=classification.relevant_message_ids,
                relevant_messages_summary=classification.context_summary,
                watching_for=output.watching_for,
                original_trigger_message_id=str(event.message.id)
            )

            watcher = await watch_manager.create_watcher(
                channel_id=event.channel_id,
                guild_id=event.guild_id,
                context=watcher_context,
                wait_duration=output.wait_duration,
                update_frequency=output.update_frequency
            )

            logger.info(
                f"[{request_id}] Step 3: Created watcher {watcher.id}: "
                f"watching_for='{output.watching_for[:50]}...', "
                f"wait_duration={output.wait_duration}s, "
                f"update_frequency={output.update_frequency.value}"
            )

            # Ensure watch loop is running
            await get_or_create_watch_loop(plugin.bot, event.channel_id, event.guild_id)
        else:
            logger.info(f"[{request_id}] No watcher created (continue_watching=False)")

        # Calculate response time and record
        end_time = datetime.now(UTC)
        response_time_ms = int((end_time - start_time).total_seconds() * 1000)

        logger.info(
            f"[{request_id}] === MENTION COMPLETE === "
            f"duration={response_time_ms}ms, tokens={total_tokens}"
        )

        # Record token usage for rate limiting
        if total_tokens > 0:
            rate_limiter.record_request(str(event.author.id), total_tokens, "mention")

        # Store conversation
        if event.guild_id:
            await store_conversation(
                guild_id=str(event.guild_id),
                channel_id=str(event.channel_id),
                user_id=str(event.author.id),
                user_username=event.author.display_name or event.author.username,
                interaction_type="mention",
                user_question=user_question,
                bot_response="[Multi-agent pipeline response]",
                tokens_used=total_tokens,
                response_time_ms=response_time_ms
            )

    except Exception as e:
        logger.error(f"[{request_id}] Error in mention handler: {e}", exc_info=True)


async def trigger_watcher_immediately(
    channel_id: int,
    guild_id: int,
    watcher_id: str,
    message_id: int,
    classification
) -> None:
    """Trigger an existing watcher immediately with a new mention.

    Args:
        channel_id: Discord channel ID
        guild_id: Discord guild ID
        watcher_id: ID of the watcher to trigger
        message_id: ID of the triggering message
        classification: Classification result from the new mention
    """
    watch_manager = get_watch_manager()
    channel_state = await watch_manager.get_or_create_channel(channel_id)
    watcher = await channel_state.get_watcher(watcher_id)

    if not watcher:
        logger.warning(f"Watcher {watcher_id} not found for immediate trigger")
        return

    # Update watcher context with new information
    watcher.context.relevant_message_ids.extend(classification.relevant_message_ids)
    watcher.context.relevant_messages_summary = (
        f"{watcher.context.relevant_messages_summary}\n"
        f"New context: {classification.context_summary}"
    )

    # Extend watcher expiration
    watcher.expires_at = datetime.now(UTC) + timedelta(seconds=watcher.wait_duration)

    # Add message to watcher queue for immediate evaluation
    await channel_state.queue_message({
        "id": str(message_id),
        "author_id": "",  # Will be filled from context
        "content": classification.intent,
        "timestamp": datetime.now(UTC),
        "is_mention": True
    })

    logger.info(f"Triggered watcher {watcher_id} immediately with message {message_id}")


async def queue_message_for_watchers(event: hikari.MessageCreateEvent) -> None:
    """Queue a message for all active watchers in the channel.

    Args:
        event: The message create event
    """
    watch_manager = get_watch_manager()
    channel_state = await watch_manager.get_or_create_channel(event.channel_id)

    await channel_state.queue_message({
        "id": str(event.message.id),
        "author_id": str(event.author.id),
        "content": event.content or "",
        "timestamp": event.message.timestamp,
        "is_mention": False
    })

    logger.debug(f"Queued message {event.message.id} for watchers in channel {event.channel_id}")


def filter_relevant_messages(timeline: str, relevant_ids: list[str]) -> str:
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


@plugin.listener(hikari.MessageCreateEvent)
async def on_message_create(event: hikari.MessageCreateEvent) -> None:
    """Handle message creation events, checking for mentions and routing to watchers.

    Flow:
    - If bot mentioned → handle_mention() (classification → response → watcher)
    - Else if channel has active watchers → queue_message_for_watchers()
    """
    # Ignore bot messages
    if event.message.author.is_bot:
        return

    # Ignore messages in DMs
    if not event.guild_id:
        return

    # Get bot user for mention checking
    bot_user = plugin.bot.get_me()
    if not bot_user:
        return

    # Check if bot is mentioned
    is_mentioned = bot_user.id in event.message.user_mentions_ids

    if is_mentioned:
        # Handle direct @mention with multi-agent pipeline
        await handle_mention(event)
    else:
        # Check if channel has active watchers
        watch_manager = get_watch_manager()
        if await watch_manager.has_active_watchers(event.channel_id):
            await queue_message_for_watchers(event)


def load(bot: lightbulb.BotApp) -> None:
    """Load the mention plugin."""
    bot.add_plugin(plugin)
    logger.info("Mention plugin loaded (multi-agent pipeline)")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the mention plugin."""
    # Stop all watch loops on unload
    import asyncio
    from smarter_dev.bot.services.watch_loop import cleanup_all_watch_loops

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(cleanup_all_watch_loops())
    except Exception as e:
        logger.warning(f"Error cleaning up watch loops: {e}")

    bot.remove_plugin(plugin)
    logger.info("Mention plugin unloaded")
