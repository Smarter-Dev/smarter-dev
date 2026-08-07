"""Discord bot AI agents module.

- ChatAgent (Pydantic AI): @mention / reply driven conversational agent
- HelpAgent: Support agent for /help commands (ChainOfThought-based)
- TLDRAgent: Message summarization agent
- ForumMonitorAgent: Forum post evaluation and response agent
- StreakCelebrationAgent: Daily streak celebration message generator
- AoCThreadAgent: Advent of Code thread intro message generator

Every name is resolved lazily (PEP 562). Half the agents here are DSPy-backed,
and ``dspy`` lives in the ``bot`` dependency group only — but this package also
holds bot-agnostic modules the *web* image imports, notably ``model_router``,
which the nightly dream session needs. Eagerly importing the submodules made
``from smarter_dev.bot.agents.model_router import build_model_for`` drag DSPy in
through this file and die with ``ModuleNotFoundError`` anywhere outside the bot
image. Attribute access still works exactly as before; only the import is
deferred to first use.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent
    from smarter_dev.bot.agents.chat_agent import get_chat_agent
    from smarter_dev.bot.agents.chat_models import (
        AgentReturn,
        Author,
        ChannelInfo,
        FollowupAgentInput,
        InitialAgentInput,
        Message,
        MessageScore,
        ResponseBody,
        TurnDecision,
    )
    from smarter_dev.bot.agents.chat_tools import ChatDeps
    from smarter_dev.bot.agents.forum_agent import ForumMonitorAgent
    from smarter_dev.bot.agents.help_agent import HelpAgent
    from smarter_dev.bot.agents.models import DiscordMessage
    from smarter_dev.bot.agents.streak_agent import StreakCelebrationAgent
    from smarter_dev.bot.agents.tldr_agent import TLDRAgent, estimate_message_tokens

# Exported name -> the submodule that defines it.
_EXPORTS = {
    "AoCThreadAgent": "aoc_thread_agent",
    "get_chat_agent": "chat_agent",
    "AgentReturn": "chat_models",
    "Author": "chat_models",
    "ChannelInfo": "chat_models",
    "FollowupAgentInput": "chat_models",
    "InitialAgentInput": "chat_models",
    "Message": "chat_models",
    "MessageScore": "chat_models",
    "ResponseBody": "chat_models",
    "TurnDecision": "chat_models",
    "ChatDeps": "chat_tools",
    "ForumMonitorAgent": "forum_agent",
    "HelpAgent": "help_agent",
    "DiscordMessage": "models",
    "StreakCelebrationAgent": "streak_agent",
    "TLDRAgent": "tldr_agent",
    "estimate_message_tokens": "tldr_agent",
}


def __getattr__(name: str) -> object:
    """Import the defining submodule on first access to ``name``."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value  # cache, so this runs once per name
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_EXPORTS])

__all__ = [
    "get_chat_agent",
    "ChatDeps",
    "InitialAgentInput",
    "FollowupAgentInput",
    "AgentReturn",
    "Author",
    "ChannelInfo",
    "Message",
    "MessageScore",
    "ResponseBody",
    "TurnDecision",
    "HelpAgent",
    "TLDRAgent",
    "ForumMonitorAgent",
    "StreakCelebrationAgent",
    "AoCThreadAgent",
    "DiscordMessage",
    "estimate_message_tokens",
]
