"""Discord bot AI agents module.

This module contains AI agents powered by DSPy for various bot functionalities.
Each agent specializes in a specific type of interaction:

- MentionAgent: Conversational chatbot for @mentions (ReAct-based)
- HelpAgent: Support agent for /help commands (ChainOfThought-based)
"""

from smarter_dev.bot.agents.mention_agent import MentionAgent
from smarter_dev.bot.agents.help_agent import HelpAgent
from smarter_dev.bot.agents.models import DiscordMessage

__all__ = [
    "MentionAgent",
    "HelpAgent",
    "DiscordMessage",
]
