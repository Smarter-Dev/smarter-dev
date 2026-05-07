"""AI moderation triage agent for role-mention-triggered moderation.

The agent acts as a stopgap while waiting for human moderators. It can
timeout users, purge harmful messages, flag users for review, and send
a channel message explaining the situation. A full report is sent to
the configured mod channel.

Uses DSPy ReAct with triage tools.
"""

from __future__ import annotations

import logging

import dspy

from smarter_dev.bot.agents.mod_tools import ActionTracker, create_moderation_tools
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.llm_config import get_llm_model, get_model_info

logger = logging.getLogger(__name__)

# Use medium model for moderation decisions (needs good judgment)
MODERATION_LM = get_llm_model("medium")

model_info = get_model_info("medium")
logger.info(
    f"ModerationAgent using LLM model: {model_info['model_name']} "
    f"(provider: {model_info['provider']})"
)


class ModerationSignature(dspy.Signature):
    """You are a Discord moderation triage assistant. Your job is to FREEZE
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
    """

    conversation_context: str = dspy.InputField(
        desc="Recent channel messages providing context for the moderation review"
    )
    trigger_message: str = dspy.InputField(
        desc="The message that mentioned the moderator role, triggering this review"
    )
    trigger_author: str = dspy.InputField(
        desc="Username of who sent the trigger message"
    )
    guild_instructions: str = dspy.InputField(
        desc="Guild-specific moderation instructions and rules to follow"
    )
    assessment: str = dspy.OutputField(
        desc="Your assessment of the situation and what action (if any) you took"
    )


class FollowUpMessageSignature(dspy.Signature):
    """You took moderation actions but forgot to send a channel message.
    You MUST compose a message now addressing the impacted users directly.

    Tell them what you did and why. Use a firm but professional tone.
    Do NOT mention any users by name or @ — the system appends pings automatically.
    Keep it to 2-4 sentences.
    """

    actions_summary: str = dspy.InputField(
        desc="Summary of the moderation actions you took"
    )
    assessment: str = dspy.InputField(
        desc="Your assessment of the situation"
    )
    message: str = dspy.OutputField(
        desc="The channel message to send to impacted users"
    )


async def _enforce_channel_message(
    tracker: ActionTracker, assessment: str, lm: dspy.LM
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
        predict = dspy.Predict(FollowUpMessageSignature)
        with dspy.context(lm=lm):
            result = await dspy.asyncify(predict)(
                actions_summary=actions_summary,
                assessment=assessment,
            )
        tracker.channel_message = result.message
    except Exception:
        logger.exception("Failed to generate follow-up channel message")
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
        enabled_tools: List of tool names enabled for this guild
        trigger_message_id: ID of the trigger message

    Returns:
        Tuple of (assessment string, ActionTracker with all actions taken)
    """
    tools, tracker = create_moderation_tools(
        bot=bot,
        guild_id=guild_id,
        channel_id=channel_id,
        trigger_message_id=trigger_message_id,
        enabled_tools=enabled_tools,
    )

    context_text = format_context_messages(context_messages)

    # Build the ReAct agent with triage tools
    react_agent = dspy.ReAct(
        ModerationSignature,
        tools=tools,
        max_iters=8,
    )

    try:
        with dspy.context(lm=MODERATION_LM):
            result = await dspy.asyncify(react_agent)(
                conversation_context=context_text,
                trigger_message=trigger_message_content,
                trigger_author=trigger_author,
                guild_instructions=guild_instructions or "Use your best judgment to moderate fairly.",
            )

        assessment = result.assessment

        # Enforce: if actions were taken, a channel message MUST be sent.
        # Give the agent one more turn with only send_mod_message available.
        if tracker.has_actions and not tracker.channel_message:
            logger.warning(
                f"Agent took actions but didn't send a channel message for guild {guild_id}. "
                "Running follow-up to generate message."
            )
            await _enforce_channel_message(tracker, assessment, MODERATION_LM)

        logger.info(
            f"Moderation triage completed for guild {guild_id}: {assessment[:100]}..."
        )
        return assessment, tracker

    except Exception as e:
        logger.exception(f"Moderation triage failed for guild {guild_id}: {e}")
        return f"Moderation triage failed: {e}", tracker
