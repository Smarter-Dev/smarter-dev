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
    """Evaluate whether new messages continue a conversation the bot is watching.

    The bot just responded and is watching for the user to continue the conversation.
    Your job is to determine if new messages are a continuation - this includes BOTH
    questions AND statements/replies.

    ## should_respond=True when:
    - User replies to what the bot said (even just a statement or reaction)
    - User answers a question the bot asked them
    - User shares their thoughts on the topic being discussed
    - User asks a follow-up question
    - User wants to continue chatting about the same topic
    - Message naturally continues the conversation thread

    Example: If bot asked "What do YOU think ice cream tastes like?", then:
    - "I think it tastes sweet and creamy" → RESPOND (answering the bot's question)
    - "vanilla is my favorite" → RESPOND (continuing the topic)
    - "lol good question" → RESPOND (engaging with the conversation)

    ## should_respond=False when:
    - Message is about a COMPLETELY DIFFERENT subject
    - Message is clearly directed at someone else
    - Bot is @mentioned for a NEW unrelated question
    - Message is from a different conversation thread entirely

    Example: "can someone get me a soda?" or "hey @bot explain quantum physics" are NOT relevant.

    ## Key Principle

    If the user is engaging with the conversation in ANY way (question, answer, statement,
    reaction), respond. Only ignore messages that are clearly unrelated or directed elsewhere.

    ## Output

    - should_respond: True if user is continuing the conversation
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
