"""Help agent plugin for Discord bot conversational assistance.

This module provides a /help command and @mention handler that uses AI to answer
user questions about the bot's functionality, particularly the bytes economy
and squad management systems.
"""

from __future__ import annotations

import hikari
import lightbulb
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING

from smarter_dev.bot.agent import HelpAgent, DiscordMessage, rate_limiter
from smarter_dev.bot.utils.messages import gather_message_context

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Create plugin
plugin = lightbulb.Plugin("help")

# Global help agent instance
help_agent = HelpAgent()



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
    """Store a help conversation in the database for auditing and analytics.
    
    Args:
        guild_id: Discord guild ID
        channel_id: Discord channel ID
        user_id: Discord user ID
        user_username: Username at time of conversation
        interaction_type: 'slash_command' or 'mention'
        user_question: User's question
        bot_response: Bot's response
        context_messages: Context messages from channel
        tokens_used: AI tokens consumed
        response_time_ms: Response generation time
        
    Returns:
        bool: True if stored successfully, False otherwise
    """
    try:
        from smarter_dev.bot.services.api_client import APIClient
        from smarter_dev.shared.config import get_settings
        
        settings = get_settings()
        
        # Sanitize context messages for privacy
        sanitized_context = []
        if context_messages:
            for msg in context_messages:
                sanitized_context.append({
                    "author": msg.author,  # Already sanitized (display name only)
                    "timestamp": msg.timestamp.isoformat(),
                    "content": msg.content[:500]  # Truncate long messages
                })
        
        # Generate session ID (could be improved to link related conversations)
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
            "is_sensitive": False,  # TODO: Add sensitive content detection
            # Help-specific metadata for unified LLM command tracking
            "command_metadata": {
                "command_type": "help",
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


async def generate_help_response(
    user_id: str,
    user_question: str,
    context_messages: List[DiscordMessage] = None,
    guild_id: str = None,
    channel_id: str = None,
    user_username: str = None,
    interaction_type: str = "unknown",
    bot_id: str = None
) -> str:
    """Generate a help response with rate limiting and conversation storage.
    
    Args:
        user_id: Discord user ID
        user_question: User's question
        context_messages: Recent conversation context
        guild_id: Discord guild ID
        channel_id: Discord channel ID 
        user_username: Username for conversation record
        interaction_type: 'slash_command' or 'mention'
        bot_id: The bot's Discord user ID for context identification
        
    Returns:
        str: Generated response or rate limit message
    """
    # Check user rate limit for help command
    if not rate_limiter.check_user_limit(user_id, 'help'):
        remaining_requests = rate_limiter.get_user_remaining_requests(user_id, 'help')
        reset_time = rate_limiter.get_user_reset_time(user_id, 'help')
        
        if reset_time:
            minutes_left = max(1, int((reset_time - datetime.now()).total_seconds() / 60))
            return f"🕒 You've reached the rate limit of 10 help questions per 30 minutes. Please try again in {minutes_left} minutes."
        else:
            return "🕒 You've reached the rate limit. Please try again in a few minutes."
    
    # Check token usage limit
    if not rate_limiter.check_token_limit():
        return "⚠️ **Help System at Capacity**\n\nThe AI help system has reached its usage limits for this time period.\n\n**Please try again in 5-10 minutes** or use specific commands like `/bytes` or `/squad` for direct assistance."
    
    try:
        # Track response time
        start_time = datetime.now(timezone.utc)
        
        # Check remaining messages for conversation pacing
        # Since we're about to use one request, subtract 1 to get messages remaining AFTER this one
        current_remaining = rate_limiter.get_user_remaining_requests(user_id, 'help')
        messages_remaining = max(0, current_remaining - 1)
        
        # Generate response with token tracking
        response, tokens_used = help_agent.generate_response(
            user_question, 
            context_messages, 
            bot_id,
            interaction_type,
            messages_remaining
        )
        
        # Calculate response time
        end_time = datetime.now(timezone.utc)
        response_time_ms = int((end_time - start_time).total_seconds() * 1000)
        
        # Use fallback token estimate if no usage data available
        if tokens_used == 0:
            # Estimate based on response length (rough approximation)
            tokens_used = max(100, len(response) // 4)  # ~4 chars per token
            logger.warning(f"No token usage data available, using estimate: {tokens_used}")
        
        # Record the request with actual or estimated token usage for help command
        rate_limiter.record_request(user_id, tokens_used, 'help')
        
        logger.info(f"Help response generated for {user_id}: {tokens_used} tokens used in {response_time_ms}ms")
        
        # Store conversation in database if we have the required context
        if guild_id and channel_id and user_username:
            try:
                await store_conversation(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    user_username=user_username,
                    interaction_type=interaction_type,
                    user_question=user_question,
                    bot_response=response,
                    context_messages=context_messages,
                    tokens_used=tokens_used,
                    response_time_ms=response_time_ms
                )
            except Exception as storage_error:
                # Don't fail the response if storage fails
                logger.warning(f"Failed to store conversation for {user_id}: {storage_error}")
        
        return response
        
    except Exception as e:
        error_message = str(e).lower()
        logger.error(f"Failed to generate help response: {e}")
        
        # Provide specific error messages based on the type of failure
        if "overloaded" in error_message or "503" in error_message:
            return "🔄 **AI Service Temporarily Overloaded**\n\nThe AI service is experiencing high demand right now. This usually resolves within a few minutes.\n\n**Please try again in 2-3 minutes** or use `/help` with a specific question to get priority processing."
        
        elif "unavailable" in error_message or "502" in error_message or "504" in error_message:
            return "⚠️ **AI Service Temporarily Unavailable**\n\nThe AI help system is currently down for maintenance or experiencing technical issues.\n\n**Alternative:** Try using `/bytes` or `/squad` commands for specific features, or contact an administrator if urgent."
        
        elif "rate" in error_message or "quota" in error_message or "429" in error_message:
            return "⏱️ **Service Rate Limited**\n\nThe AI service has reached its usage limits. This is temporary and resets automatically.\n\n**Please wait 5-10 minutes** before asking for help again."
        
        elif "timeout" in error_message or "connection" in error_message:
            return "🌐 **Connection Issues**\n\nThere's a temporary network issue connecting to the AI service.\n\n**Please try again in a moment.** If this persists, the service may be experiencing outages."
        
        elif "api" in error_message and ("key" in error_message or "auth" in error_message):
            return "🔧 **Service Configuration Issue**\n\nThere's a configuration problem with the AI service.\n\n**Please contact an administrator** - this requires technical attention."
        
        else:
            return "❌ **Unexpected Error**\n\nSomething unexpected went wrong with the help system.\n\n**Try again in a moment** or contact an administrator if this keeps happening."


@plugin.command
@lightbulb.option("question", "Your question about the bot's functionality", required=False)
@lightbulb.command("help", "Get help with the bot's features and commands")
@lightbulb.implements(lightbulb.SlashCommand)
async def help_command(ctx: lightbulb.Context) -> None:
    """Handle help command - provides AI-powered assistance."""
    
    # Defer the response immediately to avoid 3-second timeout
    await ctx.respond(hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=hikari.MessageFlag.EPHEMERAL)
    
    user_question = ctx.options.question
    if not user_question:
        user_question = "How does this bot work? What commands are available?"
    
    # Gather context from recent channel messages
    context_messages = await gather_message_context(
        ctx.bot, 
        ctx.channel_id, 
        limit=10
    )
    
    # Get bot user for ID
    bot_user = ctx.bot.get_me()
    
    # Generate response with conversation storage
    response = await generate_help_response(
        user_id=str(ctx.user.id),
        user_question=user_question,
        context_messages=context_messages,
        guild_id=str(ctx.guild_id) if ctx.guild_id else None,
        channel_id=str(ctx.channel_id),
        user_username=ctx.user.display_name or ctx.user.username,
        interaction_type="slash_command",
        bot_id=str(bot_user.id) if bot_user else None
    )
    
    # Edit the deferred response with the actual content
    await ctx.edit_last_response(response)
    
    logger.info(f"Help command used by {ctx.user.display_name or ctx.user.username} ({ctx.user.id}): {user_question[:50]}...")


@plugin.listener(hikari.MessageCreateEvent)
async def on_message_create(event: hikari.MessageCreateEvent) -> None:
    """Handle @mention messages to provide help responses."""
    
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
    if not user_question:
        user_question = "How can you help me?"
    
    # Start typing indicator to show the bot is processing
    async with plugin.bot.rest.trigger_typing(event.channel_id):
        # Gather context from recent channel messages (excluding the current message)
        context_messages = await gather_message_context(
            plugin.bot, 
            event.channel_id, 
            limit=11  # Get 11 to account for current message
        )
        
        # Remove the current message from context
        if context_messages:
            context_messages = [
                msg for msg in context_messages 
                if msg.content != event.content
            ][-10:]  # Keep last 10
        
        # Generate response with conversation storage (typing indicator will continue during this)
        response = await generate_help_response(
            user_id=str(event.author.id),
            user_question=user_question,
            context_messages=context_messages,
            guild_id=str(event.guild_id) if event.guild_id else None,
            channel_id=str(event.channel_id),
            user_username=event.author.display_name or event.author.username,
            interaction_type="mention",
            bot_id=str(bot_user.id)
        )
    
    # Send public response as a reply (typing indicator stops automatically when we send the message)
    await plugin.bot.rest.create_message(event.channel_id, response, reply=event.message)
    
    logger.info(f"Help mention handled for {event.author.display_name or event.author.username} ({event.author.id}): {user_question[:50]}...")


def load(bot: lightbulb.BotApp) -> None:
    """Load the help plugin."""
    bot.add_plugin(plugin)
    logger.info("Help plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the help plugin."""
    bot.remove_plugin(plugin)
    logger.info("Help plugin unloaded")