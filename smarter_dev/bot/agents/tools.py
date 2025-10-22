"""Tools available to Discord bot agents via ReAct pattern.

These tools enable agents to take Discord interaction actions within the
context-bound channel and guild where they were mentioned. All tools are
created as a factory to ensure they can only operate in their intended context.
"""

import logging
from typing import Callable, List
from functools import wraps
import inspect

from ddgs import DDGS

logger = logging.getLogger(__name__)


class ToolTracker:
    """Tracks tool calls made by the agent for reporting."""

    def __init__(self):
        """Initialize the tool tracker."""
        self.calls: List[dict] = []

    def track_call(self, tool_name: str, **kwargs) -> None:
        """Track a tool call with its parameters.

        Args:
            tool_name: Name of the tool that was called
            **kwargs: Parameters passed to the tool
        """
        # Format parameters for display
        params = self._format_params(kwargs)
        self.calls.append({
            "name": tool_name,
            "params": params,
            "raw_params": kwargs
        })
        logger.debug(f"[Tool Tracker] Recorded: {tool_name}({params})")

    def _format_params(self, params: dict) -> str:
        """Format parameters for display.

        Args:
            params: Dictionary of parameters

        Returns:
            Formatted parameter string like "param1=value1, param2=value2"
        """
        if not params:
            return ""

        formatted = []
        for key, value in params.items():
            # Truncate long values for display
            if isinstance(value, str) and len(value) > 50:
                display_value = f'"{value[:47]}..."'
            elif isinstance(value, str):
                display_value = f'"{value}"'
            else:
                display_value = repr(value)
            formatted.append(f"{key}={display_value}")
        return ", ".join(formatted)

    def get_summary(self) -> str:
        """Get formatted summary of all tool calls.

        Returns:
            Formatted string with all tool calls, or empty string if no calls were made
        """
        if not self.calls:
            return ""

        lines = ["-# Tools Used:"]
        for call in self.calls:
            lines.append(f"-# `{call['name']}({call['params']})`")
        return "\n".join(lines)

    def has_calls(self) -> bool:
        """Check if any tools were called.

        Returns:
            True if any tools were tracked, False otherwise
        """
        return len(self.calls) > 0


def create_mention_tools(bot, channel_id: str, guild_id: str, trigger_message_id: str) -> tuple[List[Callable], ToolTracker]:
    """Create context-bound Discord interaction tools for a mention agent.

    All returned tools are bound to the specific channel and guild where the
    mention occurred, ensuring they can only operate in that context.

    Args:
        bot: Discord bot instance (lightbulb.BotApp)
        channel_id: Channel where the mention occurred (string)
        guild_id: Guild where the mention occurred (string)
        trigger_message_id: ID of the message that triggered the mention

    Returns:
        Tuple of (List of callable async functions, ToolTracker instance)
    """

    # Create tracker for this agent execution
    tracker = ToolTracker()

    def _wrap_tool(tool_name: str, tool_func: Callable) -> Callable:
        """Wrap a tool function to track its calls.

        Args:
            tool_name: Name of the tool for tracking
            tool_func: The async tool function to wrap

        Returns:
            Wrapped async function that tracks calls
        """
        @wraps(tool_func)
        async def wrapper(*args, **kwargs):
            # Track the call with parameter names and values
            sig = inspect.signature(tool_func)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()
            tracker.track_call(tool_name, **bound_args.arguments)
            # Call the original function
            return await tool_func(*args, **kwargs)
        return wrapper

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

            # Get the first result from text search for a quick answer
            search_results = list(DDGS().text(query, max_results=1))

            if search_results and len(search_results) > 0:
                result = search_results[0]
                # Combine title and snippet for a quick answer
                answer = f"{result.get('title', 'No title')} - {result.get('body', '')[:200]}"
                return {
                    "success": True,
                    "answer": answer,
                    "url": result.get("href")
                }
            else:
                # No answer available
                return {
                    "success": True,
                    "answer": None,
                    "note": "No results found - use search_web for comprehensive search"
                }
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

            # Perform web search using DuckDuckGo
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
                return {
                    "success": True,
                    "results": results,
                    "count": len(results)
                }
            else:
                logger.warning(f"[Tool] search_web got no results for query: {query}")
                return {
                    "success": True,
                    "results": [],
                    "count": 0,
                    "note": "No results found for this query"
                }
        except Exception as e:
            logger.error(f"[Tool] search_web failed for query '{query}': {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }

    # Wrap only search tools with tracking (not message/reaction tools)
    tools = [
        send_message,
        reply_to_message,
        add_reaction_to_message,
        list_reaction_types,
        _wrap_tool("search_web_instant_answer", search_web_instant_answer),
        _wrap_tool("search_web", search_web)
    ]

    # Return wrapped tools and tracker
    return tools, tracker
