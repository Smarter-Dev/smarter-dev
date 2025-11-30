"""Advent of Code thread message agent for generating fun daily thread intros."""

from __future__ import annotations

import logging
from datetime import datetime

import dspy

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.llm_config import get_llm_model
from smarter_dev.llm_config import get_model_info

logger = logging.getLogger(__name__)

# Configure LLM model from environment
AOC_AGENT_LM = get_llm_model("default")

# Log which model is being used
model_info = get_model_info("default")
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
    - Match the energy to the day (early days = fresh start vibes, mid-month = momentum, late = home stretch)
    """

    day: int = dspy.InputField(description="Day number (1-25)")
    date_str: str = dspy.InputField(description="The date in format like 'December 5th, 2025'")
    year: int = dspy.InputField(description="The year")
    response: str = dspy.OutputField(description="Fun, encouraging intro message for the thread")


class AoCChristmasSignature(dspy.Signature):
    """You are an EXTREMELY enthusiastic Advent of Code celebration agent for CHRISTMAS DAY - the grand finale!

    ## YOUR EPIC MISSION
    Generate an absolutely LEGENDARY celebration message (3-5 sentences, under 500 characters) that:
    - Goes ALL OUT celebrating Day 25 - the final challenge!
    - Acknowledges this is Christmas Day AND the AoC finale
    - Celebrates the incredible journey of 25 days of coding
    - Creates maximum hype for the ultimate puzzle
    - Thanks the community for an amazing month of problem-solving together

    ## STYLE GUIDELINES
    - This is THE BIG ONE - bring maximum energy!
    - Blend Christmas cheer with coding celebration
    - Be dramatic, festive, and genuinely appreciative
    - Use emojis liberally - it's Christmas!
    - Make people feel proud of making it this far
    - Create a sense of epic finale energy
    """

    year: int = dspy.InputField(description="The year")
    response: str = dspy.OutputField(description="Epic Christmas Day finale celebration message!")


class AoCThreadAgent(BaseAgent):
    """Agent for generating Advent of Code thread intro messages."""

    def __init__(self):
        """Initialize the AoC thread agent."""
        super().__init__()
        self._agent = dspy.Predict(AoCThreadSignature)
        self._christmas_agent = dspy.Predict(AoCChristmasSignature)

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
            day: Day number (1-25)
            year: The year

        Returns:
            Tuple[str, int]: Generated message and token usage
        """
        try:
            # Format the date nicely
            suffix = self._get_date_suffix(day)
            date_str = f"December {day}{suffix}, {year}"

            # Use special Christmas agent for day 25
            if day == 25:
                with dspy.context(lm=AOC_AGENT_LM, track_usage=True):
                    async_agent = dspy.asyncify(self._christmas_agent)
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
            if day == 25:
                return "It's Christmas Day and the final Advent of Code challenge! Let's finish this journey together!", 0
            return f"Day {day} is here! Let's solve today's challenge together.", 0


# Global AoC thread agent instance
aoc_thread_agent = AoCThreadAgent()
