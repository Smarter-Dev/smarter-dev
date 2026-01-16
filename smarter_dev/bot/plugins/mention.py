"""Mention handler plugin for conversational @mention interactions.

This module provides @mention handling that uses AI to respond conversationally
to members who mention the bot in the server. The agent controls its own participation
loop using wait_for_messages, fetch_new_messages, and other flow control tools.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

import hikari
import lightbulb

from smarter_dev.bot.agents.mention_agent import mention_agent
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.channel_state import get_channel_state_manager
from smarter_dev.bot.services.rate_limiter import rate_limiter
from smarter_dev.shared.config import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Create plugin
plugin = lightbulb.Plugin("mention")


async def store_conversation(
    guild_id: str,
    channel_id: str,
    user_id: str,
    user_username: str,
    interaction_type: str,
    user_question: str,
    bot_response: str,
    context_messages: list[DiscordMessage] = None,
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
        context_messages: Context messages from channel
        tokens_used: AI tokens consumed
        response_time_ms: Response generation time

    Returns:
        bool: True if stored successfully, False otherwise
    """
    try:
        settings = get_settings()

        # Sanitize context messages for privacy
        sanitized_context = []
        if context_messages:
            for msg in context_messages:
                sanitized_context.append({
                    "author": msg.author,
                    "timestamp": msg.timestamp.isoformat(),
                    "content": msg.content[:500]  # Truncate long messages
                })

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
            "context_messages": sanitized_context,
            "user_question": user_question[:2000],  # Truncate to prevent database errors
            "bot_response": bot_response[:4000],  # Truncate to prevent database errors
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
                # Continue anyway - conversation already happened, just failed to store
                return False
        else:
            logger.debug("No API endpoint configured, skipping conversation storage")
            return True

    except Exception as e:
        logger.error(f"Error preparing conversation storage: {e}", exc_info=True)
        return False


