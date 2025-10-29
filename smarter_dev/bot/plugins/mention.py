"""Mention handler plugin for conversational @mention interactions.

This module provides @mention handling that uses AI to respond conversationally
to members who mention the bot in the server.
"""

from __future__ import annotations

import hikari
import lightbulb
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING

from smarter_dev.bot.agents.mention_agent import mention_agent
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.bot.agents.relevance_checker import get_relevance_checker
from smarter_dev.bot.services.rate_limiter import rate_limiter
from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.channel_state import get_channel_state_manager
from smarter_dev.bot.utils.messages import ConversationContextBuilder
from smarter_dev.shared.config import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Create plugin
plugin = lightbulb.Plugin("mention")


async def handle_delayed_response(
    bot: hikari.GatewayBot,
    channel_id: int,
    guild_id: int
) -> None:
    """Handle a delayed response triggered by the debounce timer.

    This function is called when the debounce timer fires (15-second lull in conversation,
    or 1-minute max timeout). It builds context, checks relevance, and triggers the agent.

    Args:
        bot: Discord bot instance
        channel_id: Discord channel ID
        guild_id: Discord guild ID
    """
    channel_state = get_channel_state_manager()

    try:
        # Check if agent should start
        if not channel_state.start_agent(channel_id):
            logger.debug(f"Channel {channel_id}: Agent already running, skipping debounced response")
            return

        try:
            # Build conversation context
            context_builder = ConversationContextBuilder(bot, guild_id)
            context = await context_builder.build_context(channel_id, None)

            # Check if conversation is relevant for bot response
            relevance_checker = get_relevance_checker()
            should_respond, reasoning = await relevance_checker.should_respond(context["conversation_timeline"])

            if not should_respond:
                logger.debug(f"Channel {channel_id}: Conversation not relevant for debounced response - {reasoning}")
                return

            logger.info(f"Channel {channel_id}: Debounced response - Conversation relevant - {reasoning}")

            # Show typing indicator while agent processes
            async with bot.rest.trigger_typing(channel_id):
                success, tokens_used, response_text = await mention_agent.generate_response(
                    bot=bot,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    trigger_message_id=None,  # No specific trigger message for debounced response
                    messages_remaining=10
                )

            if success and response_text:
                logger.info(
                    f"Channel {channel_id}: Debounced auto-response sent ({len(response_text)} chars, {tokens_used} tokens)"
                )
            else:
                logger.debug(f"Channel {channel_id}: Agent did not send a debounced response")

        except Exception as e:
            logger.error(f"Error in delayed response handler for channel {channel_id}: {e}", exc_info=True)
        finally:
            # Always mark agent as finished
            channel_state.finish_agent(channel_id)

            # If messages arrived during agent execution, start debounce for them
            state = channel_state.get_state(channel_id)
            if state.messages_arrived_during_run and channel_state.is_watching(channel_id):
                logger.debug(f"Channel {channel_id}: Messages arrived during debounced response, restarting debounce")
                callback = lambda: handle_delayed_response(bot, channel_id, guild_id)
                channel_state.schedule_delayed_response(channel_id, callback)

    except Exception as e:
        logger.error(f"Error processing delayed response for channel {channel_id}: {e}", exc_info=True)


async def store_conversation(
    guild_id: str,
    channel_id: str,
    user_id: str,
    user_username: str,
    interaction_type: str,
    user_question: str,
    bot_response: str,
    context_messages: List[DiscordMessage] = None,
    tokens_used: int = 0,
    response_time_ms: Optional[int] = None
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
            # Mention-specific metadata
            "command_metadata": {
                "command_type": "mention",
                "question_length": len(user_question),
                "context_message_count": len(sanitized_context) if sanitized_context else 0
            }
        }

        # Store conversation via API
        async with APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key
        ) as api_client:
            response = await api_client.post("/admin/conversations", json_data=conversation_data)

            if response.status_code == 201:
                logger.info(f"Conversation stored successfully for user {user_id}")
                return True
            else:
                logger.warning(f"Failed to store conversation: HTTP {response.status_code}")
                return False

    except Exception as e:
        logger.error(f"Error storing conversation: {e}")
        return False


