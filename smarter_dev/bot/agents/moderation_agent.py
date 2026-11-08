"""AI moderation triage agent for role-mention-triggered moderation.

The agent acts as a stopgap while waiting for human moderators. It can
timeout users, purge harmful messages, cite the server's rules, flag users
for review, and send a channel message explaining the situation. A full
report is sent to the configured mod channel.

Runs on pydantic-ai with the triage tools from :mod:`mod_tools`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent
from pydantic_ai import UsageLimits
from pydantic_ai.toolsets import FunctionToolset

from smarter_dev.bot.agents.mod_tools import ActionTracker
from smarter_dev.bot.agents.mod_tools import create_moderation_tools
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.bot.agents.utility_model import build_utility_agent
from smarter_dev.bot.agents.utility_model import render_inputs
from smarter_dev.bot.agents.utility_model import utility_metered
from smarter_dev.bot.plugins.rules import MAX_RULES_PROMPT_CHARS
from smarter_dev.bot.plugins.rules import load_guild_rules
from smarter_dev.bot.plugins.rules import rules_within_prompt_budget
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.shared.usage_meter import UsageMeter
from smarter_dev.web.guild_rules import GuildRule
from smarter_dev.web.guild_rules import format_rules_for_prompt
from smarter_dev.web.moderation_gateway import Trigger

logger = logging.getLogger(__name__)

# DSPy's ReAct took up to 8 tool steps and then one extract step. Here one
# model request can carry several tool calls, so 8 tool rounds plus the final
# answer is at least the old budget.
MAX_TRIAGE_REQUESTS = 9

MODERATION_INSTRUCTIONS = (
    Path(__file__).parent / "prompts" / "moderation_agent_moderation_instructions.md"
).read_text(encoding="utf-8")

FOLLOW_UP_MESSAGE_INSTRUCTIONS = (
    Path(__file__).parent / "prompts" / "moderation_agent_follow_up_message_instructions.md"
).read_text(encoding="utf-8")


class ModerationAssessment(BaseModel):
    assessment: str = Field(
        description="Your assessment of the situation and what action (if any) you took"
    )


class FollowUpMessage(BaseModel):
    message: str = Field(description="The channel message to send to impacted users")


_moderation_agent: Agent[None, ModerationAssessment] | None = None
_follow_up_agent: Agent[None, FollowUpMessage] | None = None


def get_moderation_agent() -> Agent[None, ModerationAssessment]:
    """Return the singleton triage agent; each run brings its own tools."""
    global _moderation_agent
    if _moderation_agent is None:
        # Triage loops over its moderation tools, so its requests share the
        # growing history: keep the message cache breakpoint.
        _moderation_agent = build_utility_agent(
            ModerationAssessment, MODERATION_INSTRUCTIONS, cache_messages=True
        )
    return _moderation_agent


def get_follow_up_agent() -> Agent[None, FollowUpMessage]:
    """Return the singleton follow-up message agent, building it on first use."""
    global _follow_up_agent
    if _follow_up_agent is None:
        _follow_up_agent = build_utility_agent(
            FollowUpMessage, FOLLOW_UP_MESSAGE_INSTRUCTIONS
        )
    return _follow_up_agent


async def _enforce_channel_message(
    tracker: ActionTracker, assessment: str, meter: UsageMeter
) -> None:
    """Generate a channel message when the agent took actions but didn't send one."""
    # Build a summary of what was done
    parts = []
    for t in tracker.timeouts:
        parts.append(f"Timed out {t['username']} for {t['duration']}: {t['reason']}")
    for p in tracker.purges:
        parts.append(f"Purged {p['count']} messages from {p['username']}: {p['reason']}")
    for d in tracker.deletions:
        parts.append(f"Deleted message {d['message_id']}: {d['reason']}")
    for c in tracker.cites:
        listed = ", ".join(str(index) for index in c["rule_indexes"])
        verb = "Possibly cited" if c.get("possibly_applied") else "Cited"
        parts.append(f"{verb} rule(s) {listed} on message {c['message_id']}: {c['reason']}")
    actions_summary = "\n".join(parts) if parts else "Actions were taken."

    try:
        agent = get_follow_up_agent()
        result = await agent.run(
            render_inputs(actions_summary=actions_summary, assessment=assessment),
            model=meter.wrap(agent.model),
        )
        tracker.channel_message = result.output.message.strip()
    except Exception:
        log_exception(logger, "Failed to generate follow-up channel message")
        # Fallback: generic message
        tracker.channel_message = (
            "Moderation action has been taken. A human moderator will review "
            "the situation shortly."
        )


async def load_triage_rules(guild_id: str) -> tuple[GuildRule, ...]:
    """The guild's rules as the triage is shown them, capped like ``/rule``'s.

    Empty when none are configured or they cannot be read: triage then runs
    as it did before, without ``cite_rule``.
    """
    try:
        configured = await load_guild_rules(guild_id)
    except Exception:
        log_exception(logger, f"Could not load rules for guild {guild_id}")
        return ()
    rules, dropped = rules_within_prompt_budget(configured, MAX_RULES_PROMPT_CHARS)
    if dropped:
        logger.warning(
            f"Guild {guild_id} has {dropped} rule(s) past the "
            f"{MAX_RULES_PROMPT_CHARS}-character prompt cap; triage was not shown them"
        )
    return rules


