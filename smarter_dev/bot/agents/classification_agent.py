"""Classification agent for multi-agent mention pipeline.

This module contains:
- ClassificationSignature: Determines if bot should respond and extracts context
- WatcherMatchSignature: Matches new mentions to existing watchers
- ClassificationAgent: Agent for classifying incoming mentions
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import dspy

from smarter_dev.llm_config import get_llm_model, get_model_info

logger = logging.getLogger(__name__)

# Configure LLM model - use fast model for classification
CLASSIFICATION_LM = get_llm_model("fast")

# Log which model is being used
model_info = get_model_info("fast")
logger.info(
    f"ClassificationAgent using LLM model: {model_info['model_name']} "
    f"(provider: {model_info['provider']})"
)


class ClassificationSignature(dspy.Signature):
    """Classify an incoming @mention to determine if the bot should respond.

    You analyze Discord conversations to determine:
    1. Is this message actually directed AT the bot (vs. ABOUT the bot)?
    2. What is the user's intent?
    3. Which messages are relevant to the request?

    ## Message Directionality

    **FOR the bot (should_respond=True):**
    - Direct questions, requests, or commands to the bot
    - The bot is being asked to do something or answer something
    - The conversation is with the bot as a participant

    **ABOUT the bot (should_respond=False):**
    - Message replies to someone else but mentions the bot in passing
    - Someone is citing or referencing what the bot said to another person
    - The bot is being discussed but not addressed
    - Mental health crisis, illegal activity, or safety emergency mentions

    **Key signal:** If a message replies to User A and mentions the bot, it's probably
    directed at User A, not the bot.

    ## Output Guidelines

    - should_respond: True only if the message is directed AT the bot
    - intent: 1-2 sentences describing what the user is asking/wanting
    - relevant_message_ids: Comma-separated list of message IDs that provide context
    - context_summary: 2-3 sentences summarizing the relevant conversation context
    """

    conversation_timeline: str = dspy.InputField(
        description="Chronological conversation timeline with message IDs, timestamps, authors, and content"
    )
    trigger_message_id: str = dspy.InputField(
        description="The message ID that triggered this mention (the one containing the @mention)"
    )
    bot_id: str = dspy.InputField(
        description="The bot's user ID for identifying self-references"
    )

    should_respond: bool = dspy.OutputField(
        description="True if the message is directed AT the bot and requires a response, False if it's ABOUT the bot or doesn't need response"
    )
    intent: str = dspy.OutputField(
        description="1-2 sentence description of what the user is asking or wanting (empty if should_respond is False)"
    )
    relevant_message_ids: str = dspy.OutputField(
        description="Comma-separated list of message IDs that are relevant to this request (from the timeline's [ID: ...] prefixes)"
    )
    context_summary: str = dspy.OutputField(
        description="2-3 sentence summary of the relevant conversation context"
    )


class WatcherMatchSignature(dspy.Signature):
    """Match a new mention to an existing watcher if they're about the same topic.

    You compare a new @mention request against a list of active watchers to determine
    if the new request is a continuation of an existing conversation topic.

    ## Matching Criteria

    A new mention MATCHES an existing watcher if:
    - It's clearly a follow-up to the same conversation topic
    - The user is asking for clarification or expansion on what the watcher is monitoring
    - The intent relates directly to what the watcher is "watching_for"

    A new mention does NOT match if:
    - It's a completely new topic or question
    - It's from a different conversation thread
    - The intent is unrelated to what any watcher is monitoring

    ## Output

    Return the watcher ID that matches, or "NONE" if no match.
    """

    new_intent: str = dspy.InputField(
        description="The intent extracted from the new mention"
    )
    new_context_summary: str = dspy.InputField(
        description="Context summary from the new mention"
    )
    watchers_info: str = dspy.InputField(
        description="JSON list of active watchers with id and watching_for fields"
    )

    matched_watcher_id: str = dspy.OutputField(
        description="The ID of the matching watcher, or 'NONE' if no match"
    )
    match_reasoning: str = dspy.OutputField(
        description="Brief explanation of why this watcher matches or why none match"
    )


@dataclass
class ClassificationResult:
    """Result of classifying an incoming mention."""

    should_respond: bool
    """Whether the bot should respond to this mention."""

    intent: str
    """Description of user's intent."""

    relevant_message_ids: list[str]
    """List of relevant message IDs."""

    context_summary: str
    """Summary of relevant context."""

    matched_watcher_id: str | None
    """ID of matched watcher, or None if no match."""

    tokens_used: int
    """Tokens consumed during classification."""


