"""Tools available to Discord bot agents via ReAct pattern.

These tools enable agents to take Discord interaction actions within the
context-bound channel and guild where they were mentioned. All tools are
created as a factory to ensure they can only operate in their intended context.
"""

import logging
from typing import Callable, List

from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)


def create_mention_tools(bot, channel_id: str, guild_id: str, trigger_message_id: str) -> List[Callable]:
    """Create context-bound Discord interaction tools for a mention agent.

    All returned tools are bound to the specific channel and guild where the
    mention occurred, ensuring they can only operate in that context.

    Args:
        bot: Discord bot instance (lightbulb.BotApp)
        channel_id: Channel where the mention occurred (string)
        guild_id: Guild where the mention occurred (string)
        trigger_message_id: ID of the message that triggered the mention

    Returns:
        List of callable async functions for the ReAct agent to use
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
        """Search for instant answers and quick facts using DuckDuckGo.

        Returns quick facts and direct answers when available (e.g., definitions, capitals, dates).
        If no instant answer is found, returns None - use search_web for comprehensive results.

        Args:
            query: The question or topic to search for (e.g., "What is photosynthesis?")

        Returns:
            dict with 'success' boolean, 'answer' (str or None), and optional 'source' field

        Example:
            search_web_instant_answer("What is the capital of France?")
            -> {"success": True, "answer": "Paris", "source": "Wikipedia"}
        """
        try:
            logger.debug(f"[Tool] search_web_instant_answer called with query: {query}")

            # Search using DuckDuckGo instant answers
            results = DDGS().answers(query)

            if results and len(results) > 0:
                # Return the first instant answer
                answer_obj = results[0]
                return {
                    "success": True,
                    "answer": answer_obj.get("text", answer_obj.get("answer")),
                    "source": answer_obj.get("source", "DuckDuckGo")
                }
            else:
                # No instant answer available
                return {
                    "success": True,
                    "answer": None,
                    "note": "No instant answer available - use search_web for comprehensive results"
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
            search_results = DDGS().text(query, max_results=max_results)

            if search_results:
                results = [
                    {
                        "title": result.get("title"),
                        "url": result.get("href"),
                        "snippet": result.get("body")
                    }
                    for result in search_results
                ]
                return {
                    "success": True,
                    "results": results,
                    "count": len(results)
                }
            else:
                return {
                    "success": True,
                    "results": [],
                    "count": 0,
                    "note": "No results found for this query"
                }
        except Exception as e:
            logger.error(f"[Tool] search_web failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    # Return list of tools for ReAct agent
    return [
        send_message,
        reply_to_message,
        add_reaction_to_message,
        list_reaction_types,
        search_web_instant_answer,
        search_web
    ]
