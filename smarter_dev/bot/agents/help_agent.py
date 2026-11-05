"""Help agent for /help command responses."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import hikari
from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.bot.agents.utility_model import build_utility_agent
from smarter_dev.bot.agents.utility_model import render_inputs
from smarter_dev.bot.agents.utility_model import tokens_used
from smarter_dev.bot.agents.utility_model import utility_metered

logger = logging.getLogger(__name__)

HELP_INSTRUCTIONS = (Path(__file__).parent / "prompts" / "help_agent.md").read_text(
    encoding="utf-8"
)


class HelpResponse(BaseModel):
    response: str = Field(
        description=(
            "CRITICAL: Your response MUST be under 2000 characters. Discord has a "
            "strict 2000 character limit. Count characters and adjust content to fit "
            "within this limit. Helpful response explaining bot functionality"
        )
    )


_help_agent: Agent[None, HelpResponse] | None = None


def get_help_agent() -> Agent[None, HelpResponse]:
    """Return the singleton help agent, building it on first use."""
    global _help_agent
    if _help_agent is None:
        _help_agent = build_utility_agent(HelpResponse, HELP_INSTRUCTIONS)
    return _help_agent


class HelpAgent(BaseAgent):
    """Help agent for /help commands."""

    async def generate_response(
        self,
        user_question: str,
        bot: hikari.GatewayBot,
        channel_id: int,
        guild_id: int | None = None,
        trigger_message_id: int | None = None,
        messages_remaining: int = 10,
        user_id: int | str | None = None,
    ) -> tuple[str, int]:
        """Generate a helpful response to a user's question.

        Args:
            user_question: The user's question about the bot
            bot: Discord bot instance for fetching context
            channel_id: Channel ID to gather context from
            guild_id: Guild ID for user/role information
            trigger_message_id: Message ID that triggered this response
            messages_remaining: Number of help messages user can send after this one
            user_id: Discord user asking, for the usage ledger

        Returns:
            Tuple[str, int]: Generated response and token usage count
        """
        from smarter_dev.bot.utils.messages import ConversationContextBuilder

        # Build structured context
        context_builder = ConversationContextBuilder(bot, guild_id)
        context = await context_builder.build_context(channel_id, trigger_message_id)

        agent = get_help_agent()
        async with utility_metered(
            "help_agent",
            discord_user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
        ) as meter:
            result = await agent.run(
                render_inputs(
                    conversation_timeline=context["conversation_timeline"],
                    users=json.dumps(context["users"], default=str),
                    channel=json.dumps(context["channel"], default=str),
                    me=json.dumps(context["me"], default=str),
                    user_question=user_question,
                    messages_remaining=messages_remaining,
                ),
                model=meter.wrap(agent.model),
            )

        # Validate response length for Discord
        response = self._validate_response_length(result.output.response.strip())
        tokens = tokens_used(result)

        logger.info(f"HelpAgent response generated: {len(response)} chars, {tokens} tokens")

        return response, tokens


# Global help agent instance
help_agent = HelpAgent()
