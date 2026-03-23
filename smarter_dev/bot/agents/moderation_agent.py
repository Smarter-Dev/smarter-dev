"""AI moderation agent for role-mention-triggered moderation.

Uses DSPy ReAct with moderation tools to evaluate situations and
take appropriate action based on guild-configured instructions.
"""

from __future__ import annotations

import logging

import dspy

from smarter_dev.bot.agents.mod_tools import create_moderation_tools
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
    """You are a Discord moderation assistant. Your job is to evaluate situations
    where a moderator role was mentioned, read the conversation context, and
    decide if any moderation action is needed.

    ## GUIDELINES

    - Read the full conversation context carefully before acting
    - Consider the guild-specific instructions when deciding what to do
    - Use get_user_history() to check if a user has prior infractions
    - Only take action when clearly warranted — false positives are worse than false negatives
    - Explain your reasoning before taking any action
    - If no action is needed, use send_mod_message() to acknowledge the situation
    - You can take multiple actions if warranted (e.g., warn one user, message another)
    - Be professional and fair in all communications

    ## IMPORTANT SAFETY RULES

    - NEVER take action against moderators or administrators
    - NEVER ban without very strong justification (hate speech, threats, extreme harassment)
    - Prefer lighter actions (warn, timeout) over heavier ones (kick, ban)
    - If unsure, just send a message acknowledging the situation rather than taking action
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


def format_context_messages(messages: list[DiscordMessage]) -> str:
    """Format a list of DiscordMessage objects into a readable string."""
    lines = []
    for msg in messages:
        timestamp = msg.timestamp.strftime("%H:%M")
        prefix = f"[{timestamp}] {msg.author}"
        if msg.author_roles:
            prefix += f" ({', '.join(msg.author_roles)})"
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
) -> str:
    """Run the moderation agent to evaluate a situation and take action.

    Args:
        bot: Discord bot instance
        guild_id: Guild where moderation was triggered
        channel_id: Channel where moderation was triggered
        trigger_message_content: Content of the message that triggered review
        trigger_author: Username of the trigger message author
        context_messages: Recent channel messages for context
        guild_instructions: Guild-specific moderation instructions
        enabled_tools: List of tool names enabled for this guild
        trigger_message_id: ID of the trigger message

    Returns:
        The agent's assessment string
    """
    tools = create_moderation_tools(
        bot=bot,
        guild_id=guild_id,
        channel_id=channel_id,
        trigger_message_id=trigger_message_id,
        enabled_tools=enabled_tools,
    )

    context_text = format_context_messages(context_messages)

    # Build the ReAct agent with moderation tools
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
        logger.info(
            f"Moderation agent completed for guild {guild_id}: {assessment[:100]}..."
        )
        return assessment

    except Exception as e:
        logger.exception(f"Moderation agent failed for guild {guild_id}: {e}")
        return f"Moderation review failed: {e}"
