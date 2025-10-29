"""Conversation relevance checker for determining if bot should respond during watching periods.

This module uses a lightweight DSPy module to analyze conversation context and decide
whether the bot should participate in the ongoing discussion.
"""

from __future__ import annotations

import logging
import dspy
from typing import Optional, Tuple

from smarter_dev.llm_config import get_llm_model, get_model_info

logger = logging.getLogger(__name__)

# Configure LLM model from environment
lm = get_llm_model("default")
dspy.configure(lm=lm, track_usage=True)

# Log which model is being used
model_info = get_model_info("default")
logger.info(f"🤖 RelevanceChecker using LLM model: {model_info['model_name']} (provider: {model_info['provider']})")


class ConversationRelevanceSignature(dspy.Signature):
    """Analyze if the bot should participate in an ongoing Discord conversation.

    You're watching a Discord conversation unfold. Based on the recent messages,
    determine if you (the bot) should add a comment or response.

    Consider:
    - Are users asking questions that might benefit from your perspective?
    - Is there active discussion about topics you can meaningfully contribute to?
    - Would your input feel natural and add value, or would it interrupt?
    - Are users directly or indirectly inviting participation (e.g., asking for opinions, ideas, explanations)?

    You should respond "yes" if:
    - There are unanswered questions you could help with
    - The discussion would benefit from your unique perspective
    - Your participation would feel natural, not forced

    You should respond "no" if:
    - The conversation is a private discussion between specific users
    - The topic isn't something you can meaningfully contribute to
    - It's clear the users don't need help and are just chatting
    - Your input would feel like interrupting or being nosey
    """
    conversation_context: str = dspy.InputField(
        desc="Recent messages from the Discord channel, formatted with timestamps and authors"
    )
    should_respond: bool = dspy.OutputField(
        desc="True if bot should respond, False otherwise"
    )
    reasoning: str = dspy.OutputField(
        desc="Brief explanation of why the bot should or shouldn't respond"
    )


class ConversationRelevanceChecker:
    """Check if bot should respond based on conversation context."""

    def __init__(self):
        """Initialize the relevance checker."""
        self.module = dspy.ChainOfThought(ConversationRelevanceSignature)
        logger.info("ConversationRelevanceChecker initialized")

    async def should_respond(self, conversation_context: str) -> Tuple[bool, str]:
        """Determine if bot should respond to the conversation.

        Args:
            conversation_context: Formatted conversation context (recent messages)

        Returns:
            Tuple of (should_respond: bool, reasoning: str)
        """
        try:
            result = await self._call_module_async(conversation_context)
            should_respond = result.should_respond
            reasoning = result.reasoning

            logger.debug(
                f"Relevance check: {'should respond' if should_respond else 'should not respond'} - {reasoning}"
            )
            return should_respond, reasoning

        except Exception as e:
            logger.error(f"Error checking conversation relevance: {e}", exc_info=True)
            # Default to not responding if there's an error
            return False, f"Error during relevance check: {e}"

    def _call_module_sync(self, conversation_context: str) -> dspy.Prediction:
        """Call the DSPy module synchronously.

        Args:
            conversation_context: Formatted conversation context

        Returns:
            DSPy prediction with should_respond and reasoning
        """
        return self.module(conversation_context=conversation_context)

    async def _call_module_async(self, conversation_context: str) -> dspy.Prediction:
        """Call the DSPy module asynchronously.

        Args:
            conversation_context: Formatted conversation context

        Returns:
            DSPy prediction with should_respond and reasoning
        """
        # DSPy doesn't have native async support, so we run it in a thread pool
        import asyncio

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._call_module_sync, conversation_context)


# Global instance
_relevance_checker: Optional[ConversationRelevanceChecker] = None


def get_relevance_checker() -> ConversationRelevanceChecker:
    """Get or create the global relevance checker.

    Returns:
        The global ConversationRelevanceChecker instance
    """
    global _relevance_checker
    if _relevance_checker is None:
        _relevance_checker = ConversationRelevanceChecker()
    return _relevance_checker


def initialize_relevance_checker() -> ConversationRelevanceChecker:
    """Initialize the global relevance checker.

    Returns:
        The initialized ConversationRelevanceChecker instance
    """
    global _relevance_checker
    _relevance_checker = ConversationRelevanceChecker()
    return _relevance_checker
