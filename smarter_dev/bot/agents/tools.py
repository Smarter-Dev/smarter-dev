"""Tools available to Discord bot agents via ReAct pattern.

These tools enable agents to take Discord interaction actions within the
context-bound channel and guild where they were mentioned. All tools are
created as a factory to ensure they can only operate in their intended context.
"""

import logging
from typing import Callable, List, Optional, Dict, Any
from datetime import datetime, timedelta
from urllib.parse import urlparse
import asyncio
import re
import html
import io
import time
import random

import dspy
from ddgs import DDGS
import httpx
from markdownify import markdownify as md
import pdfplumber

from smarter_dev.bot.services.channel_state import get_channel_state_manager

logger = logging.getLogger(__name__)

# Custom user agent for the bot
BOT_USER_AGENT = "Smarter Dev Discord Bot - admin@smarter.dev"


class URLRateLimiter:
    """Rate limiter for URL requests, enforcing 5-second delays between requests to the same domain."""

    MIN_DELAY_SECONDS = 5

    def __init__(self):
        """Initialize the rate limiter."""
        # Track last request time per domain
        # Format: {domain: datetime}
        self.last_request_time: Dict[str, datetime] = {}
        # Lock for thread-safe operations
        self._lock = asyncio.Lock()

    async def wait_if_needed(self, url: str) -> None:
        """Wait if necessary to respect rate limiting for a domain.

        Args:
            url: The URL being requested
        """
        async with self._lock:
            # Extract domain from URL
            parsed = urlparse(url)
            domain = parsed.netloc

            # Check if we've made a request to this domain recently
            if domain in self.last_request_time:
                last_time = self.last_request_time[domain]
                elapsed = (datetime.now() - last_time).total_seconds()

                # Wait if not enough time has passed
                if elapsed < self.MIN_DELAY_SECONDS:
                    wait_time = self.MIN_DELAY_SECONDS - elapsed
                    logger.debug(f"[Rate Limit] Waiting {wait_time:.1f}s before next request to {domain}")
                    await asyncio.sleep(wait_time)

            # Update last request time
            self.last_request_time[domain] = datetime.now()
            logger.debug(f"[Rate Limit] Request to {domain} allowed")


# Global rate limiter instance
url_rate_limiter = URLRateLimiter()


class URLContentExtractionSignature(dspy.Signature):
    """You are an intelligent content extraction agent. Given web content (HTML converted to Markdown,
    PDF text, or plain text) and a specific question, extract and return the relevant information
    that answers the question.

    Focus on:
    - Directly answering the question asked
    - Being concise but complete
    - Including relevant context when needed
    - Citing specific information from the content
    - Admitting when the answer isn't in the content

    Keep responses under 1500 characters to fit Discord message limits.
    """
    url: str = dspy.InputField(description="The source URL of the content")
    content: str = dspy.InputField(description="The full content to analyze (Markdown, text, etc.)")
    content_type: str = dspy.InputField(description="Type of content: html, pdf, text, json")
    question: str = dspy.InputField(description="The specific question to answer about this content")

    answer: str = dspy.OutputField(
        description="Direct answer to the question based on the content, under 1500 characters"
    )


class EngagementPlanningSignature(dspy.Signature):
    """You are a strategic conversation planning agent for a Discord bot. You see the FULL conversation
    history and your job is to generate a comprehensive plan for how to engage with the current conversation.

    Your role is to:
    1. **Understand the situation**: Summarize what's happening in the conversation, who's involved, and what they want
    2. **Assess message directionality**: Determine if messages are actually directed at the bot or if the bot is just being referenced
    3. **Plan strategically**: Decide what tools to use and what actions to take to best engage with the conversation
    4. **Be specific**: Provide step-by-step recommendations that an execution agent can follow precisely

    ## Understanding Message Directionality

    Before planning engagement, analyze who is talking to whom. Not every mention of the bot requires a response.

    **Key Questions to Consider**:
    1. Is the message a reply to another user? If so, the primary audience is that user, not the bot
    2. Is the bot being asked for input (questions, requests, commands) or just referenced (citations, agreements)?
    3. Looking at the full conversation flow, is this an active request for the bot's participation, or is the bot
       being mentioned in passing during a conversation between other users?
    4. Does the reply structure and timestamps show an ongoing conversation between other people where the bot
       is just being cited?

    **When to Recommend Engagement**:
    - Messages clearly directed at the bot with questions or requests
    - Active conversations where the bot is a participant and input is being solicited
    - Direct commands or requests for the bot to perform actions

    **When to Recommend Staying Silent**:
    - Messages replying to other users where the bot is just referenced or cited
    - Passive mentions where no input is actually being requested
    - Conversations between others where the bot is mentioned in passing
    - If the recommended action is silence, explicitly state this in your plan

    The execution agent (Gemini) will follow your plan exactly, so be concrete about:
    - Which specific tools to call (send_message, reply_to_message, search_web, open_url, add_reaction_to_message, generate_in_depth_response, etc.)
    - What parameters to pass to each tool
    - The order to execute actions in
    - Or if no action should be taken

    Guidelines:
    - If the conversation is simple (greetings, quick reactions), recommend simple actions (react + brief message)
    - If research is needed, recommend search_web_instant_answer() or search_web() first
    - For technical/detailed responses, recommend TWO steps:
      1. `result = generate_in_depth_response(prompt_summary, prompt)` - generates response
      2. `send_message(result['response'])` - sends it to Discord
      - Use this for: technical explanations, code examples, detailed answers (anything longer than 2-3 lines)
      - First parameter is a brief summary shown to users (e.g., "async/await in Python")
      - Second parameter is the complete prompt with context, research results, and what's needed
      - Response is automatically limited to 1900 chars (Discord's 2000 char limit)
    - For quick casual responses, Gemini can write them directly
    - If a URL needs analysis, recommend open_url(url, question) with a specific question
    - If engaging with a specific message, recommend reply_to_message(message_id, content)
    - Always consider the conversation context and channel purpose when planning

    Keep your plan clear, actionable, and concise.
    """

    conversation_timeline: str = dspy.InputField(
        description="Full conversation timeline (20 messages) showing chronological message flow with timestamps, reply threads, and [NEW] markers"
    )
    users: list = dspy.InputField(
        description="List of users with user_id, discord_name, nickname, server_nickname, role_names, is_bot fields"
    )
    channel: dict = dspy.InputField(
        description="Channel info with name and description fields"
    )
    me: dict = dspy.InputField(
        description="Bot info with bot_name and bot_id fields"
    )

    summary: str = dspy.OutputField(
        description="Brief 2-3 sentence summary of the conversation state: who's involved, what's happening, what they want"
    )
    recommended_actions: str = dspy.OutputField(
        description="Step-by-step action plan with specific tools and parameters to use. Be concrete and precise. Example: '1. search_web(\"best used cars under $10k\") 2. send_message(\"Based on the search results...\")'"
    )
    reasoning: str = dspy.OutputField(
        description="Brief explanation (2-3 sentences) of why these actions make sense for this conversation"
    )


