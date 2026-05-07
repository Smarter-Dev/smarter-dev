"""Discord bot AI agents module.

This module contains AI agents powered by DSPy for various bot functionalities.
Each agent specializes in a specific type of interaction:

- ClassificationAgent: Classifies @mentions and determines intent
- ResponseAgent: Generates responses with structured flow control
- EvaluationAgent: Evaluates new messages for watcher triggers
- HelpAgent: Support agent for /help commands (ChainOfThought-based)
- TLDRAgent: Message summarization agent
- ForumMonitorAgent: Forum post evaluation and response agent
- StreakCelebrationAgent: Daily streak celebration message generator
- AoCThreadAgent: Advent of Code thread intro message generator
"""

from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent
from smarter_dev.bot.agents.classification_agent import ClassificationAgent
from smarter_dev.bot.agents.evaluation_agent import EvaluationAgent
from smarter_dev.bot.agents.forum_agent import ForumMonitorAgent
from smarter_dev.bot.agents.help_agent import HelpAgent
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.bot.agents.response_agent import ResponseAgent
from smarter_dev.bot.agents.streak_agent import StreakCelebrationAgent
from smarter_dev.bot.agents.tldr_agent import TLDRAgent
from smarter_dev.bot.agents.tldr_agent import estimate_message_tokens
from smarter_dev.bot.agents.watcher import ResponseAgentOutput
from smarter_dev.bot.agents.watcher import UpdateFrequency
from smarter_dev.bot.agents.watcher import Watcher
from smarter_dev.bot.agents.watcher import WatcherContext

__all__ = [
    "ClassificationAgent",
    "ResponseAgent",
    "EvaluationAgent",
    "Watcher",
    "WatcherContext",
    "UpdateFrequency",
    "ResponseAgentOutput",
    "HelpAgent",
    "TLDRAgent",
    "ForumMonitorAgent",
    "StreakCelebrationAgent",
    "AoCThreadAgent",
    "DiscordMessage",
    "estimate_message_tokens",
]
