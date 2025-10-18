"""Tools available to Discord bot agents via ReAct pattern.

These tools enable agents to take Discord interaction actions within the
context-bound channel and guild where they were mentioned. All tools are
created as a factory to ensure they can only operate in their intended context.
"""

import logging
from typing import Callable, List

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

        Args:
            content: Message content to send

        Returns:
            dict with 'success' and 'result' or 'error' keys
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
        """Reply to a specific message in the current channel.

        Args:
            message_id: ID of the message to reply to
            content: Reply content

        Returns:
            dict with 'success' and 'result' or 'error' keys
        """
        try:
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

        Args:
            message_id: ID of the message to react to
            emoji: Emoji to add (Unicode emoji, e.g., "👍" or custom emoji ID)

        Returns:
            dict with 'success' and 'result' or 'error' keys
        """
        try:
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

        Returns:
            dict with 'success' and 'emoji_list' or 'error' keys
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

    # Return list of tools for ReAct agent
    return [
        send_message,
        reply_to_message,
        add_reaction_to_message,
        list_reaction_types
    ]
