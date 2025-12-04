"""Advent of Code thread message agent for generating fun daily thread intros."""

from __future__ import annotations

import logging
from datetime import datetime

import dspy

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.llm_config import get_llm_model
from smarter_dev.llm_config import get_model_info

# Final day of Advent of Code (must match advent_of_code_service.AOC_END_DAY)
AOC_FINAL_DAY = 12

logger = logging.getLogger(__name__)

# Configure LLM model from environment - use fast model for quick responses
AOC_AGENT_LM = get_llm_model("fast")

# Log which model is being used
model_info = get_model_info("fast")
logger.info(f"AoCThreadAgent using LLM model: {model_info['model_name']} (provider: {model_info['provider']})")


class AoCThreadSignature(dspy.Signature):
    """You are an enthusiastic Advent of Code celebration agent that generates fun, encouraging messages for daily AoC discussion threads.

    ## YOUR MISSION
    Generate a short, fun intro message (2-4 sentences, under 300 characters) that:
    - Creates excitement for today's Advent of Code challenge
    - References the day number and date naturally
    - Encourages collaboration and fun problem-solving
    - Has a festive, coding-themed vibe appropriate for December

    ## STYLE GUIDELINES
    - Be encouraging and welcoming to all skill levels
    - Use coding/programming humor when appropriate
    - Keep it concise - this appears at the top of a discussion thread
    - Vary your style - sometimes punny, sometimes inspirational, sometimes playful
    - Match the energy to the day (early days = fresh start vibes, mid-event = momentum, approaching finale = building excitement)
    """

    day: int = dspy.InputField(description="Day number (1-12)")
    date_str: str = dspy.InputField(description="The date in format like 'December 5th, 2025'")
    year: int = dspy.InputField(description="The year")
    response: str = dspy.OutputField(description="Fun, encouraging intro message for the thread")


class AoCFinalDaySignature(dspy.Signature):
    """You are an EXTREMELY enthusiastic Advent of Code celebration agent for THE GRAND FINALE - Day 12!

    ## YOUR EPIC MISSION
    Generate an absolutely LEGENDARY celebration message (3-5 sentences, under 500 characters) that:
    - Goes ALL OUT celebrating Day 12 - the final challenge of this year's Advent of Code!
    - Celebrates the incredible journey of 12 days of coding puzzles
    - Creates maximum hype for the ultimate puzzle
    - Thanks the community for an amazing stretch of problem-solving together
    - Wishes everyone a Merry Christmas and Happy New Year!

    ## STYLE GUIDELINES
    - This is THE BIG ONE - bring maximum energy!
    - Blend holiday cheer with coding celebration
    - Be dramatic, festive, and genuinely appreciative
    - Use emojis liberally - it's the holidays!
    - Make people feel proud of making it this far
    - Create a sense of epic finale energy and holiday warmth
    """

    year: int = dspy.InputField(description="The year")
    response: str = dspy.OutputField(description="Epic finale celebration message with holiday wishes!")


class AoCThreadAgent(BaseAgent):
    """Agent for generating Advent of Code thread intro messages."""

    def __init__(self):
        """Initialize the AoC thread agent."""
        super().__init__()
        self._agent = dspy.Predict(AoCThreadSignature)
        self._final_day_agent = dspy.Predict(AoCFinalDaySignature)

    def _get_date_suffix(self, day: int) -> str:
        """Get the ordinal suffix for a day number."""
        if 11 <= day <= 13:
            return "th"
        return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

    async def generate_thread_message(
        self,
        day: int,
        year: int
    ) -> tuple[str, int]:
        """Generate an intro message for an AoC thread.

        Args:
            day: Day number (1-12)
            year: The year

        Returns:
            Tuple[str, int]: Generated message and token usage
        """
        try:
            # Format the date nicely
            suffix = self._get_date_suffix(day)
            date_str = f"December {day}{suffix}, {year}"

            # Use special finale agent for the final day
            if day == AOC_FINAL_DAY:
                with dspy.context(lm=AOC_AGENT_LM, track_usage=True):
                    async_agent = dspy.asyncify(self._final_day_agent)
                    result = await async_agent(year=year)
            else:
                with dspy.context(lm=AOC_AGENT_LM, track_usage=True):
                    async_agent = dspy.asyncify(self._agent)
                    result = await async_agent(
                        day=day,
                        date_str=date_str,
                        year=year
                    )

            # Get token usage
            tokens_used = self._extract_token_usage(result)

            # Fallback estimation if needed
            if tokens_used == 0:
                output_chars = len(result.response)
                tokens_used = output_chars // 4
                logger.debug(f"AOC DEBUG: Fallback estimation - {tokens_used} tokens")

            return result.response, tokens_used

        except Exception as e:
            logger.error(f"Error generating AoC thread message: {e}")
            # Return a fallback message
            if day == AOC_FINAL_DAY:
                return "It's the final day of Advent of Code! Let's finish this journey together. Merry Christmas and Happy New Year!", 0
            return f"Day {day} is here! Let's solve today's challenge together.", 0


# Global AoC thread agent instance
aoc_thread_agent = AoCThreadAgent()
