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
from smarter_dev.bot.services.rate_limiter import rate_limiter
from smarter_dev.bot.services.api_client import APIClient
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
    """Handle @mention messages to provide conversational responses."""

    # Skip if no message content or if it's from a bot
    if not event.content or event.author.is_bot:
        return

    # Skip if not a guild message
    if not event.guild_id:
        return

    # Check if bot is mentioned
    bot_user = plugin.bot.get_me()
    if not bot_user or bot_user.id not in event.message.user_mentions_ids:
        return

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

    try:
        # Track response time
        start_time = datetime.now(timezone.utc)

        # Generate response using mention agent
        async with plugin.bot.rest.trigger_typing(event.channel_id):
            response, tokens_used = await mention_agent.generate_response(
                bot=plugin.bot,
                channel_id=event.channel_id,
                guild_id=event.guild_id,
                trigger_message_id=event.message.id,
                messages_remaining=10
            )

        # Calculate response time
        end_time = datetime.now(timezone.utc)
        response_time_ms = int((end_time - start_time).total_seconds() * 1000)

        # Only send response if agent didn't skip
        if response:
            # Record token usage
            if tokens_used == 0:
                tokens_used = max(50, len(response) // 4)  # Rough estimate

            rate_limiter.record_request(str(event.author.id), tokens_used, 'mention')

            logger.info(f"Mention response generated: {len(response)} chars, {tokens_used} tokens in {response_time_ms}ms")

            # Send response as a reply
            await plugin.bot.rest.create_message(event.channel_id, response, reply=event.message)

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
                        bot_response=response,
                        context_messages=None,
                        tokens_used=tokens_used,
                        response_time_ms=response_time_ms
                    )
                except Exception as storage_error:
                    # Don't fail the response if storage fails
                    logger.warning(f"Failed to store conversation for {event.author.id}: {storage_error}")
        else:
            # Agent decided to skip (e.g., harmful content)
            logger.info(f"Mention skipped by agent for user {event.author.id}")

    except Exception as e:
        error_msg = str(e).lower()
        logger.error(f"Failed to generate mention response: {e}")

        # Provide specific error messages
        if "overloaded" in error_msg or "503" in error_msg:
            await plugin.bot.rest.create_message(event.channel_id,
                "🔄 I'm a bit overloaded right now. Try again in a moment!", reply=event.message)
        elif "unavailable" in error_msg or "502" in error_msg:
            await plugin.bot.rest.create_message(event.channel_id,
                "⚠️ I'm experiencing technical issues. Please try again later!", reply=event.message)
        else:
            await plugin.bot.rest.create_message(event.channel_id,
                "❌ Something went wrong. Please try again!", reply=event.message)


def load(bot: lightbulb.BotApp) -> None:
    """Load the mention plugin."""
    bot.add_plugin(plugin)
    logger.info("Mention plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the mention plugin."""
    bot.remove_plugin(plugin)
    logger.info("Mention plugin unloaded")