@plugin.listener(hikari.MessageCreateEvent)
async def on_message_create(event: hikari.MessageCreateEvent) -> None:
    """Handle message creation events, checking for mentions and triggering agent.

    This simplified handler delegates flow control to the agent via tools:
    - Direct @mentions: Invoke agent with auto-restart loop
    - Messages in monitored channels: Queue them for agent's wait_for_messages tool
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

    channel_state = get_channel_state_manager()

    # Check if bot is mentioned
    is_mentioned = bot_user.id in event.message.user_mentions_ids

    if is_mentioned:
        # Handle direct @mention - invoke agent with auto-restart loop
        # Extract the question (remove bot mention)
        user_question = event.content
        for user_id in event.message.user_mentions_ids:
            if user_id == bot_user.id:
                user_question = user_question.replace(f"<@{user_id}>", "").replace(f"<@!{user_id}>", "")

        user_question = user_question.strip()

        # Check rate limiting
        if not rate_limiter.check_token_limit():
            error_msg = "⚠️ **Mention System at Capacity**\n\nI'm currently handling a lot of requests. Please try again in a few minutes!"
            await plugin.bot.rest.create_message(event.channel_id, error_msg, reply=event.message)
            return

        # Check if agent is already running in this channel
        if not channel_state.start_agent(event.channel_id):
            # Agent is already running, queue this mention for the agent to process
            logger.debug(f"Agent already running in channel {event.channel_id}, queueing mention")
            await channel_state.queue_message(event.channel_id, {
                "id": str(event.message.id),
                "author_id": str(event.author.id),
                "content": event.content,
                "timestamp": event.message.timestamp,
                "is_mention": True
            })
            # Increment message counter for this message
            channel_state.increment_messages_processed(event.channel_id, 1)
            return

        try:
            # Track response time
            start_time = datetime.now(UTC)

            # Reset message counter for this conversation session
            channel_state.reset_messages_processed(event.channel_id)
            # The trigger message counts as 1
            channel_state.increment_messages_processed(event.channel_id, 1)

            # Run agent with auto-restart loop
            # Agent controls typing indicator via start_typing/stop_typing tools
            is_continuation = False
            total_tokens = 0
            previous_summary = ""

            while True:
                try:
                    # Generate response using mention agent
                    # Agent will manage its own flow via tools
                    success, tokens_used, response_text = await mention_agent.generate_response(
                        bot=plugin.bot,
                        channel_id=event.channel_id,
                        guild_id=event.guild_id,
                        trigger_message_id=event.message.id,
                        messages_remaining=10,
                        is_continuation=is_continuation,
                        previous_summary=previous_summary
                    )

                    total_tokens += tokens_used

                    if success and response_text:
                        logger.info(
                            f"Mention response sent: {len(response_text)} chars, {tokens_used} tokens"
                        )
                    else:
                        logger.debug("Agent did not send a response")

                except Exception as e:
                    logger.error(f"Error in agent execution: {e}", exc_info=True)
                    break

                # Check if agent wants to continue monitoring the channel
                if not channel_state.should_continue_monitoring(event.channel_id):
                    logger.debug(f"Channel {event.channel_id}: Agent finished monitoring")
                    break

                # Agent is waiting for messages via wait_for_messages tool
                # It will be auto-restarted with fresh context
                logger.debug(f"Channel {event.channel_id}: Agent continuing to monitor")
                is_continuation = True

                # Get any summary the agent stored for context continuity
                previous_summary = channel_state.get_conversation_summary(event.channel_id) or ""
                if previous_summary:
                    logger.debug(f"Channel {event.channel_id}: Passing conversation summary to restarted agent ({len(previous_summary)} chars)")
                    # Clear the summary after retrieving it (one-time use)
                    channel_state.set_conversation_summary(event.channel_id, None)

                # Reset message counter for fresh context in restarted agent
                channel_state.reset_messages_processed(event.channel_id)
                # Loop continues to restart agent

            # Calculate total response time
            end_time = datetime.now(UTC)
            response_time_ms = int((end_time - start_time).total_seconds() * 1000)

            # Record token usage for rate limiting
            if total_tokens > 0:
                rate_limiter.record_request(str(event.author.id), total_tokens, "mention")

            # Store conversation in database if we have the required context
            if event.guild_id:
                try:
                    await store_conversation(
                        guild_id=str(event.guild_id),
                        channel_id=str(event.channel_id),
                        user_id=str(event.author.id),
                        user_username=event.author.display_name or event.author.username,
                        interaction_type="mention",
                        user_question=user_question,
                        bot_response="[Agent response managed via tools]",
                        context_messages=None,
                        tokens_used=total_tokens,
                        response_time_ms=response_time_ms
                    )
                except Exception as storage_error:
                    # Don't fail if storage fails - agent already sent response
                    logger.warning(f"Failed to store conversation for {event.author.id}: {storage_error}")

        except Exception as e:
            logger.error(f"Error in mention handler: {e}", exc_info=True)
        finally:
            # Always mark agent as finished
            channel_state.finish_agent(event.channel_id)

    else:
        # Not a direct mention - check if agent is monitoring this channel
        if channel_state.is_agent_running(event.channel_id):
            # Agent is currently running - queue the message for wait_for_messages tool
            logger.debug(f"Channel {event.channel_id}: Queueing message for waiting agent")
            await channel_state.queue_message(event.channel_id, {
                "id": str(event.message.id),
                "author_id": str(event.author.id),
                "content": event.content,
                "timestamp": event.message.timestamp,
                "is_mention": False
            })
            # Increment message counter for this message
            channel_state.increment_messages_processed(event.channel_id, 1)


def load(bot: lightbulb.BotApp) -> None:
    """Load the mention plugin."""
    bot.add_plugin(plugin)
    logger.info("Mention plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the mention plugin."""
    bot.remove_plugin(plugin)
    logger.info("Mention plugin unloaded")
