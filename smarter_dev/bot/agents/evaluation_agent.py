"""Evaluation agent for multi-agent mention pipeline.

This module contains the evaluation agent that:
- Evaluates new messages against watcher criteria
- Decides whether to trigger the response agent
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import dspy

from smarter_dev.llm_config import get_llm_model, get_model_info

logger = logging.getLogger(__name__)

# Configure LLM model - use fast model for evaluation
EVALUATION_LM = get_llm_model("fast")

# Log which model is being used
model_info = get_model_info("fast")
logger.info(
    f"EvaluationAgent using LLM model: {model_info['model_name']} "
    f"(provider: {model_info['provider']})"
)


class EvaluationSignature(dspy.Signature):
    """Evaluate whether new messages are related follow-ups to a watched conversation topic.

    The bot just answered a question and is watching for natural follow-up questions.
    Your job is to determine if new messages are a continuation of that conversation.

    ## should_respond=True when:
    - Message is a follow-up question about what the bot just explained
    - Message asks for clarification, examples, or more detail on the topic
    - Message explores a closely related aspect of the same subject area
    - User wants to go deeper into something the bot mentioned
    - Message naturally continues the conversation thread

    Example: If bot explained "protocols vs inheritance", then "what about composition?"
    or "can you give an example?" or "how does this work in Python?" ARE relevant follow-ups.

    ## should_respond=False when:
    - Message is about a COMPLETELY DIFFERENT subject (not just a different aspect)
    - Message is casual chat, greetings, or off-topic banter
    - Bot is @mentioned for a clearly NEW unrelated question
    - Message is from a different conversation thread entirely

    Example: If bot explained "protocols vs inheritance", then "can someone get me a soda?"
    or "what's the weather?" or "hey @bot explain quantum physics" are NOT relevant.

    ## Key Principle

    Think: "Is this person continuing the conversation about what the bot just discussed?"
    A related follow-up question counts as continuing the conversation, even if it's not
    the exact same sub-topic.

    ## Output

    - should_respond: True for natural follow-ups within the conversation topic
    - relevant_message_ids: Comma-separated IDs (empty if should_respond=False)
    - reasoning: Brief explanation
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

    should_respond: bool = dspy.OutputField(
        description="True if the new messages are relevant and warrant a response"
    )
    relevant_message_ids: str = dspy.OutputField(
        description="Comma-separated list of message IDs that are relevant (empty if should_respond is False)"
    )
    reasoning: str = dspy.OutputField(
        description="1-2 sentence explanation of why this does or doesn't warrant a response"
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
                    bot_id=bot_id
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

            # Estimate tokens
            estimated_tokens = (
                len(watching_for) + len(original_context) + len(new_messages)
            ) // 4 + 50

            logger.debug(
                f"Evaluation result: should_respond={should_respond}, "
                f"relevant_ids={relevant_message_ids}, "
                f"reasoning='{result.reasoning[:100]}...'"
            )

            return EvaluationResult(
                should_respond=should_respond,
                relevant_message_ids=relevant_message_ids,
                reasoning=result.reasoning or "",
                tokens_used=estimated_tokens
            )

        except Exception as e:
            logger.error(f"Evaluation failed: {e}", exc_info=True)
            # Default to not responding on error
            return EvaluationResult(
                should_respond=False,
                relevant_message_ids=[],
                reasoning=f"Evaluation error: {e}",
                tokens_used=0
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
