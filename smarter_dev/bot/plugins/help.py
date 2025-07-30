"""Help agent plugin for Discord bot conversational assistance.

This module provides a /help command and @mention handler that uses AI to answer
user questions about the bot's functionality, particularly the bytes economy
and squad management systems.
"""

from __future__ import annotations

import hikari
import lightbulb
import logging
from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING

from smarter_dev.bot.agent import HelpAgent, DiscordMessage, rate_limiter

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Create plugin
plugin = lightbulb.Plugin("help")

# Global help agent instance
help_agent = HelpAgent()


async def gather_message_context(
    bot: hikari.GatewayBot, 
    channel_id: int, 
    limit: int = 5
) -> List[DiscordMessage]:
    """Gather recent messages from a channel for context.
    
    Args:
        bot: Discord bot instance
        channel_id: Channel to gather messages from
        limit: Number of recent messages to gather
        
    Returns:
        List[DiscordMessage]: Recent messages for context
    """
    try:
        messages = []
        async for message in bot.rest.fetch_messages(channel_id).limit(limit):
            # Skip bot messages and system messages
            if message.author.is_bot or message.type != hikari.MessageType.DEFAULT:
                continue
                
            # Convert to our message format
            discord_msg = DiscordMessage(
                author=message.author.display_name or message.author.username,
                timestamp=message.created_at.replace(tzinfo=timezone.utc),
                content=message.content or ""
            )
            messages.append(discord_msg)
        
        # Return in chronological order (oldest first)
        return list(reversed(messages))
        
    except Exception as e:
        logger.warning(f"Failed to gather message context: {e}")
        return []


async def generate_help_response(
    user_id: str,
    user_question: str,
    context_messages: List[DiscordMessage] = None
) -> str:
    """Generate a help response with rate limiting.
    
    Args:
        user_id: Discord user ID
        user_question: User's question
        context_messages: Recent conversation context
        
    Returns:
        str: Generated response or rate limit message
    """
    # Check user rate limit
    if not rate_limiter.check_user_limit(user_id):
        remaining_requests = rate_limiter.get_user_remaining_requests(user_id)
        reset_time = rate_limiter.get_user_reset_time(user_id)
        
        if reset_time:
            minutes_left = max(1, int((reset_time - datetime.now()).total_seconds() / 60))
            return f"🕒 You've reached the rate limit of 10 questions per 30 minutes. Please try again in {minutes_left} minutes."
        else:
            return "🕒 You've reached the rate limit. Please try again in a few minutes."
    
    # Check token usage limit
    if not rate_limiter.check_token_limit():
        return "⚠️ The help system is currently at capacity. Please try again in a few minutes."
    
    try:
        # Generate response with token tracking
        response, tokens_used = help_agent.generate_response(user_question, context_messages)
        
        # Use fallback token estimate if no usage data available
        if tokens_used == 0:
            # Estimate based on response length (rough approximation)
            tokens_used = max(100, len(response) // 4)  # ~4 chars per token
            logger.warning(f"No token usage data available, using estimate: {tokens_used}")
        
        # Record the request with actual or estimated token usage
        rate_limiter.record_request(user_id, tokens_used)
        
        logger.info(f"Help response generated for {user_id}: {tokens_used} tokens used")
        
        return response
        
    except Exception as e:
        logger.error(f"Failed to generate help response: {e}")
        return "❌ Sorry, I'm having trouble generating a response right now. Please try again in a moment or contact an administrator if this persists."


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
        limit=5
    )
    
    # Generate response
    response = await generate_help_response(
        str(ctx.user.id),
        user_question,
        context_messages
    )
    
    # Edit the deferred response with the actual content
    await ctx.edit_last_response(response)
    
    logger.info(f"Help command used by {ctx.user.username} ({ctx.user.id}): {user_question[:50]}...")


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
            limit=6  # Get 6 to account for current message
        )
        
        # Remove the current message from context
        if context_messages:
            context_messages = [
                msg for msg in context_messages 
                if msg.content != event.content
            ][-5:]  # Keep last 5
        
        # Generate response (typing indicator will continue during this)
        response = await generate_help_response(
            str(event.author.id),
            user_question,
            context_messages
        )
    
    # Send public response (typing indicator stops automatically when we send the message)
    await plugin.bot.rest.create_message(event.channel_id, response)
    
    logger.info(f"Help mention handled for {event.author.username} ({event.author.id}): {user_question[:50]}...")


def load(bot: lightbulb.BotApp) -> None:
    """Load the help plugin."""
    bot.add_plugin(plugin)
    logger.info("Help plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the help plugin."""
    bot.remove_plugin(plugin)
    logger.info("Help plugin unloaded")