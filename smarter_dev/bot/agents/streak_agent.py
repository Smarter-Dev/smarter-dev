"""Streak celebration agent for generating fun daily streak bonus messages."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.bot.agents.utility_model import build_utility_agent
from smarter_dev.bot.agents.utility_model import render_inputs
from smarter_dev.bot.agents.utility_model import tokens_used
from smarter_dev.bot.agents.utility_model import utility_metered

logger = logging.getLogger(__name__)

STREAK_CELEBRATION_INSTRUCTIONS = (
    Path(__file__).parent / "prompts" / "streak_agent.md"
).read_text(encoding="utf-8")


class StreakCelebration(BaseModel):
    response: str = Field(description="Very fun celebration message!")


_streak_celebration_agent: Agent[None, StreakCelebration] | None = None


def get_streak_celebration_agent() -> Agent[None, StreakCelebration]:
    """Return the singleton streak-celebration agent, building it on first use."""
    global _streak_celebration_agent
    if _streak_celebration_agent is None:
        _streak_celebration_agent = build_utility_agent(
            StreakCelebration, STREAK_CELEBRATION_INSTRUCTIONS
        )
    return _streak_celebration_agent


class StreakCelebrationAgent(BaseAgent):
    """Agent for generating celebratory streak bonus messages."""

    async def generate_celebration_message(
        self,
        bytes_earned: int,
        streak_multiplier: int,
        streak_days: int,
        user_id: int,
        user_message: str,
        *,
        guild_id: int | str | None = None,
        channel_id: int | str | None = None,
    ) -> tuple[str, int]:
        """Generate a celebratory message for streak bonuses.

        Args:
            bytes_earned: Number of bytes the user earned
            streak_multiplier: The streak bonus multiplier applied
            streak_days: Number of days in the user's streak
            user_id: Discord user ID for mentioning
            user_message: Content of the user's message that triggered the reward
            guild_id, channel_id: Where the reward was earned, for the usage ledger

        Returns:
            Tuple[str, int]: Generated celebratory message and token usage
        """
        # Only generate celebration message if there's actually a streak bonus
        if streak_multiplier <= 1:
            return "", 0

        try:
            # Create user mention string
            user_mention = f"<@{user_id}>"

            agent = get_streak_celebration_agent()
            async with utility_metered(
                "streak_celebration",
                discord_user_id=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
            ) as meter:
                result = await agent.run(
                    render_inputs(
                        bytes_earned=bytes_earned,
                        streak_multiplier=streak_multiplier,
                        streak_days=streak_days,
                        user_message=user_message,
                    ),
                    model=meter.wrap(agent.model),
                )

            celebration = result.output.response.strip()
            if not celebration:
                return "", 0

            return f"-# {user_mention} {celebration}", tokens_used(result)

        except Exception as e:
            logger.error(
                f"Error generating streak celebration message: {type(e).__name__}"
            )
            return "", 0

