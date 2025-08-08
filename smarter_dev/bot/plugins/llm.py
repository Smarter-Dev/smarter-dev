"""LLM-powered features plugin for Discord bot.

This module provides AI-powered commands using DSPy agents for various
text processing tasks like summarization, translation, etc.
"""

from __future__ import annotations

import hikari
import lightbulb
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING

from smarter_dev.bot.agent import TLDRAgent, DiscordMessage, rate_limiter
from smarter_dev.bot.utils.messages import gather_message_context
from smarter_dev.bot.views.tldr_views import TLDRShareView

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Create plugin
plugin = lightbulb.Plugin("llm")

# Global TLDR agent instance
tldr_agent = TLDRAgent()




async def store_tldr_conversation(
    guild_id: str,
    channel_id: str,
    user_id: str,
    user_username: str,
    message_count_requested: int,
    messages_summarized: int,
    summary: str,
    tokens_used: int = 0,
    response_time_ms: Optional[int] = None
) -> bool:
    """Store a TLDR conversation in the database for auditing and analytics.
    
    Args:
        guild_id: Discord guild ID
        channel_id: Discord channel ID
        user_id: Discord user ID
        user_username: Username at time of conversation
        message_count_requested: Number of messages user requested to summarize
        messages_summarized: Actual number of messages summarized
        summary: Generated summary text
        tokens_used: AI tokens consumed
        response_time_ms: Response generation time
        
    Returns:
        bool: True if stored successfully, False otherwise
    """
    try:
        from smarter_dev.bot.services.api_client import APIClient
        from smarter_dev.shared.config import get_settings
        
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
            "interaction_type": "slash_command",
            "context_messages": [],  # We don't store the actual messages for privacy
            "user_question": f"Summarize last {message_count_requested} messages",
            "bot_response": summary[:4000],  # Truncate to prevent database errors
            "tokens_used": tokens_used,
            "response_time_ms": response_time_ms,
            "retention_policy": "standard",
            "is_sensitive": False,
            # TLDR-specific metadata
            "command_metadata": {
                "command_type": "tldr",
                "messages_requested": message_count_requested,
                "messages_processed": messages_summarized
            }
        }
        
        # Store conversation via API
        async with APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key
        ) as api_client:
            response = await api_client.post("/admin/conversations", json_data=conversation_data)
            
            if response.status_code == 201:
                logger.info(f"TLDR conversation stored successfully for user {user_id}")
                return True
            else:
                logger.warning(f"Failed to store TLDR conversation: HTTP {response.status_code}")
                return False
                
    except Exception as e:
        logger.error(f"Error storing TLDR conversation: {e}")
        return False


async def generate_tldr_summary(
    user_id: str,
    messages: List[DiscordMessage],
    message_count_requested: int,
    guild_id: str = None,
    channel_id: str = None,
    user_username: str = None
) -> str:
    """Generate a TLDR summary with rate limiting and conversation storage.
    
    Args:
        user_id: Discord user ID
        messages: Messages to summarize
        message_count_requested: Number of messages user requested
        guild_id: Discord guild ID
        channel_id: Discord channel ID 
        user_username: Username for conversation record
        
    Returns:
        str: Generated summary or rate limit/error message
    """
    # Check user rate limit for TLDR command
    if not rate_limiter.check_user_limit(user_id, 'tldr'):
        remaining_requests = rate_limiter.get_user_remaining_requests(user_id, 'tldr')
        reset_time = rate_limiter.get_user_reset_time(user_id, 'tldr')
        
        if reset_time:
            hours_left = max(1, int((reset_time - datetime.now()).total_seconds() / 3600))
            minutes_left = max(1, int((reset_time - datetime.now()).total_seconds() / 60))
            time_left = f"{hours_left} hour{'s' if hours_left != 1 else ''}" if hours_left >= 1 else f"{minutes_left} minute{'s' if minutes_left != 1 else ''}"
            return f"🕒 You've reached the rate limit of 5 TLDR requests per hour. Please try again in {time_left}."
        else:
            return "🕒 You've reached the TLDR rate limit. Please try again in a few minutes."
    
    # Check token usage limit
    if not rate_limiter.check_token_limit():
        return "⚠️ **LLM System at Capacity**\n\nThe AI system has reached its usage limits for this time period.\n\n**Please try again in 5-10 minutes** or use specific commands like `/bytes` or `/squad` for direct assistance."
    
    try:
        # Track response time
        start_time = datetime.now(timezone.utc)
        
        # Generate summary with token tracking
        summary, tokens_used, messages_summarized = tldr_agent.generate_summary(messages)
        
        # Calculate response time
        end_time = datetime.now(timezone.utc)
        response_time_ms = int((end_time - start_time).total_seconds() * 1000)
        
        # Use fallback token estimate if no usage data available
        if tokens_used == 0:
            # Estimate based on message content and response length
            from smarter_dev.bot.agent import estimate_message_tokens
            estimated_input = estimate_message_tokens(messages)
            estimated_output = len(summary) // 4
            tokens_used = estimated_input + estimated_output
            logger.warning(f"No token usage data available, using estimate: {tokens_used}")
        
        # Record the request with actual or estimated token usage for TLDR command
        rate_limiter.record_request(user_id, tokens_used, 'tldr')
        
        logger.info(f"TLDR summary generated for {user_id}: {tokens_used} tokens used in {response_time_ms}ms, {messages_summarized}/{message_count_requested} messages")
        
        # Store conversation in database if we have the required context
        if guild_id and channel_id and user_username:
            try:
                await store_tldr_conversation(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    user_username=user_username,
                    message_count_requested=message_count_requested,
                    messages_summarized=messages_summarized,
                    summary=summary,
                    tokens_used=tokens_used,
                    response_time_ms=response_time_ms
                )
            except Exception as storage_error:
                # Don't fail the response if storage fails
                logger.warning(f"Failed to store TLDR conversation for {user_id}: {storage_error}")
        
        return summary
        
    except Exception as e:
        error_message = str(e).lower()
        logger.error(f"Failed to generate TLDR summary: {e}")
        
        # Provide specific error messages based on the type of failure
        if "overloaded" in error_message or "503" in error_message:
            return "🔄 **AI Service Temporarily Overloaded**\n\nThe AI service is experiencing high demand right now. This usually resolves within a few minutes.\n\n**Please try again in 2-3 minutes** or try a smaller message count."
        
        elif "unavailable" in error_message or "502" in error_message or "504" in error_message:
            return "⚠️ **AI Service Temporarily Unavailable**\n\nThe AI help system is currently down for maintenance or experiencing technical issues.\n\n**Alternative:** Contact an administrator if urgent."
        
        elif "rate" in error_message or "quota" in error_message or "429" in error_message:
            return "⏱️ **Service Rate Limited**\n\nThe AI service has reached its usage limits. This is temporary and resets automatically.\n\n**Please wait 5-10 minutes** before trying again."
        
        elif "timeout" in error_message or "connection" in error_message:
            return "🌐 **Connection Issues**\n\nThere's a temporary network issue connecting to the AI service.\n\n**Please try again in a moment.** If this persists, the service may be experiencing outages."
        
        elif "context" in error_message or "token" in error_message or "length" in error_message:
            return "📄 **Content Too Large**\n\nThe messages you're trying to summarize contain too much text for the AI to process.\n\n**Try using a smaller message count** (like `/tldr count:5`) or wait for the conversation to move on a bit."
        
        else:
            return "❌ **Unexpected Error**\n\nSomething unexpected went wrong with the summarization.\n\n**Try again in a moment** with a smaller message count, or contact an administrator if this keeps happening."


