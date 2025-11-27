"""Discord bot AI agents module.

This module contains AI agents powered by DSPy for various bot functionalities.
Each agent specializes in a specific type of interaction:

- MentionAgent: Conversational chatbot for @mentions (ReAct-based)
- HelpAgent: Support agent for /help commands (ChainOfThought-based)
- TLDRAgent: Message summarization agent
- ForumMonitorAgent: Forum post evaluation and response agent
- StreakCelebrationAgent: Daily streak celebration message generator
"""

from smarter_dev.bot.agents.mention_agent import MentionAgent
from smarter_dev.bot.agents.help_agent import HelpAgent
from smarter_dev.bot.agents.tldr_agent import TLDRAgent, estimate_message_tokens
from smarter_dev.bot.agents.forum_agent import ForumMonitorAgent
from smarter_dev.bot.agents.streak_agent import StreakCelebrationAgent
from smarter_dev.bot.agents.models import DiscordMessage

__all__ = [
    "MentionAgent",
    "HelpAgent",
    "TLDRAgent",
    "ForumMonitorAgent",
    "StreakCelebrationAgent",
    "DiscordMessage",
    "estimate_message_tokens",
]