class ClassificationAgent:
    """Agent for classifying incoming @mentions."""

    def __init__(self):
        """Initialize the classification agent."""
        self._classifier = dspy.Predict(ClassificationSignature)
        self._watcher_matcher = dspy.Predict(WatcherMatchSignature)

    async def classify(
        self,
        conversation_timeline: str,
        trigger_message_id: str,
        bot_id: str,
        active_watchers: list[dict[str, str]] | None = None
    ) -> ClassificationResult:
        """Classify an incoming mention.

        Args:
            conversation_timeline: The conversation context
            trigger_message_id: ID of the message that triggered this
            bot_id: The bot's user ID
            active_watchers: Optional list of active watchers for matching

        Returns:
            ClassificationResult with classification and optional watcher match
        """
        try:
            # Run classification
            with dspy.context(lm=CLASSIFICATION_LM):
                result = self._classifier(
                    conversation_timeline=conversation_timeline,
                    trigger_message_id=trigger_message_id,
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

            # Check for watcher match if we should respond and have watchers
            matched_watcher_id = None
            if should_respond and active_watchers:
                matched_watcher_id = await self._match_watcher(
                    intent=result.intent,
                    context_summary=result.context_summary,
                    watchers=active_watchers
                )

            # Estimate tokens (DSPy doesn't always provide accurate counts)
            estimated_tokens = len(conversation_timeline) // 4 + 100

            return ClassificationResult(
                should_respond=should_respond,
                intent=result.intent or "",
                relevant_message_ids=relevant_message_ids,
                context_summary=result.context_summary or "",
                matched_watcher_id=matched_watcher_id,
                tokens_used=estimated_tokens
            )

        except Exception as e:
            logger.error(f"Classification failed: {e}", exc_info=True)
            # Default to not responding on error
            return ClassificationResult(
                should_respond=False,
                intent="",
                relevant_message_ids=[],
                context_summary="",
                matched_watcher_id=None,
                tokens_used=0
            )

    async def _match_watcher(
        self,
        intent: str,
        context_summary: str,
        watchers: list[dict[str, str]]
    ) -> str | None:
        """Match a mention to an existing watcher.

        Args:
            intent: The intent from classification
            context_summary: The context summary
            watchers: List of watcher dicts with 'id' and 'watching_for'

        Returns:
            Matched watcher ID or None
        """
        if not watchers:
            return None

        try:
            import json

            watchers_info = json.dumps([
                {"id": w["id"], "watching_for": w["watching_for"]}
                for w in watchers
            ])

            with dspy.context(lm=CLASSIFICATION_LM):
                result = self._watcher_matcher(
                    new_intent=intent,
                    new_context_summary=context_summary,
                    watchers_info=watchers_info
                )

            matched_id = result.matched_watcher_id.strip()

            if matched_id.upper() == "NONE" or not matched_id:
                logger.debug(f"No watcher match: {result.match_reasoning}")
                return None

            # Verify the returned ID is actually in our watcher list
            valid_ids = {w["id"] for w in watchers}
            if matched_id in valid_ids:
                logger.info(f"Matched watcher {matched_id}: {result.match_reasoning}")
                return matched_id
            else:
                logger.warning(f"Returned watcher ID {matched_id} not in valid set")
                return None

        except Exception as e:
            logger.error(f"Watcher matching failed: {e}", exc_info=True)
            return None


# Global instance
_classification_agent: ClassificationAgent | None = None


def get_classification_agent() -> ClassificationAgent:
    """Get or create the global classification agent.

    Returns:
        The global ClassificationAgent instance
    """
    global _classification_agent
    if _classification_agent is None:
        _classification_agent = ClassificationAgent()
    return _classification_agent