class InDepthResponseSignature(dspy.Signature):
    """You're that knowledgeable community member who loves diving deep into topics and explaining things in a way
    that actually makes sense. When someone asks a technical question or wants a detailed explanation, you're there
    with the good stuff - thorough, practical, but totally conversational.

    ## Your Vibe
    - You're chatting with someone on Discord, not writing documentation
    - Share knowledge like you're explaining to a friend over coffee
    - Be enthusiastic when something's cool, direct when something's tricky
    - Use natural language - "you can do X" not "one may accomplish X"
    - It's okay to say "honestly" or "basically" or "here's the thing"
    - Show personality - this is a conversation, not a textbook

    ## How to Structure Your Response
    - Start naturally - jump right into the explanation without formal intros
    - Use examples liberally - they're way more helpful than abstract explanations
    - Break complex topics into digestible chunks, but keep the flow conversational
    - If you need to list things, keep it casual (not overly formal bullet points)
    - End with something useful - a tip, a "this should work for you", or relevant next step

    ## Technical Content
    - Always format code properly: `inline code` for small snippets, ```language blocks``` for examples
    - Specify the language in code blocks (```python, ```javascript, etc.)
    - Include practical examples that someone can actually use or adapt
    - Explain WHY things work a certain way, not just HOW
    - If there are common gotchas or tips, mention them naturally

    ## Length & Style - CRITICAL CONSTRAINT
    - **ABSOLUTE MAXIMUM: 1900 characters** - Discord has a 2000 character limit, you MUST stay under 1900
    - Aim for 500-1500 characters for ideal balance of depth and readability
    - Don't pad for length - say what needs saying, then stop
    - It's fine to be direct and concise when that's what's needed
    - Use Discord markdown naturally: **bold** for emphasis, *italic* for nuance
    - Keep it readable - don't cram too much into one block
    - If you can't fit everything in 1900 chars, prioritize the most important points

    ## Context Handling
    - The prompt contains all context (question, conversation, search results)
    - Work with what you're given - don't ask for clarification
    - If replying to a specific message, make it feel like a natural response to them
    - Reference relevant context from the conversation when it makes sense

    Remember: You're not generating a report, you're sharing knowledge in a conversation. Be helpful, be thorough,
    but most importantly - be human. And ALWAYS stay under 1900 characters or your message will fail to send!
    """

    prompt_summary: str = dspy.InputField(
        description="Brief summary of what response is being generated (shown to users in status message)"
    )
    prompt: str = dspy.InputField(
        description="Complete prompt with all context: the question, relevant conversation, any search results, and what response is needed"
    )

    response: str = dspy.OutputField(
        description="In-depth, well-formatted response ready to send to Discord. MUST be under 1900 characters (Discord's limit is 2000). Aim for 500-1500 characters ideally, properly formatted with code blocks and markdown as needed."
    )


class SearchCache:
    """Manages search result caching with 24-hour TTL and per-channel query tracking."""

    # Cache TTL in hours
    CACHE_TTL_HOURS = 24

    def __init__(self):
        """Initialize the search cache."""
        # Separate caches for quick and full searches
        # Format: {query: (results, timestamp)}
        self.quick_search_cache: Dict[str, tuple[Any, datetime]] = {}
        self.full_search_cache: Dict[str, tuple[Any, datetime]] = {}
        # Track recent queries per channel
        # Format: {channel_id: [query1, query2, ...]}
        self.channel_queries: Dict[str, List[str]] = {}
        # URL cache (not tracked per channel)
        # Format: {url: (content, timestamp)}
        self.url_cache: Dict[str, tuple[str, datetime]] = {}
        # URL answer cache for question-specific responses
        # Format: {url::question: (answer, timestamp)}
        self.url_answer_cache: Dict[str, tuple[str, datetime]] = {}

    def get(self, query: str, cache_type: str = "full") -> Optional[Any]:
        """Get a cached result if it exists and hasn't expired.

        Args:
            query: The search query
            cache_type: "quick" for instant answer cache, "full" for full search cache

        Returns:
            Cached results if found and valid, None otherwise
        """
        cache = self.quick_search_cache if cache_type == "quick" else self.full_search_cache

        if query not in cache:
            return None

        results, timestamp = cache[query]
        # Check if expired
        if datetime.now() - timestamp > timedelta(hours=self.CACHE_TTL_HOURS):
            del cache[query]
            return None

        return results

    def set(self, query: str, results: Any, cache_type: str = "full") -> None:
        """Store results in cache.

        Args:
            query: The search query
            results: The search results to cache
            cache_type: "quick" for instant answer cache, "full" for full search cache
        """
        cache = self.quick_search_cache if cache_type == "quick" else self.full_search_cache
        cache[query] = (results, datetime.now())
        logger.debug(f"[Cache] Stored {cache_type} search for '{query}'")

    def add_channel_query(self, channel_id: str, query: str) -> None:
        """Track that a query was searched in a channel.

        Args:
            channel_id: The Discord channel ID
            query: The search query
        """
        if channel_id not in self.channel_queries:
            self.channel_queries[channel_id] = []
        # Add to front if not already there
        if query not in self.channel_queries[channel_id]:
            self.channel_queries[channel_id].insert(0, query)

    def get_channel_queries(self, channel_id: str) -> List[str]:
        """Get list of recent search queries in a channel.

        Args:
            channel_id: The Discord channel ID

        Returns:
            List of recent search query strings, or empty list if none
        """
        return self.channel_queries.get(channel_id, [])

    def get_url(self, url: str) -> Optional[str]:
        """Get cached URL content if it exists and hasn't expired.

        Args:
            url: The URL to retrieve

        Returns:
            Cached content if found and valid, None otherwise
        """
        if url not in self.url_cache:
            return None

        content, timestamp = self.url_cache[url]
        # Check if expired
        if datetime.now() - timestamp > timedelta(hours=self.CACHE_TTL_HOURS):
            del self.url_cache[url]
            return None

        return content

    def set_url(self, url: str, content: str) -> None:
        """Store URL content in cache.

        Args:
            url: The URL that was fetched
            content: The content/response to cache
        """
        self.url_cache[url] = (content, datetime.now())
        logger.debug(f"[Cache] Stored URL content for '{url}'")

    def get_url_answer(self, url: str, question: str) -> Optional[str]:
        """Get cached answer for a specific question about a URL.

        Args:
            url: The URL that was queried
            question: The specific question asked about the URL

        Returns:
            Cached answer if found and valid, None otherwise
        """
        cache_key = f"{url}::{question}"

        if cache_key not in self.url_answer_cache:
            return None

        answer, timestamp = self.url_answer_cache[cache_key]
        # Check if expired
        if datetime.now() - timestamp > timedelta(hours=self.CACHE_TTL_HOURS):
            del self.url_answer_cache[cache_key]
            return None

        return answer

    def set_url_answer(self, url: str, question: str, answer: str) -> None:
        """Store answer for a specific question about a URL.

        Args:
            url: The URL that was queried
            question: The specific question asked
            answer: The extracted answer to cache
        """
        cache_key = f"{url}::{question}"
        self.url_answer_cache[cache_key] = (answer, datetime.now())
        logger.debug(f"[Cache] Stored URL answer for '{url}' with question")


