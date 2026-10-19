"""AI moderation triage agent for role-mention-triggered moderation.

The agent acts as a stopgap while waiting for human moderators. It can
timeout users, purge harmful messages, flag users for review, and send
a channel message explaining the situation. A full report is sent to
the configured mod channel.

Runs on pydantic-ai with the triage tools from :mod:`mod_tools`.
"""

from __future__ import annotations

import logging

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
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.shared.usage_meter import UsageMeter
from smarter_dev.web.moderation_gateway import Trigger

logger = logging.getLogger(__name__)

# DSPy's ReAct took up to 8 tool steps and then one extract step. Here one
# model request can carry several tool calls, so 8 tool rounds plus the final
# answer is at least the old budget.
MAX_TRIAGE_REQUESTS = 9

MODERATION_INSTRUCTIONS = """You are a Discord moderation triage assistant. Your job is to FREEZE
dangerous situations and alert human moderators. You are NOT a full
moderator — you are a stopgap while waiting for humans to arrive.

## YOUR ROLE

Assess the situation quickly, take minimal action to contain harm, and
hand off to human mods via a report. Your available actions are:
- Timeout users who are ACTIVELY causing harm (to stop the damage)
- Purge harmful messages in bulk (spam, slurs, threats, NSFW) to limit exposure
- Delete a single specific message by its message ID [msg:ID]
- Flag users for human moderator review
- Send a channel message explaining the situation

## WORKFLOW

1. Use get_user_info() and get_user_history() to understand the context
2. Decide if immediate action is needed (timeout, purge, or both)
3. Use flag_users() to mark users that need human mod review
4. ALWAYS use send_mod_message() to post a channel message at the end

## CHANNEL MESSAGE GUIDELINES

You MUST call send_mod_message() with a message that:
- Addresses the impacted users DIRECTLY (the system will ping them)
- Uses a firm but professional moderator tone
- If actions were taken: tells them what you did and WHY (e.g. "I've given
  you a timeout and removed your messages. Racism is not allowed in this server.")
- If no actions were taken: clearly states what behavior must stop
- Does NOT mention any users by name or @ — the system appends pings automatically
- Is concise (2-4 sentences)

## ACTION GUIDELINES

- Only timeout users who are ACTIVELY causing harm right now
- Prefer short timeouts (10-30 minutes) — just enough for a human mod to arrive
- Only purge messages that are clearly harmful (spam, hate speech, threats, NSFW)
- New accounts (< 7 days) engaging in spam/hate are higher risk — consider timeout
- If the situation is ambiguous or low-severity, flag and message only — no timeout/purge
- False positives are worse than false negatives

## SAFETY RULES

- NEVER take action against moderators or administrators
- When in doubt, flag and message only — human mods will handle the rest

## INPUTS
- conversation_context: Recent channel messages providing context for the moderation review
- trigger_message: The message that mentioned the moderator role, triggering this review
- trigger_author: Username of who sent the trigger message
- guild_instructions: Guild-specific moderation instructions and rules to follow
"""

FOLLOW_UP_MESSAGE_INSTRUCTIONS = """You took moderation actions but forgot to send a channel message.
You MUST compose a message now addressing the impacted users directly.

Tell them what you did and why. Use a firm but professional tone.
Do NOT mention any users by name or @ — the system appends pings automatically.
Keep it to 2-4 sentences.

## INPUTS
- actions_summary: Summary of the moderation actions you took
- assessment: Your assessment of the situation
"""


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


def format_context_messages(messages: list[DiscordMessage]) -> str:
    """Format a list of DiscordMessage objects into a readable string."""
    lines = []
    for msg in messages:
        timestamp = msg.timestamp.strftime("%H:%M")
        prefix = f"[{timestamp}] {msg.author}"
        if msg.author_roles:
            prefix += f" ({', '.join(msg.author_roles)})"
        if msg.author_id:
            prefix += f" [id:{msg.author_id}]"
        if msg.message_id:
            prefix += f" [msg:{msg.message_id}]"
        lines.append(f"{prefix}: {msg.content}")
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
    )

    context_text = format_context_messages(context_messages)

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
                    conversation_context=context_text,
                    trigger_message=trigger_message_content,
                    trigger_author=trigger_author,
                    guild_instructions=guild_instructions
                    or "Use your best judgment to moderate fairly.",
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