@plugin.listener(hikari.MessageCreateEvent)
async def on_message_create(event: hikari.MessageCreateEvent) -> None:
    """Handle @mention messages and debounced conversation participation.

    This handler does two things:
    1. Immediately responds to @mentions of the bot
    2. For watched channels (in active conversation periods), schedules a debounced
       response if no mention is present (15-second lull with 1-minute max cap)
    """

    # Skip if no message content or if it's from a bot
    if not event.content or event.author.is_bot:
        return

    # Skip if not a guild message
    if not event.guild_id:
        return

    # Get bot user and channel state manager for later use
    bot_user = plugin.bot.get_me()
    channel_state = get_channel_state_manager()

    # Check if bot is mentioned
    is_mentioned = bot_user and bot_user.id in event.message.user_mentions_ids

    if is_mentioned:
        # Handle direct @mention - immediate response
        # Cancel any pending debounced response if bot is directly mentioned
        channel_state.cancel_response_timer(event.channel_id)
        channel_state.reset_message_tracking(event.channel_id)

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
            # Agent is already running, mention will be processed by the currently running agent
            logger.debug(f"Agent already running in channel {event.channel_id}, deferring mention")
            return

        try:
            # Track response time
            start_time = datetime.now(timezone.utc)

            # Show typing indicator while agent processes
            async with plugin.bot.rest.trigger_typing(event.channel_id):
                try:
                    # Generate response using mention agent
                    # Agent will send messages directly via tools
                    success, tokens_used, response_text = await mention_agent.generate_response(
                        bot=plugin.bot,
                        channel_id=event.channel_id,
                        guild_id=event.guild_id,
                        trigger_message_id=event.message.id,
                        messages_remaining=10
                    )
                finally:
                    # Always mark agent as finished, even if there's an error
                    channel_state.finish_agent(event.channel_id)

                    # If messages arrived during agent execution, start debounce for them
                    state = channel_state.get_state(event.channel_id)
                    if state.messages_arrived_during_run and channel_state.is_watching(event.channel_id):
                        logger.debug(f"Channel {event.channel_id}: Messages arrived during mention response, starting debounce")
                        callback = lambda: handle_delayed_response(plugin.bot, event.channel_id, event.guild_id)
                        channel_state.schedule_delayed_response(event.channel_id, callback)

            # Calculate response time
            end_time = datetime.now(timezone.utc)
            response_time_ms = int((end_time - start_time).total_seconds() * 1000)

            # If agent successfully sent a message, record metrics and store conversation
            if success and response_text:
                # Record token usage
                if tokens_used == 0:
                    tokens_used = max(50, len(response_text) // 4)  # Rough estimate

                rate_limiter.record_request(str(event.author.id), tokens_used, 'mention')

                logger.info(f"Mention response sent: {len(response_text)} chars, {tokens_used} tokens in {response_time_ms}ms")

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
                            bot_response=response_text,
                            context_messages=None,
                            tokens_used=tokens_used,
                            response_time_ms=response_time_ms
                        )
                    except Exception as storage_error:
                        # Don't fail if storage fails - agent already sent response
                        logger.warning(f"Failed to store conversation for {event.author.id}: {storage_error}")
            else:
                # Agent decided to skip (e.g., harmful content) or encountered an error
                logger.info(f"Mention not processed for user {event.author.id} (success={success}, has_response={response_text is not None})")

        except Exception as e:
            logger.error(f"Error in mention handler: {e}", exc_info=True)
            # Ensure agent is marked as finished
            channel_state.finish_agent(event.channel_id)
            # Agent should have handled this, but log any unhandled exceptions

    else:
        # Not a direct mention - check if channel is being watched for debounced responses
        if channel_state.is_watching(event.channel_id):
            if channel_state.is_agent_running(event.channel_id):
                # Agent is currently running - mark that we received messages
                # These will trigger debounce when agent finishes
                channel_state.mark_messages_arrived_during_run(event.channel_id)
            else:
                # Channel is in active conversation period and agent is not currently running
                # Schedule a debounced response with 15-second lull and 1-minute max cap
                callback = lambda: handle_delayed_response(plugin.bot, event.channel_id, event.guild_id)
                channel_state.schedule_delayed_response(event.channel_id, callback)


def load(bot: lightbulb.BotApp) -> None:
    """Load the mention plugin."""
    bot.add_plugin(plugin)
    logger.info("Mention plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the mention plugin."""
    bot.remove_plugin(plugin)
    logger.info("Mention plugin unloaded")
