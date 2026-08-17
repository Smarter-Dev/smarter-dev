"""Parity tools for replay evals: real web tools, honest stubs for the rest.

The production proactive agent carries all 11 chat tools. In fixture and
scenario replays there is no live Discord, bot API, or media service, so:

- ``web_search`` / ``web_read`` run for real (they only need the internet;
  their in-channel status posts are best-effort and degrade silently with
  the replay's null bot);
- every other tool is a same-named stub that returns an honest
  "unavailable in replay" string — the agent sees the same tool surface as
  production, and the run record shows when it *would* have acted.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from smarter_dev.bot.agents.chat_tools import web_read, web_search  # noqa: E402
from smarter_dev.bot.proactive.parity import _budgeted  # noqa: E402

_UNAVAILABLE = "This tool is unavailable in replay evals; in production it {}."


async def list_available_reactions(ctx) -> str:
    """List the emojis the bot may react with."""
    return _UNAVAILABLE.format("lists the guild's emojis")


async def add_reaction(ctx, message_id: str, emoji: str) -> str:
    """Add an emoji reaction to a message."""
    return _UNAVAILABLE.format("adds the reaction via Discord")


async def report_behavior(ctx, user_id: str, reason: str) -> str:
    """Escalate concerning behavior to the moderation team."""
    return _UNAVAILABLE.format("escalates to the mod team")


async def run_code(ctx, reason: str, code: str) -> str:
    """Execute code in the bot's sandbox."""
    return _UNAVAILABLE.format("executes code in the bot's sandbox")


async def generate_image(ctx, prompt: str) -> str:
    """Generate an image to attach to the reply."""
    return _UNAVAILABLE.format("generates an image")


async def remember(ctx, text: str) -> str:
    """Save a durable memory note about this channel."""
    return _UNAVAILABLE.format("saves a memory note")


async def register_handler(ctx, trigger_type: str, description: str) -> str:
    """Register a channel automation handler."""
    return _UNAVAILABLE.format("registers a channel automation")


async def list_handlers(ctx) -> str:
    """List this channel's automation handlers."""
    return _UNAVAILABLE.format("lists channel automations")


async def delete_handler(ctx, handler_id: str) -> str:
    """Delete a channel automation handler."""
    return _UNAVAILABLE.format("deletes a channel automation")


def replay_parity_tools() -> list:
    """The production tool surface, replay-safe, budget-wrapped."""
    return [
        _budgeted(tool)
        for tool in (
            web_search,
            web_read,
            list_available_reactions,
            add_reaction,
            report_behavior,
            run_code,
            generate_image,
            remember,
            register_handler,
            list_handlers,
            delete_handler,
        )
    ]
