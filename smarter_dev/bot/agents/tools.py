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

            # Stop typing indicator before sending
            channel_state.typing_active = False
            message = await bot.rest.create_message(int(channel_id), content)
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

            # Stop typing indicator before sending
            channel_state.typing_active = False
            message = await bot.rest.create_message(
                int(channel_id),
                content,
                reply=int(message_id)
            )
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
            usage_message = f"> -# Opening '{url}'"
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
                    title = ""

                    # Process content based on content type
                    if "html" in content_type:
                        logger.debug(f"[Tool] Converting HTML to Markdown for {url}")
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
                                # Use list for efficient string building
                                pdf_text_parts = []
                                content_length = 0
                                max_content_length = 2000  # Stop extracting after 2000 chars

                                # Get total page count for title
                                total_pages = len(pdf.pages)
                                logger.debug(f"[Tool] PDF has {total_pages} pages, extracting text...")

                                for page_num, page in enumerate(pdf.pages, 1):
                                    # Stop if we have enough content
                                    if content_length >= max_content_length:
                                        logger.debug(f"[Tool] Reached content limit at page {page_num} of {total_pages}")
                                        break

                                    try:
                                        page_text = page.extract_text()
                                        if page_text:
                                            page_header = f"--- Page {page_num} ---\n"
                                            pdf_text_parts.append(page_header)
                                            pdf_text_parts.append(page_text)
                                            pdf_text_parts.append("\n\n")
                                            content_length += len(page_header) + len(page_text) + 2
                                    except Exception as page_error:
                                        logger.warning(f"[Tool] Failed to extract text from page {page_num}: {page_error}")
                                        continue

                                content = "".join(pdf_text_parts).strip()
                                title = f"PDF ({total_pages} pages)"
                                logger.debug(f"[Tool] Extracted {len(content)} chars from PDF")
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
                logger.debug(f"[Tool] Channel {channel_id}: 5-minute timeout with no messages")
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
        """Signal that the agent wants to stop monitoring the channel.

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
        start_typing,
        stop_typing,
        fetch_new_messages,
        wait_for_duration,
        wait_for_messages,
        stop_monitoring
    ]

    return tools, channel_queries
