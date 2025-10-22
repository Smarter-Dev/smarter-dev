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

from ddgs import DDGS
import httpx
from markdownify import markdownify as md
import pdfplumber

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

        Use this for sending standalone messages that continue the conversation.
        For replying to a specific message, use reply_to_message instead.

        Args:
            content: Message content to send (max 2000 characters for Discord)

        Returns:
            dict with 'success' boolean and 'result' or 'error' string

        Example:
            send_message("That's a great point!")
        """
        try:
            logger.debug(f"[Tool] send_message called in channel {channel_id}")
            message = await bot.rest.create_message(int(channel_id), content)
            return {
                "success": True,
                "result": f"Message sent successfully (ID: {message.id})"
            }
        except Exception as e:
            logger.error(f"[Tool] send_message failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def reply_to_message(message_id: str, content: str) -> dict:
        """Reply to a specific message in the current channel, creating a threaded reply.

        Message IDs appear in the conversation timeline as [ID: <number>] at the start of each line.
        Extract the number and pass it as message_id to create contextual threaded replies.

        Args:
            message_id: Discord message ID (extract from timeline's [ID: ...] prefix)
            content: Reply content (max 2000 characters for Discord)

        Returns:
            dict with 'success' boolean and 'result' or 'error' string

        Example:
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
            message = await bot.rest.create_message(
                int(channel_id),
                content,
                reply=int(message_id)
            )
            return {
                "success": True,
                "result": f"Reply sent successfully (ID: {message.id})"
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
            usage_message = f"-# Quick searching for '{query}'"
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
                f"-# Searching the web for '{query}' with max {max_results} result{'s' if max_results != 1 else ''}"
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

    async def open_url(url: str) -> dict:
        """Fetch and cache URL content with support for HTML, PDF, and text formats.

        Follows redirects and returns the page content/title. Converts HTML to Markdown
        for readability, and extracts text from PDFs. Results are cached in memory for
        24 hours.

        Supported content types:
        - text/plain: Plain text, returned as-is
        - text/html: Converted to Markdown with preserved structure
        - text/markdown: Markdown, returned as-is
        - application/json: JSON, returned as-is
        - application/pdf: Text extracted from all pages

        Args:
            url: The URL to open and fetch content from (e.g., "https://example.com")

        Returns:
            dict with 'success' boolean, 'title' (page/document title), 'content'
            (text or Markdown), and 'final_url' (after following redirects)

        Example:
            open_url("https://en.wikipedia.org/wiki/Python_(programming_language)")
            -> {"success": True, "title": "Python (programming language)", "content": "# Python\n...", "final_url": "..."}
        """
        try:
            logger.debug(f"[Tool] open_url called with url: {url}")

            # Send usage message
            usage_message = f"-# Opening '{url}'"
            try:
                await bot.rest.create_message(int(channel_id), usage_message)
            except Exception as e:
                logger.error(f"[Tool] Failed to send url usage message: {e}")

            # Check cache first
            cached_content = search_cache.get_url(url)
            if cached_content is not None:
                logger.debug(f"[Cache] URL hit for '{url}'")
                # Return cached result
                return {
                    "success": True,
                    "content": cached_content,
                    "note": "Result from cache"
                }

            # Fetch the URL with httpx (follows redirects by default)
            try:
                # Apply rate limiting
                await url_rate_limiter.wait_if_needed(url)

                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=10.0,
                    headers={"User-Agent": BOT_USER_AGENT}
                ) as client:
                    response = await client.get(url)
                    response.raise_for_status()

                    # Check Content-Type header
                    content_type = response.headers.get("content-type", "").lower()
                    allowed_types = ["text/plain", "text/html", "text/markdown", "application/json", "application/pdf"]

                    # Check if content type is allowed
                    if not any(allowed in content_type for allowed in allowed_types):
                        raise ValueError(f"Unsupported content type: {content_type}. Only text/plain, text/html, text/markdown, application/json, and application/pdf are accepted.")

                    # Get the final URL after redirects
                    final_url = str(response.url)
                    content = ""
                    title = ""

                    # Process content based on content type
                    if "html" in content_type:
                        # Convert HTML to Markdown
                        html_content = response.text
                        content = md(html_content, heading_style="ATX")

                        # Extract title from HTML if present
                        if "<title>" in html_content.lower():
                            try:
                                start = html_content.lower().index("<title>") + 7
                                end = html_content.lower().index("</title>", start)
                                title = html_content[start:end].strip()
                            except Exception:
                                title = "[Unable to extract title]"

                    elif "pdf" in content_type:
                        # Extract text from PDF
                        try:
                            pdf_bytes = response.content
                            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                                pdf_text = ""
                                for page_num, page in enumerate(pdf.pages, 1):
                                    page_text = page.extract_text()
                                    if page_text:
                                        pdf_text += f"--- Page {page_num} ---\n{page_text}\n\n"

                                content = pdf_text.strip()
                                title = f"PDF ({len(pdf.pages)} pages)"
                        except Exception as e:
                            logger.error(f"[Tool] Failed to extract PDF text: {e}")
                            raise ValueError(f"Failed to extract text from PDF: {e}")

                    else:
                        # For plain text, markdown, and JSON, use as-is
                        content = response.text

                    # Truncate content to keep manageable (first 2000 chars)
                    if len(content) > 2000:
                        content = content[:2000] + "..."

                    result = {
                        "success": True,
                        "title": title or "[No title]",
                        "content": content,
                        "final_url": final_url
                    }

                    # Cache the result
                    search_cache.set_url(url, content)

                    return result

            except httpx.TimeoutException:
                logger.error(f"[Tool] Timeout fetching URL '{url}'")
                return {
                    "success": False,
                    "error": "Request timeout - URL took too long to respond"
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

        except Exception as e:
            logger.error(f"[Tool] open_url failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
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
        open_url
    ]

    return tools, channel_queries