# Global search cache instance
search_cache = SearchCache()


def create_mention_tools(bot, channel_id: str, guild_id: str, trigger_message_id: str) -> tuple[List[Callable], List[str]]:
    """Create context-bound Discord interaction tools for a mention agent.

    All returned tools are bound to the specific channel and guild where the
    mention occurred, ensuring they can only operate in that context.

    Args:
        bot: Discord bot instance (lightbulb.BotApp)
        channel_id: Channel where the mention occurred (string)
        guild_id: Guild where the mention occurred (string)
        trigger_message_id: ID of the message that triggered the mention

    Returns:
        Tuple of (List of callable async functions, List of recent search queries in this channel)
    """

    async def send_message(content: str) -> dict:
        """Send a message to the channel where the bot was mentioned.

        REQUIRES: start_typing() must be called first to initialize the typing indicator.

        Use this for sending standalone messages that continue the conversation.
        For replying to a specific message, use reply_to_message instead.

        Args:
            content: Message content to send (max 2000 characters for Discord)

        Returns:
            dict with 'success' boolean and 'result' or 'error' string

        Example:
            start_typing()
            send_message("That's a great point!")
        """
        try:
            logger.debug(f"[Tool] send_message called in channel {channel_id}")
            channel_state = get_channel_state_manager().get_state(int(channel_id))

            # Check that typing indicator was started
            if not channel_state.typing_active:
                return {
                    "success": False,
                    "error": "Typing indicator not active. Call start_typing() first before sending messages."
                }

            # Check for duplicate message
            if channel_state.is_duplicate_message(content):
                logger.warning(f"[Tool] Duplicate message detected, rejecting send: {content[:50]}...")
                return {
                    "success": False,
                    "error": "This message was already sent within the last minute. Please avoid sending duplicate messages."
                }

            # Stop typing indicator before sending
            channel_state.typing_active = False
            message = await bot.rest.create_message(int(channel_id), content)

            # Track the message to prevent duplicates
            channel_state.add_recent_message(content)

            return {
                "success": True,
                "result": f"Message sent successfully (ID: {message.id}). Typing indicator stopped."
            }
        except Exception as e:
            logger.error(f"[Tool] send_message failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def reply_to_message(message_id: str, content: str) -> dict:
        """Reply to a specific message in the current channel, creating a threaded reply.

        REQUIRES: start_typing() must be called first to initialize the typing indicator.

        Message IDs appear in the conversation timeline as [ID: <number>] at the start of each line.
        Extract the number and pass it as message_id to create contextual threaded replies.

        Args:
            message_id: Discord message ID (extract from timeline's [ID: ...] prefix)
            content: Reply content (max 2000 characters for Discord)

        Returns:
            dict with 'success' boolean and 'result' or 'error' string

        Example:
            start_typing()
            reply_to_message("1234567890", "Great question! Here's what I think...")
        """
        try:
            # Validate message_id is numeric
            if not message_id or not message_id.strip().isdigit():
                return {
                    "success": False,
                    "error": f"Invalid message_id '{message_id}' - must be a numeric Discord message ID"
                }

            logger.debug(f"[Tool] reply_to_message called for message {message_id}")
            channel_state = get_channel_state_manager().get_state(int(channel_id))

            # Check that typing indicator was started
            if not channel_state.typing_active:
                return {
                    "success": False,
                    "error": "Typing indicator not active. Call start_typing() first before sending replies."
                }

            # Check for duplicate message
            if channel_state.is_duplicate_message(content):
                logger.warning(f"[Tool] Duplicate reply detected, rejecting send: {content[:50]}...")
                return {
                    "success": False,
                    "error": "This message was already sent within the last minute. Please avoid sending duplicate messages."
                }

            # Stop typing indicator before sending
            channel_state.typing_active = False
            message = await bot.rest.create_message(
                int(channel_id),
                content,
                reply=int(message_id)
            )

            # Track the message to prevent duplicates
            channel_state.add_recent_message(content)

            return {
                "success": True,
                "result": f"Reply sent successfully (ID: {message.id}). Typing indicator stopped."
            }
        except Exception as e:
            logger.error(f"[Tool] reply_to_message failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def add_reaction_to_message(message_id: str, emoji: str) -> dict:
        """Add an emoji reaction to a message in the current channel.

        Message IDs appear in the conversation timeline as [ID: <number>] at the start of each line.
        You can react to any message in the timeline to show engagement or support.

        Args:
            message_id: Discord message ID (extract from timeline's [ID: ...] prefix)
            emoji: Emoji to add (Unicode like "👍", "❤️", "😂" or custom in format "emoji_name:id")

        Returns:
            dict with 'success' boolean and 'result' or 'error' string

        Example:
            add_reaction_to_message("1234567890", "👍")
            add_reaction_to_message("1234567890", "laughing:123456789")
        """
        try:
            # Validate message_id is numeric
            if not message_id or not message_id.strip().isdigit():
                return {
                    "success": False,
                    "error": f"Invalid message_id '{message_id}' - must be a numeric Discord message ID"
                }

            # Clean up emoji format if it's in mention format <:name:id> or <emoji:id>
            emoji = emoji.strip()
            if emoji.startswith('<') and emoji.endswith('>'):
                # Strip angle brackets: <:emoji_name:123> -> :emoji_name:123
                emoji = emoji[1:-1]
                # If it still has a colon at start, keep as is (for custom emoji format)
                # Otherwise this might be invalid

            logger.debug(f"[Tool] add_reaction_to_message called for message {message_id} with emoji {emoji}")
            await bot.rest.add_reaction(
                int(channel_id),
                int(message_id),
                emoji
            )
            return {
                "success": True,
                "result": f"Reaction added successfully"
            }
        except Exception as e:
            logger.error(f"[Tool] add_reaction_to_message failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def list_reaction_types() -> dict:
        """List available emojis for the guild (both Unicode and custom emojis).

        Call this to discover what emojis you can use with add_reaction_to_message.
        Includes common Unicode emojis plus any custom emojis defined in this guild.

        Returns:
            dict with 'success' boolean and 'emoji_list' array, or 'error' string
            Each emoji in the list has 'name', 'type' (unicode/custom), and optional 'id' for custom

        Example:
            list_reaction_types() -> {"success": True, "emoji_list": [...], "count": 45}
        """
        try:
            logger.debug(f"[Tool] list_reaction_types called for guild {guild_id}")

            # Fetch custom emojis from the guild
            try:
                guild_emojis = await bot.rest.fetch_guild_emojis(int(guild_id))
                emoji_list = [
                    {
                        "name": emoji.name,
                        "id": str(emoji.id),
                        "mention": emoji.mention,
                        "type": "custom"
                    }
                    for emoji in guild_emojis
                ]
            except Exception as e:
                logger.warning(f"Could not fetch guild emojis: {e}")
                emoji_list = []

            # Add common Unicode emojis
            common_unicode_emojis = ["👍", "❤️", "😀", "😂", "🎉", "🔥", "✨", "😍", "🙏"]
            for emoji in common_unicode_emojis:
                emoji_list.append({
                    "name": emoji,
                    "type": "unicode"
                })

            return {
                "success": True,
                "emoji_list": emoji_list,
                "count": len(emoji_list)
            }
        except Exception as e:
            logger.error(f"[Tool] list_reaction_types failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def search_web_instant_answer(query: str) -> dict:
        """Search for quick answers and facts using DuckDuckGo.

        Returns the top search result as a quick answer for direct/simple questions.
        For more detailed information, use search_web instead.

        Queries should be either a topic or a specific question. Queries should be clear and specific,
        avoid vague or overly broad queries. The query should reflect the intent of the search, "DFS software
        development" is better than "DFS" but "Meaning of DFS in software development?" is even better as it clearly
        indicates the user is looking for a definition in terms of software development.

        Args:
            query: The question or topic to search for (e.g., "What is photosynthesis?")

        Returns:
            dict with 'success' boolean, 'answer' (title + snippet), and 'url' field

        Example:
            search_web_instant_answer("What is the capital of France?")
            -> {"success": True, "answer": "Paris - ...", "url": "..."}
        """
        try:
            logger.debug(f"[Tool] search_web_instant_answer called with query: {query}")

            # Send usage message (always, even for cache hits)
            usage_message = f'> -# Looking up details about "{query}"'
            try:
                await bot.rest.create_message(int(channel_id), usage_message)
            except Exception as e:
                logger.error(f"[Tool] Failed to send search usage message: {e}")

            # Check cache first
            cached_result = search_cache.get(query, cache_type="quick")
            if cached_result is not None:
                logger.debug(f"[Cache] Quick search hit for '{query}'")
                return cached_result

            # Not in cache, perform search
            search_results = list(DDGS().text(query, max_results=1))

            if search_results and len(search_results) > 0:
                result = search_results[0]
                # Combine title and snippet for a quick answer
                answer = f"{result.get('title', 'No title')} - {result.get('body', '')[:200]}"
                response = {
                    "success": True,
                    "answer": answer,
                    "url": result.get("href")
                }
            else:
                # No answer available
                response = {
                    "success": True,
                    "answer": None,
                    "note": "No results found - use search_web for comprehensive search"
                }

            # Cache and track this query
            search_cache.set(query, response, cache_type="quick")
            search_cache.add_channel_query(channel_id, query)

            return response

        except Exception as e:
            logger.error(f"[Tool] search_web_instant_answer failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def search_web(query: str, max_results: int = 3) -> dict:
        """Perform a comprehensive web search using DuckDuckGo.

        Returns multiple search results with titles, URLs, and snippets.
        Limited to 3 results by default to keep responses concise for Discord.

        Queries should be either a topic or a specific question. Queries should be clear and specific,
        avoid vague or overly broad queries. The query should reflect the intent of the search, "DFS software
        development" is better than "DFS" but "Meaning of DFS in software development?" is even better as it clearly
        indicates the user is looking for a definition in terms of software development.

        Args:
            query: The search query (e.g., "Python async programming")
            max_results: Maximum number of results to return (default 3, max 5 to stay concise)

        Returns:
            dict with 'success' boolean and 'results' list containing dicts with
            'title', 'url', and 'snippet' fields

        Example:
            search_web("machine learning frameworks")
            -> {"success": True, "results": [{"title": "...", "url": "...", "snippet": "..."}]}
        """
        try:
            # Limit to max 5 results to keep responses Discord-friendly
            max_results = min(max_results, 5)

            logger.debug(f"[Tool] search_web called with query: {query}, max_results: {max_results}")

            # Send usage message (always, even for cache hits)
            usage_message = (
                f'> -# Searching for "{query}" ({max_results} result{"s" if max_results != 1 else ""} max)'
            )
            try:
                await bot.rest.create_message(int(channel_id), usage_message)
            except Exception as e:
                logger.error(f"[Tool] Failed to send search usage message: {e}")

            # Use composite key to cache by query + max_results
            cache_key = f"{query}:{max_results}"

            # Check cache first
            cached_result = search_cache.get(cache_key, cache_type="full")
            if cached_result is not None:
                logger.debug(f"[Cache] Full search hit for '{cache_key}'")
                return cached_result

            # Not in cache, perform search
            search_results = list(DDGS().text(query, max_results=max_results))
            logger.debug(f"[Tool] search_web got {len(search_results)} results for query: {query}")

            if search_results:
                results = [
                    {
                        "title": result.get("title"),
                        "url": result.get("href"),
                        "snippet": result.get("body")
                    }
                    for result in search_results
                ]
                logger.debug(f"[Tool] search_web returning {len(results)} formatted results")
                response = {
                    "success": True,
                    "results": results,
                    "count": len(results)
                }
            else:
                logger.warning(f"[Tool] search_web got no results for query: {query}")
                response = {
                    "success": True,
                    "results": [],
                    "count": 0,
                    "note": "No results found for this query"
                }

            # Cache and track this query
            search_cache.set(cache_key, response, cache_type="full")
            search_cache.add_channel_query(channel_id, query)

            return response

        except Exception as e:
            logger.error(f"[Tool] search_web failed for query '{query}': {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }

    async def open_url(url: str, question: str) -> dict:
        """Fetch URL content and extract information answering a specific question.

        Supported content types:
        - text/plain: Plain text
        - text/html: Converted to Markdown with preserved structure
        - text/markdown: Markdown
        - application/json: JSON
        - application/pdf: Text extracted from all pages

        Args:
            url: The URL to open and fetch content from (e.g., "https://example.com")
            question: The specific question to answer about the content (e.g., "What is the release date?")

        Returns:
            dict with 'success' boolean, 'answer' (extracted response), and 'url'

        Example:
            open_url("https://en.wikipedia.org/wiki/Python_(programming_language)", "When was Python first released?")
            -> {"success": True, "answer": "Python was first released in 1991...", "url": "..."}
        """
        try:
            logger.debug(f"[Tool] open_url called with url: {url}, question: {question}")

            # Send usage message
            usage_message = f"> -# Opening '{url}' to answer: {question}"
            try:
                await bot.rest.create_message(int(channel_id), usage_message)
            except Exception as e:
                logger.error(f"[Tool] Failed to send url usage message: {e}")

            # Tier 2 Cache: Check if we already have an answer for this specific question
            cached_answer = search_cache.get_url_answer(url, question)
            if cached_answer is not None:
                logger.debug(f"[Cache] Answer cache hit for '{url}' with question")
                return {
                    "success": True,
                    "answer": cached_answer,
                    "url": url,
                    "note": "Answer from cache"
                }

            # Tier 1 Cache: Check if we have the content cached
            cached_content = search_cache.get_url(url)
            content = None
            content_type_str = ""
            final_url = url

            if cached_content is not None:
                logger.debug(f"[Cache] Content cache hit for '{url}'")
                content = cached_content
                # We'll need to determine content type for Gemini
                # For cached content, we'll default to "html" (most common)
                content_type_str = "html"
            else:
                # Content not cached - fetch it
                # Fetch the URL with httpx (follows redirects by default)
                try:
                    # Apply rate limiting
                    await url_rate_limiter.wait_if_needed(url)

                    async with httpx.AsyncClient(
                        follow_redirects=True,
                        timeout=30.0,
                        headers={"User-Agent": BOT_USER_AGENT}
                    ) as client:
                        response = await client.get(url)
                        response.raise_for_status()

                        # Check Content-Type header
                        content_type = response.headers.get("content-type", "").lower()
                        logger.debug(f"[Tool] Response content-type: '{content_type}'")

                        # Get response text for checking
                        response_text = response.text

                        # If no content-type but content looks like HTML, treat as HTML
                        if not content_type and response_text.strip().startswith("<"):
                            logger.debug(f"[Tool] No content-type header but content looks like HTML, treating as HTML")
                            content_type = "text/html"

                        allowed_types = ["text/plain", "text/html", "text/markdown", "application/json", "application/pdf"]

                        # Check if content type is allowed
                        if not any(allowed in content_type for allowed in allowed_types):
                            raise ValueError(f"Unsupported content type: {content_type}. Only text/plain, text/html, text/markdown, application/json, and application/pdf are accepted.")

                        # Get the final URL after redirects
                        final_url = str(response.url)
                        content = ""

                        # Process content based on content type
                        if "html" in content_type:
                            logger.debug(f"[Tool] Converting HTML to Markdown for {url}")
                            # Convert HTML to Markdown
                            html_content = response.text
                            content = md(html_content, heading_style="ATX")
                            content_type_str = "html"

                        elif "pdf" in content_type:
                            # Extract text from PDF (full content, no truncation)
                            try:
                                pdf_bytes = response.content
                                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                                    # Use list for efficient string building
                                    pdf_text_parts = []

                                    # Get total page count
                                    total_pages = len(pdf.pages)
                                    logger.debug(f"[Tool] PDF has {total_pages} pages, extracting full text...")

                                    for page_num, page in enumerate(pdf.pages, 1):
                                        try:
                                            page_text = page.extract_text()
                                            if page_text:
                                                page_header = f"--- Page {page_num} ---\n"
                                                pdf_text_parts.append(page_header)
                                                pdf_text_parts.append(page_text)
                                                pdf_text_parts.append("\n\n")
                                        except Exception as page_error:
                                            logger.warning(f"[Tool] Failed to extract text from page {page_num}: {page_error}")
                                            continue

                                    content = "".join(pdf_text_parts).strip()
                                    logger.debug(f"[Tool] Extracted {len(content)} chars from PDF")
                            except Exception as e:
                                logger.error(f"[Tool] Failed to extract PDF text: {e}")
                                raise ValueError(f"Failed to extract text from PDF: {e}")
                            content_type_str = "pdf"

                        elif "json" in content_type:
                            # For JSON, use as-is
                            content = response.text
                            content_type_str = "json"

                        else:
                            # For plain text, markdown, use as-is
                            content = response.text
                            content_type_str = "text"

                        # Cache the content (Tier 1)
                        search_cache.set_url(url, content)
                        logger.debug(f"[Tool] Fetched and cached {len(content)} chars from {url}")

                except httpx.TimeoutException:
                    logger.error(f"[Tool] Timeout fetching URL '{url}'")
                    return {
                        "success": False,
                        "error": "Request timeout - URL took too long to respond (30 second limit)",
                        "next_step": "Try searching for a different URL with similar information, or use search_web() to find alternative sources on this topic"
                    }
                except httpx.HTTPStatusError as e:
                    logger.error(f"[Tool] HTTP error fetching URL '{url}': {e.response.status_code}")
                    return {
                        "success": False,
                        "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
                    }
                except ValueError as e:
                    logger.error(f"[Tool] Unsupported content type for URL '{url}': {e}")
                    return {
                        "success": False,
                        "error": str(e)
                    }
                except Exception as e:
                    logger.error(f"[Tool] Failed to fetch URL '{url}': {e}")
                    return {
                        "success": False,
                        "error": str(e)
                    }

            # At this point, we have the content (either from cache or freshly fetched)
            # Now use Gemini to extract the answer to the question
            if content is None or len(content) == 0:
                return {
                    "success": False,
                    "error": "No content available to process"
                }

            logger.debug(f"[Tool] Using Gemini to extract answer from {len(content)} chars")

            # Initialize Gemini 2.5 Flash Lite via the judge model
            from smarter_dev.llm_config import get_llm_model
            import dspy

            gemini_lm = get_llm_model("judge")  # gemini-2.5-flash-lite

            # Use context manager to temporarily use Gemini without affecting global LLM config
            with dspy.context(lm=gemini_lm):
                # Use DSPy to extract the answer
                predictor = dspy.Predict(URLContentExtractionSignature)
                result = predictor(
                    url=final_url,
                    content=content,
                    content_type=content_type_str,
                    question=question
                )

            answer = result.answer

            # Cache the answer (Tier 2)
            search_cache.set_url_answer(url, question, answer)
            logger.debug(f"[Tool] Extracted and cached answer: {len(answer)} chars")

            return {
                "success": True,
                "answer": answer,
                "url": final_url
            }

        except Exception as e:
            logger.error(f"[Tool] open_url failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }

    async def start_typing() -> dict:
        """Start the typing indicator in the channel.

        Shows the "bot is typing" status to users. Use this when you're thinking or
        preparing a response. The indicator will remain active until you send a message
        (which auto-stops it) or call stop_typing().

        Returns:
            dict with 'success' boolean and message

        Example:
            start_typing()
        """
        try:
            logger.debug(f"[Tool] start_typing called in channel {channel_id}")
            channel_state_mgr = get_channel_state_manager()
            channel_state = channel_state_mgr.get_state(int(channel_id))

            if not channel_state.typing_active:
                channel_state.typing_active = True

                # Start background typing task that retriggers every 9 seconds
                await channel_state_mgr.start_typing_task(
                    int(channel_id),
                    bot.rest.trigger_typing
                )
                logger.debug(f"[Tool] Typing indicator started for channel {channel_id}")
                await asyncio.sleep(0.5)

            return {
                "success": True,
                "result": "Typing indicator started"
            }
        except Exception as e:
            logger.error(f"[Tool] start_typing failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def stop_typing() -> dict:
        """Stop the typing indicator in the channel.

        Stops the "bot is typing" status. Use this if you decide not to send a message
        or want to explicitly stop typing while thinking.

        Returns:
            dict with 'success' boolean and message

        Example:
            stop_typing()
        """
        try:
            logger.debug(f"[Tool] stop_typing called in channel {channel_id}")
            channel_state_mgr = get_channel_state_manager()
            channel_state = channel_state_mgr.get_state(int(channel_id))

            channel_state.typing_active = False
            channel_state_mgr.stop_typing_task(int(channel_id))

            logger.debug(f"[Tool] Typing indicator stopped for channel {channel_id}")
            return {
                "success": True,
                "result": "Typing indicator stopped"
            }
        except Exception as e:
            logger.error(f"[Tool] stop_typing failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def fetch_new_messages() -> dict:
        """Fetch messages sent since the last check.

        Returns a timeline of new messages in the channel. Useful for checking what's
        happened since you last checked. Updates the checkpoint so the next call returns
        only truly new messages.

        Returns:
            dict with 'success' boolean and 'messages' list (formatted timeline) or 'error'

        Example:
            fetch_new_messages()
        """
        try:
            logger.debug(f"[Tool] fetch_new_messages called in channel {channel_id}")
            channel_state_mgr = get_channel_state_manager()
            channel_state = channel_state_mgr.get_state(int(channel_id))

            # Get the last message ID we saw
            last_seen_id = channel_state.last_message_id_seen

            # Fetch messages from Discord API after the last seen message
            try:
                if last_seen_id:
                    # Fetch messages after the checkpoint
                    messages_list = []
                    async for msg in bot.rest.fetch_messages(int(channel_id), after=int(last_seen_id)):
                        messages_list.append(msg)
                        if len(messages_list) >= 50:  # Limit to 50 messages per fetch
                            break
                else:
                    # First fetch - get last 10 messages for context
                    messages_list = []
                    async for msg in bot.rest.fetch_messages(int(channel_id)):
                        messages_list.append(msg)
                        if len(messages_list) >= 10:
                            break

                if messages_list:
                    # Update checkpoint to last message ID
                    last_msg = messages_list[-1]
                    channel_state_mgr.set_last_message_id(int(channel_id), str(last_msg.id))

                    # Format messages as timeline
                    formatted_messages = []
                    for msg in reversed(messages_list):  # Reverse to chronological order
                        author = msg.author
                        author_name = author.global_name or author.username if author else "[Unknown]"
                        timestamp = msg.timestamp.strftime("%H:%M")
                        formatted_messages.append({
                            "id": str(msg.id),
                            "author": author_name,
                            "timestamp": timestamp,
                            "content": msg.content
                        })

                    return {
                        "success": True,
                        "messages": formatted_messages,
                        "count": len(formatted_messages)
                    }
                else:
                    return {
                        "success": True,
                        "messages": [],
                        "count": 0
                    }

            except Exception as e:
                logger.error(f"[Tool] Error fetching messages from Discord: {e}")
                return {
                    "success": False,
                    "error": f"Failed to fetch messages: {str(e)}"
                }

        except Exception as e:
            logger.error(f"[Tool] fetch_new_messages failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def wait_for_duration(seconds: int) -> dict:
        """Wait for a specified duration (max 300 seconds / 5 minutes).

        Use this when you want to pause before taking action, like thinking before
        responding or waiting for user reaction.

        Args:
            seconds: How many seconds to wait (must be 1-300)

        Returns:
            dict with 'success' boolean and wait duration

        Example:
            wait_for_duration(30)
        """
        try:
            # Validate seconds
            if not isinstance(seconds, (int, float)):
                return {
                    "success": False,
                    "error": "seconds must be a number"
                }

            seconds = int(seconds)
            if seconds < 1 or seconds > 300:
                return {
                    "success": False,
                    "error": "seconds must be between 1 and 300"
                }

            logger.debug(f"[Tool] wait_for_duration({seconds}s) called in channel {channel_id}")
            await asyncio.sleep(seconds)
            return {
                "success": True,
                "waited": seconds,
                "result": f"Waited {seconds} seconds"
            }
        except Exception as e:
            logger.error(f"[Tool] wait_for_duration failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def wait_for_messages() -> dict:
        """Wait for new messages with smart debounce logic and 5-minute maximum timeout.

        Waits for new messages in the channel with the following behavior:
        - Returns 5-10 seconds after the most recent message (random for natural feel)
        - Returns immediately if 10+ messages are queued
        - Waits up to 5 minutes total for conversation activity
        - If 100+ messages have been processed in this conversation, automatically stops monitoring
        - Use this to monitor conversation naturally

        Returns:
            dict with 'success' boolean and message list or timeout

        Example:
            wait_for_messages()
        """
        try:
            logger.debug(f"[Tool] wait_for_messages called in channel {channel_id}")
            channel_state = get_channel_state_manager().get_state(int(channel_id))

            # Signal that agent wants to continue monitoring
            # This allows the auto-restart loop to continue after this agent execution completes
            channel_state.continue_monitoring = True

            # Check if we've processed 100+ messages - if so, end the conversation session
            messages_processed = channel_state.messages_processed
            if messages_processed >= 100:
                logger.info(f"[Tool] Channel {channel_id}: Reached 100+ messages ({messages_processed}), ending conversation session")
                channel_state.continue_monitoring = False
                return {
                    "success": True,
                    "new_messages": [],
                    "count": 0,
                    "reason": "message_limit_reached",
                    "messages_processed": messages_processed
                }

            queue = channel_state.message_queue
            queue_event = channel_state.queue_updated_event

            messages = []
            max_wait_seconds = 300  # 5 minutes

            # Phase 1: Wait up to 5 minutes for the FIRST message
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=max_wait_seconds)
                messages.append(msg)
            except asyncio.TimeoutError:
                # 5 minutes passed with no messages - conversation ended
                logger.info(f"[Tool] Channel {channel_id}: 5-minute timeout with no messages, stopping monitoring")
                channel_state.continue_monitoring = False
                queue_event.clear()
                return {
                    "success": True,
                    "new_messages": [],
                    "count": 0,
                    "reason": "timeout",
                    "messages_processed": messages_processed
                }

            # Phase 2: We got at least one message! Switch to debounce mode
            # Wait 5-10 seconds for additional messages after each message
            while True:
                last_message_time = time.time()
                debounce_duration = random.uniform(5.0, 10.0)

                # Calculate remaining debounce time
                timeout = debounce_duration - (time.time() - last_message_time)

                if timeout <= 0:
                    # Debounce expired - return messages
                    break

                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=timeout)
                    messages.append(msg)

                    # Return immediately if we hit 10 messages
                    if len(messages) >= 10:
                        queue_event.clear()
                        return {
                            "success": True,
                            "new_messages": messages,
                            "count": len(messages),
                            "reason": "queue_full",
                            "messages_processed": messages_processed
                        }
                except asyncio.TimeoutError:
                    # Debounce timeout - return messages
                    break

            # Clear the event after processing
            queue_event.clear()

            return {
                "success": True,
                "new_messages": messages,
                "count": len(messages),
                "reason": "debounce_elapsed",
                "messages_processed": messages_processed
            }
        except Exception as e:
            logger.error(f"[Tool] wait_for_messages failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def stop_monitoring() -> dict:
        """Signal that the agent wants to stop monitoring the channel. This should be called when users request to stop.

        Use this when you're done participating in the conversation and don't want
        to be auto-restarted.

        Returns:
            dict with 'success' boolean and message

        Example:
            stop_monitoring()
        """
        try:
            logger.debug(f"[Tool] stop_monitoring called in channel {channel_id}")
            channel_state = get_channel_state_manager().get_state(int(channel_id))
            channel_state.continue_monitoring = False
            return {
                "success": True,
                "result": "Monitoring stopped. Agent will not be restarted."
            }
        except Exception as e:
            logger.error(f"[Tool] stop_monitoring failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def generate_engagement_plan() -> dict:
        """Generate a strategic engagement plan using Claude Haiku 4.5.

        This tool analyzes the FULL conversation context (20 messages) and generates
        a comprehensive plan for how to engage. It's useful when:
        - The situation is complex and needs strategic thinking
        - You need to understand broader conversation context
        - You're unsure what tools to use or how to respond
        - The conversation requires multi-step engagement

        The planning agent sees the full history and returns:
        - A summary of what's happening in the conversation
        - Recommended actions and tools to use (step by step)
        - Reasoning for why these actions make sense

        Once you receive a plan, execute the recommended steps precisely.

        Returns:
            dict with success, summary, recommended_actions, and reasoning fields

        Example usage:
            plan = generate_engagement_plan()
            # Then follow the recommended_actions in the plan
        """
        try:
            from smarter_dev.llm_config import get_llm_model
            from smarter_dev.bot.utils.messages import ConversationContextBuilder

            logger.info("[Tool] generate_engagement_plan called - building full context")

            # Send status message to channel
            try:
                await bot.rest.create_message(int(channel_id), "> -# Planning my response")
            except Exception as e:
                logger.warning(f"[Tool] Failed to send planning status message: {e}")

            # Build FULL context (20 messages) for strategic planning
            context_builder = ConversationContextBuilder(
                bot,
                int(guild_id) if guild_id else None
            )
            full_context = await context_builder.build_context(
                int(channel_id),
                int(trigger_message_id) if trigger_message_id else None
            )

            # Get Claude Haiku 4.5 for strategic planning
            claude_lm = get_llm_model("default")

            # Use context manager for thread-safe LLM switching
            with dspy.context(lm=claude_lm):
                predictor = dspy.Predict(EngagementPlanningSignature)
                result = predictor(
                    conversation_timeline=full_context["conversation_timeline"],
                    users=full_context["users"],
                    channel=full_context["channel"],
                    me=full_context["me"]
                )

            logger.info(f"[Tool] Planning agent generated plan: {result.summary[:100]}...")

            return {
                "success": True,
                "summary": result.summary,
                "recommended_actions": result.recommended_actions,
                "reasoning": result.reasoning
            }

        except Exception as e:
            logger.error(f"[Tool] generate_engagement_plan failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to generate plan: {str(e)}"
            }

    async def generate_in_depth_response(prompt_summary: str, prompt: str) -> dict:
        """Generate an in-depth, technical response using Claude Haiku 4.5.

        **WHAT THIS TOOL DOES**:
        - Generates detailed technical responses using Claude (not you, Gemini)
        - Returns a Discord-ready message that you MUST send using send_message()
        - Automatically enforces Discord's 2000 character limit (truncates to 1900 chars max)
        - Designed for technical/coding questions only

        **WHEN TO USE**:
        - Technical explanations or detailed answers
        - Code examples or programming help
        - Complex topic breakdowns
        - Structured, multi-part responses
        - Any technical question that needs more depth than a quick casual message

        **WHEN NOT TO USE**:
        - Casual conversation or opinions (you handle these)
        - Simple questions you can answer in 1-2 lines
        - Non-technical topics

        **IMPORTANT**: After calling this tool, you MUST call send_message(result['response'])
        to actually send the generated response to Discord!

        Args:
            prompt_summary: Brief description shown to users (e.g., "async/await in Python", "fixing AttributeError")
            prompt: Complete prompt with all context:
                - The user's question or request
                - Relevant conversation context
                - Any search results or information gathered
                - What kind of response is needed (explanation, code example, comparison, etc.)

        Returns:
            dict with success and response fields:
            - success: True if generation succeeded, False otherwise
            - response: Discord-ready message (max 1900 chars, properly formatted)
            - error: Error message if success is False

        Example:
            result = generate_in_depth_response(
                prompt_summary="async/await in Python",
                prompt="User asked: 'How do I use async/await in Python?'
                       Explain async/await in Python with a simple example.
                       Keep it under 1500 characters."
            )
            if result['success']:
                send_message(result['response'])  # YOU MUST DO THIS!
            else:
                send_message(f"Sorry, I had trouble generating a response: {result['error']}")
        """
        try:
            from smarter_dev.llm_config import get_llm_model

            logger.info(f"[Tool] generate_in_depth_response called: {prompt_summary}")

            # Send status message to channel with the provided summary
            try:
                await bot.rest.create_message(int(channel_id), f"> -# Writing a response for \"{prompt_summary}\"")
            except Exception as e:
                logger.warning(f"[Tool] Failed to send in-depth response status message: {e}")

            # Get Claude Haiku 4.5 for in-depth response generation
            claude_lm = get_llm_model("default")

            # Use context manager for thread-safe LLM switching
            with dspy.context(lm=claude_lm):
                predictor = dspy.Predict(InDepthResponseSignature)
                result = predictor(prompt_summary=prompt_summary, prompt=prompt)

            response_text = result.response
            original_length = len(response_text)

            # Enforce Discord's 2000 character limit with safety buffer
            MAX_LENGTH = 1900
            if len(response_text) > MAX_LENGTH:
                logger.warning(f"[Tool] Response too long ({original_length} chars), truncating to {MAX_LENGTH}")
                # Truncate and add indication
                response_text = response_text[:MAX_LENGTH-50] + "\n\n...(response truncated to fit Discord's limit)"

            logger.info(f"[Tool] Generated in-depth response: {len(response_text)} chars (original: {original_length} chars)")

            return {
                "success": True,
                "response": response_text,
                "original_length": original_length,
                "truncated": len(response_text) < original_length
            }

        except Exception as e:
            logger.error(f"[Tool] generate_in_depth_response failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to generate response: {str(e)}"
            }

    # Get recent search queries for this channel
    channel_queries = search_cache.get_channel_queries(channel_id)

    # Return tools and channel queries
    tools = [
        send_message,
        reply_to_message,
        add_reaction_to_message,
        list_reaction_types,
        search_web_instant_answer,
        search_web,
        open_url,
        generate_engagement_plan,
        generate_in_depth_response,
        start_typing,
        stop_typing,
        fetch_new_messages,
        wait_for_duration,
        wait_for_messages,
        stop_monitoring
    ]

    return tools, channel_queries