@plugin.command
@lightbulb.option("count", "Number of recent messages to summarize (1-20, default: 5)", type=int, required=False, min_value=1, max_value=20)
@lightbulb.command("tldr", "Generate a summary of recent channel messages")
@lightbulb.implements(lightbulb.SlashCommand)
async def tldr_command(ctx: lightbulb.Context) -> None:
    """Handle TLDR command - provides AI-powered message summarization."""
    
    # Defer the response immediately to avoid 3-second timeout
    await ctx.respond(hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=hikari.MessageFlag.EPHEMERAL)
    
    # Get message count (default to 5)
    message_count = ctx.options.count if ctx.options.count is not None else 5
    
    # Gather messages from the channel (skip short messages for better summaries)
    messages = await gather_message_context(
        ctx.bot, 
        ctx.channel_id, 
        limit=message_count,
        skip_short_messages=True,
        min_message_length=10
    )
    
    if not messages:
        await ctx.edit_last_response(
            "📝 **No Messages to Summarize**\n\n"
            "I couldn't find any substantial messages to summarize in this channel. "
            "This might be because:\n"
            "• The channel is empty\n"
            "• Only bot messages or very short messages are present\n"
            "• There was an error accessing the channel history"
        )
        return
    
    # Generate summary with conversation storage
    summary = await generate_tldr_summary(
        user_id=str(ctx.user.id),
        messages=messages,
        message_count_requested=message_count,
        guild_id=str(ctx.guild_id) if ctx.guild_id else None,
        channel_id=str(ctx.channel_id),
        user_username=ctx.user.display_name or ctx.user.username
    )
    
    # Check if this is an error message or successful summary
    is_error = summary.startswith("🕒") or summary.startswith("⚠️") or summary.startswith("❌") or summary.startswith("🔄") or summary.startswith("🌐") or summary.startswith("📄")
    
    if is_error:
        # For error messages, don't include share button
        await ctx.edit_last_response(summary)
    else:
        # For successful summaries, add share button
        share_view = TLDRShareView(
            summary_content=summary,
            user_id=str(ctx.user.id),
            message_count=len(messages)
        )
        
        # Edit the deferred response with the content and share button
        await ctx.edit_last_response(
            content=summary,
            components=share_view.build_components()
        )
    
    logger.info(f"TLDR command used by {ctx.user.display_name or ctx.user.username} ({ctx.user.id}): requested {message_count} messages, processed {len(messages)}")


def load(bot: lightbulb.BotApp) -> None:
    """Load the LLM plugin."""
    bot.add_plugin(plugin)
    logger.info("LLM plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the LLM plugin."""
    bot.remove_plugin(plugin)
    logger.info("LLM plugin unloaded")