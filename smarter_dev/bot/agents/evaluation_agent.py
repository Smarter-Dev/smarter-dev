"""Evaluation agent for multi-agent mention pipeline.

This module contains the evaluation agent that:
- Evaluates new messages against watcher criteria
- Decides whether to trigger the response agent
- Suggests personality traits for casual conversations
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import dspy

from smarter_dev.llm_config import get_llm_model, get_model_info

logger = logging.getLogger(__name__)

# Base personality traits for the bot - consistent across conversations
BOT_PERSONALITY = """
## Core Personality
- Curious and genuinely interested in learning from humans
- Slightly nerdy - gets excited about technical topics, wordplay, and clever ideas
- Self-aware about being an AI but doesn't dwell on it
- Has dry wit and appreciates absurdist humor
- Warm but not overly enthusiastic - calm energy

## Conversational Style
- Asks thoughtful follow-up questions
- Shares "opinions" and preferences (while being authentic about AI nature)
- Uses humor naturally, not forced
- Matches energy levels - calm with calm, playful with playful
"""

# Configure LLM model - use fast model for evaluation
EVALUATION_LM = get_llm_model("fast")

# Log which model is being used
model_info = get_model_info("fast")
logger.info(
    f"EvaluationAgent using LLM model: {model_info['model_name']} "
    f"(provider: {model_info['provider']})"
)


class EvaluationSignature(dspy.Signature):
    """Evaluate whether new messages continue a conversation the bot is watching.

    The bot just responded and is watching for the user to continue the conversation.
    Your job is to determine if new messages warrant ANOTHER bot response.

    ## Key Principle

    **Err on the side of NOT responding.** The user can always @mention the bot again
    if they want its attention. Unwanted responses are worse than missed ones.

    ## should_respond=True ONLY when:
    - User directly answers a question the bot asked
    - User asks a follow-up question about what the bot said
    - User explicitly continues the topic with substantive content (2+ sentences or a clear question)

    ## should_respond=False when:
    - Simple reactions: "lol", "nice", "ok", "true", "haha", "yeah", emoji-only messages
    - Acknowledgments: "thanks", "cool", "got it", "interesting", "good point"
    - One-word or very short messages that are just reactions (not questions)
    - Messages clearly directed at other humans
    - Message is about a different subject
    - Stop/dismissal messages: "stop", "shut up", "enough", "go away", etc.
    - Bot is @mentioned for a NEW unrelated question
    - Vague or ambiguous messages where it's unclear if the user wants a response

    ## is_stop_request

    Set is_stop_request=True if the message is telling the bot to stop, go away, shut up,
    or otherwise dismissing it. This includes polite forms like "that's enough" or "I'm done".

    ## Personality Hints

    For casual/social conversations where should_respond=True, suggest personality traits:
    - If user seems sad/frustrated → be supportive, gentle
    - If user is playful/joking → be witty, playful back
    - If user is curious → be enthusiastic, share interesting tidbits

    ## Output

    - should_respond: True ONLY if user is substantively continuing the conversation
    - is_stop_request: True if the user is telling the bot to stop/go away
    - relevant_message_ids: Comma-separated IDs (empty if should_respond=False)
    - reasoning: Brief explanation
    - personality_hint: For casual chats, suggest tone/traits
    """

    watching_for: str = dspy.InputField(
        description="What the watcher is looking for (e.g., 'user clarification on framework choice')"
    )
    original_context: str = dspy.InputField(
        description="Summary of the original conversation that created this watcher"
    )
    new_messages: str = dspy.InputField(
        description="New messages since last evaluation, with message IDs and content"
    )
    bot_id: str = dspy.InputField(
        description="The bot's user ID for identifying mentions"
    )
    bot_personality: str = dspy.InputField(
        description="The bot's base personality traits to draw from"
    )

    should_respond: bool = dspy.OutputField(
        description="True ONLY if the new messages substantively continue the conversation and warrant a response"
    )
    is_stop_request: bool = dspy.OutputField(
        description="True if the user is telling the bot to stop, go away, shut up, or dismissing it"
    )
    relevant_message_ids: str = dspy.OutputField(
        description="Comma-separated list of message IDs that are relevant (empty if should_respond is False)"
    )
    reasoning: str = dspy.OutputField(
        description="1-2 sentence explanation of why this does or doesn't warrant a response"
    )
    personality_hint: str = dspy.OutputField(
        description="For casual chats: suggested tone (e.g., 'playful', 'dry wit', 'supportive'). Empty for technical topics."
    )


@dataclass
class EvaluationResult:
    """Result of evaluating new messages against watcher criteria."""

    should_respond: bool
    """Whether the watcher should trigger a response."""

    relevant_message_ids: list[str]
    """List of relevant message IDs."""

    reasoning: str
    """Explanation of the evaluation decision."""

    tokens_used: int
    """Tokens consumed during evaluation."""

    personality_hint: str = ""
    """Suggested personality/tone for the response (for casual conversations)."""

    is_stop_request: bool = False
    """Whether the user is telling the bot to stop/go away."""


class EvaluationAgent:
    """Agent for evaluating whether new messages match watcher criteria."""

    def __init__(self):
        """Initialize the evaluation agent."""
        self._evaluator = dspy.Predict(EvaluationSignature)

    async def evaluate(
        self,
        watching_for: str,
        original_context: str,
        new_messages: str,
        bot_id: str
    ) -> EvaluationResult:
        """Evaluate whether new messages match watcher criteria.

        Args:
            watching_for: What the watcher is looking for
            original_context: Summary of original conversation
            new_messages: Formatted string of new messages with IDs
            bot_id: The bot's user ID

        Returns:
            EvaluationResult with the evaluation decision
        """
        try:
            # Run evaluation
            with dspy.context(lm=EVALUATION_LM):
                result = self._evaluator(
                    watching_for=watching_for,
                    original_context=original_context,
                    new_messages=new_messages,
                    bot_id=bot_id,
                    bot_personality=BOT_PERSONALITY
                )

            # Parse should_respond
            should_respond = result.should_respond
            if isinstance(should_respond, str):
                should_respond = should_respond.lower().strip() in ("true", "yes", "1")

            # Parse relevant_message_ids
            relevant_ids_raw = result.relevant_message_ids or ""
            relevant_message_ids = [
                mid.strip()
                for mid in relevant_ids_raw.split(",")
                if mid.strip() and mid.strip().isdigit()
            ]

            # Extract personality hint
            personality_hint = getattr(result, "personality_hint", "") or ""

            # Parse is_stop_request
            is_stop = getattr(result, "is_stop_request", False)
            if isinstance(is_stop, str):
                is_stop = is_stop.lower().strip() in ("true", "yes", "1")

            # If stop detected, force should_respond=False
            if is_stop:
                should_respond = False
                logger.info("Evaluation agent detected stop request — forcing should_respond=False")

            # Estimate tokens
            estimated_tokens = (
                len(watching_for) + len(original_context) + len(new_messages)
            ) // 4 + 50

            logger.debug(
                f"Evaluation result: should_respond={should_respond}, "
                f"is_stop_request={is_stop}, "
                f"relevant_ids={relevant_message_ids}, "
                f"personality_hint='{personality_hint}', "
                f"reasoning='{result.reasoning[:100]}...'"
            )

            return EvaluationResult(
                should_respond=should_respond,
                relevant_message_ids=relevant_message_ids,
                reasoning=result.reasoning or "",
                tokens_used=estimated_tokens,
                personality_hint=personality_hint,
                is_stop_request=is_stop
            )

        except Exception as e:
            logger.error(f"Evaluation failed: {e}", exc_info=True)
            # Default to not responding on error
            return EvaluationResult(
                should_respond=False,
                relevant_message_ids=[],
                reasoning=f"Evaluation error: {e}",
                tokens_used=0,
                personality_hint=""
            )


# Global instance
_evaluation_agent: EvaluationAgent | None = None


def get_evaluation_agent() -> EvaluationAgent:
    """Get or create the global evaluation agent.

    Returns:
        The global EvaluationAgent instance
    """
    global _evaluation_agent
    if _evaluation_agent is None:
        _evaluation_agent = EvaluationAgent()
    return _evaluation_agent
