"""Discord bot AI agents module.

- ChatAgent (Pydantic AI): @mention / reply driven conversational agent
- HelpAgent: Support agent for /help commands (ChainOfThought-based)
- TLDRAgent: Message summarization agent
- ForumMonitorAgent: Forum post evaluation and response agent
- StreakCelebrationAgent: Daily streak celebration message generator
- AoCThreadAgent: Advent of Code thread intro message generator
"""

from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent
from smarter_dev.bot.agents.chat_agent import get_chat_agent
from smarter_dev.bot.agents.chat_models import (
    AgentReturn,
    Author,
    ChannelInfo,
    FollowupAgentInput,
    InitialAgentInput,
    Message,
    NoResponse,
    SendResponse,
)
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.forum_agent import ForumMonitorAgent
from smarter_dev.bot.agents.help_agent import HelpAgent
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.bot.agents.streak_agent import StreakCelebrationAgent
from smarter_dev.bot.agents.tldr_agent import TLDRAgent, estimate_message_tokens

__all__ = [
    "get_chat_agent",
    "ChatDeps",
    "InitialAgentInput",
    "FollowupAgentInput",
    "AgentReturn",
    "Author",
    "ChannelInfo",
    "Message",
    "NoResponse",
    "SendResponse",
    "HelpAgent",
    "TLDRAgent",
    "ForumMonitorAgent",
    "StreakCelebrationAgent",
    "AoCThreadAgent",
    "DiscordMessage",
    "estimate_message_tokens",
]
