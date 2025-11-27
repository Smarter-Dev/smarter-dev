"""Streak celebration agent for generating fun daily streak bonus messages."""

from __future__ import annotations

import logging

import dspy

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.llm_config import get_llm_model
from smarter_dev.llm_config import get_model_info

logger = logging.getLogger(__name__)

# Configure LLM model from environment
STREAK_AGENT_LM = get_llm_model("default")

# Log which model is being used
model_info = get_model_info("default")
logger.info(f"StreakCelebrationAgent using LLM model: {model_info['model_name']} (provider: {model_info['provider']})")


class StreakCelebrationSignature(dspy.Signature):
    """You are a wildly creative, unpredictable celebration agent that generates completely unique messages when users get streak bonuses for their daily bytes rewards.

    ## YOUR CREATIVE MISSION
    Generate a celebration (under 200 characters) that:
    - Takes inspiration from the user's message and creates fun and celebratory!
    - Mentions BYTES reward and the multiplier bonus somewhere in the message
    - Incorporates the streak days naturally

    ## On Being Appropriate
    - Be appropriate and respectful
    - If celebration is inappropriate, generate a generic informational message instead (e.g. "Your 8-day streak earned you a 2x bonus today")
    - Never comment on unfortunate, negative, or triggering content
    """

    bytes_earned: int = dspy.InputField(description="Total bytes the user earned (after multiplier)")
    streak_multiplier: int = dspy.InputField(description="The streak bonus multiplier that was applied")
    streak_days: int = dspy.InputField(description="Number of days in the user's streak")
    user_message: str = dspy.InputField(description="The user's message content - riff on this respectfully if you can!")
    response: str = dspy.OutputField(description="Very fun celebration message!")


class StreakCelebrationAgent(BaseAgent):
    """Agent for generating celebratory streak bonus messages."""

    def __init__(self):
        """Initialize the streak celebration agent."""
        super().__init__()
        self._agent = dspy.Predict(StreakCelebrationSignature)

    async def generate_celebration_message(
        self,
        bytes_earned: int,
        streak_multiplier: int,
        streak_days: int,
        user_id: int,
        user_message: str
    ) -> tuple[str, int]:
        """Generate a celebratory message for streak bonuses.

        Args:
            bytes_earned: Number of bytes the user earned
            streak_multiplier: The streak bonus multiplier applied
            streak_days: Number of days in the user's streak
            user_id: Discord user ID for mentioning
            user_message: Content of the user's message that triggered the reward

        Returns:
            Tuple[str, int]: Generated celebratory message and token usage
        """
        # Only generate celebration message if there's actually a streak bonus
        if streak_multiplier <= 1:
            return "", 0

        try:
            # Create user mention string
            user_mention = f"<@{user_id}>"

            # Use async agent to generate celebration message with proper LM context
            with dspy.context(lm=STREAK_AGENT_LM, track_usage=True):
                async_agent = dspy.asyncify(self._agent)
                result = await async_agent(
                    bytes_earned=bytes_earned,
                    streak_multiplier=streak_multiplier,
                    streak_days=streak_days,
                    user_mention=user_mention,
                    user_message=user_message
                )

            # Get token usage
            tokens_used = self._extract_token_usage(result)

            # Final fallback: estimate tokens from text length if all methods fail
            if tokens_used == 0:
                input_chars = len(f"{bytes_earned}{streak_multiplier}{streak_days}")
                output_chars = len(result.response)
                tokens_used = (input_chars + output_chars) // 4
                logger.debug(f"STREAK DEBUG: Fallback estimation - {tokens_used} tokens from text length")

            return f"-# {user_mention} {result.response}", tokens_used

        except Exception as e:
            logger.error(f"Error generating streak celebration message: {e}")
            return "", 0


# Global streak celebration agent instance
streak_celebration_agent = StreakCelebrationAgent()