REPLY_SNIPPET_CHARS = 80


def _age(now: datetime, then: datetime) -> str:
    seconds = max(int((now - then).total_seconds()), 0)
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def format_context_messages(
    messages: list[DiscordMessage], now: datetime | None = None
) -> str:
    """Format a list of DiscordMessage objects into a readable string.

    Each line carries the full UTC date and time; with ``now`` it also carries
    how long before the ping it was posted. A reply shows who and what it
    replies to on the line above it.
    """
    lines = []
    for msg in messages:
        stamp = msg.timestamp.strftime("%Y-%m-%d %H:%M UTC")
        if now is not None:
            stamp += f", {_age(now, msg.timestamp)}"
        prefix = f"[{stamp}] {msg.author}"
        if msg.author_roles:
            prefix += f" ({', '.join(msg.author_roles)})"
        if msg.author_id:
            prefix += f" [id:{msg.author_id}]"
        if msg.message_id:
            prefix += f" [msg:{msg.message_id}]"
        if msg.replied_to_author is not None:
            snippet = " ".join((msg.replied_to_content or "").split())
            if len(snippet) > REPLY_SNIPPET_CHARS:
                snippet = snippet[:REPLY_SNIPPET_CHARS] + "..."
            lines.append(f"  (reply to {msg.replied_to_author}: {snippet})")
        content = " ".join(msg.content.split())
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines)


async def run_moderation_agent(
    bot,
    guild_id: str,
    channel_id: str,
    trigger_message_content: str,
    trigger_author: str,
    context_messages: list[DiscordMessage],
    guild_instructions: str,
    enabled_tools: list[str],
    trigger_message_id: str | None = None,
    trigger: Trigger | None = None,
    trigger_author_id: str | None = None,
    trigger_author_display_name: str | None = None,
    guild_rules: Sequence[GuildRule] = (),
    enforcing: bool = False,
) -> tuple[str, ActionTracker]:
    """Run the moderation triage agent to evaluate a situation and take action.

    Args:
        bot: Discord bot instance
        guild_id: Guild where moderation was triggered
        channel_id: Channel where moderation was triggered (incident channel)
        trigger_message_content: Content of the message that triggered review
        trigger_author: Username of the trigger message author
        context_messages: Recent channel messages for context
        guild_instructions: Guild-specific moderation instructions
        enabled_tools: Destructive tools enabled for this guild; empty means none
        trigger_message_id: ID of the trigger message
        trigger: The ping as the moderation gateway's trigger (its incident)
        trigger_author_id: Discord user ID of the trigger message author
        trigger_author_display_name: Their display name, if known
        guild_rules: The guild's rules, numbered; they go into the prompt
        enforcing: Triage is enforcing; with rules, this offers ``cite_rule``

    Returns:
        Tuple of (assessment string, ActionTracker with all actions taken)
    """
    tools, tracker = create_moderation_tools(
        bot=bot,
        guild_id=guild_id,
        channel_id=channel_id,
        trigger_message_id=trigger_message_id,
        enabled_tools=enabled_tools,
        trigger=trigger,
        rules=guild_rules,
        context_messages=context_messages,
        enforcing=enforcing,
    )

    now = datetime.now(UTC)
    context_text = format_context_messages(context_messages, now=now)

    try:
        # Triage and its follow-up are one ledger operation; every request
        # either makes is reported, a run cut off by the request limit or an
        # error included (the block sees the exception before the handler below).
        async with utility_metered(
            "moderation_triage", guild_id=guild_id, channel_id=channel_id
        ) as meter:
            agent = get_moderation_agent()
            result = await agent.run(
                render_inputs(
                    current_time=now.strftime("%Y-%m-%d %H:%M UTC"),
                    conversation_context=context_text,
                    trigger_message=trigger_message_content,
                    trigger_author=trigger_author,
                    trigger_author_display_name=trigger_author_display_name
                    or trigger_author,
                    trigger_author_id=trigger_author_id or "unknown",
                    guild_instructions=guild_instructions
                    or "Use your best judgment to moderate fairly.",
                    server_rules=format_rules_for_prompt(list(guild_rules))
                    or "No server rules are configured.",
                ),
                model=meter.wrap(agent.model),
                toolsets=[FunctionToolset(tools)],
                usage_limits=UsageLimits(request_limit=MAX_TRIAGE_REQUESTS),
            )

            assessment = result.output.assessment

            # Enforce: if actions were taken, a channel message MUST be sent.
            # Give the agent one more turn with only send_mod_message available.
            if tracker.has_actions and not tracker.channel_message:
                logger.warning(
                    f"Agent took actions but didn't send a channel message for guild {guild_id}. "
                    "Running follow-up to generate message."
                )
                await _enforce_channel_message(tracker, assessment, meter)

        logger.info(
            f"Moderation triage completed for guild {guild_id}: {assessment[:100]}..."
        )
        return assessment, tracker

    except Exception as e:
        log_exception(logger, f"Moderation triage failed for guild {guild_id}: {type(e).__name__}")
        # Only the error class reaches the mod channel; the message can carry
        # provider internals. Not retried: actions may already have run.
        tracker.failure = type(e).__name__
        return f"Moderation triage failed ({tracker.failure}).", tracker
