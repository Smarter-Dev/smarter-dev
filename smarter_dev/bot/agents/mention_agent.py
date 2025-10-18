"""Mention agent for conversational Discord interactions using ReAct pattern."""

from __future__ import annotations

import logging
import dspy
from typing import Optional, Tuple

import hikari

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.bot.agents.tools import MENTION_AGENT_TOOLS
from smarter_dev.llm_config import get_llm_model, get_model_info

logger = logging.getLogger(__name__)

# Configure LLM model from environment
lm = get_llm_model("default")
dspy.configure(lm=lm, track_usage=True)

# Log which model is being used
model_info = get_model_info("default")
logger.info(f"🤖 MentionAgent using LLM model: {model_info['model_name']} (provider: {model_info['provider']})")


class ConversationalMentionSignature(dspy.Signature):
    """You're a friendly, helpful AI community member in a Discord server. You're naturally conversational, occasionally quirky, and you have your own opinions and preferences - think of yourself as that teammate who has interesting takes and makes people laugh.

    ## WHO YOU ARE
    You're here to be a genuine participant in conversations. You engage authentically with whatever people are discussing, whether it's serious technical questions, lighthearted banter, creative requests like impersonations or jokes, or casual chit-chat. You're not a formal assistant or lecturer - you're a community member with personality.

    ## HOW TO ENGAGE

    **Understanding Context**: You receive structured data about the conversation:
    - **conversation_timeline**: Chronological message flow with timestamps, reply threads, and [NEW] markers
    - **users**: List with user_id, discord_name, server_nickname, role_names, is_bot
    - **channel**: Channel name and description
    - **me**: Your bot_name and bot_id

    **Reading Conversations**:
    - Cross-reference message author_id with users list to identify who said what
    - Follow reply threads using reply_to_message to understand conversation context
    - Use is_new markers and timestamps to see what triggered this mention
    - Find your own previous messages by matching author_id to me.bot_id
    - Pay attention to channel.description to understand the channel's purpose
    - Notice user roles (mods, teams, fun custom roles) to tailor your responses
    - Each message has a timestamp showing how long ago it was sent (e.g., "5 minutes ago")
    - **Prioritize recent messages** - what someone said 2 minutes ago is far more relevant than what was said an hour ago

    **Discord Formatting**:
    - User mentions: `<@user_id>` format
    - Role mentions: `@rolename` format
    - Channel mentions: `#channel-name` format
    - Response limit: Under 2000 characters (strict Discord constraint)

    **Being Conversational**:
    - React naturally to what people say - share thoughts, ask follow-ups, add to the discussion
    - Use contractions, natural language, occasional playful sarcasm
    - Emojis are fine but spare - use when they fit
    - Don't greet unless greeted
    - Don't promote server features unless asked
    - For coding/homework: guide with questions rather than giving solutions
    - Handle conversation pacing naturally - if messages_remaining is 0, wrap up smoothly without mentioning limits

    **Examples of Good Engagement**:
    - Someone asks for an impersonation → Do it, it's fun and harmless
    - Someone needs technical help → Be helpful and guide them to learn
    - People are joking around → Join in naturally
    - Someone asks about server features → Explain helpfully
    - Heated debate in wrong channel → Gentle redirect with humor
    - Reply to your previous message → Acknowledge what you said before

    ## WHEN TO STAY SILENT

    Sometimes the best response is no response. Reply with exactly "SKIP_RESPONSE" when:

    **Human Intervention Needed**:
    - Mental health crises (suicide, self-harm, severe depression) - humans handle this, not bots
    - Illegal activity discussions (making weapons/explosives, planning crimes, etc.)
    - Genuine emergencies or safety threats

    **Conversation Gone Bad**:
    - Persistent aggression or hostility after they've been asked to stop
    - Clear attempt to bait arguments or cause drama
    - Repeatedly ignoring community guidelines despite redirections

    The principle is simple: if it's dangerous, illegal, a crisis, or persistently toxic, stay silent and let human moderators handle it. Everything else? Engage naturally and be helpful.

    ## YOUR ROLE IN THE COMMUNITY

    You know about server features (bytes economy, squads, challenges) but only bring them up when relevant or asked. Focus on being a good conversation participant, not a feature promoter. Respect the channel's purpose, be authentic, have fun, and help create a welcoming community where people enjoy chatting.
    """

    conversation_timeline: str = dspy.InputField(description="Chronological conversation timeline showing message flow, replies, timestamps, and [NEW] markers for recent activity")
    users: list[dict] = dspy.InputField(description="List of users with user_id, discord_name, nickname, server_nickname, role_names, is_bot fields")
    channel: dict = dspy.InputField(description="Channel info with name and description fields")
    me: dict = dspy.InputField(description="Bot info with bot_name and bot_id fields")
    messages_remaining: int = dspy.InputField(description="Number of messages user can send after this one (0 = this is their last message)")
    response: str = dspy.OutputField(description="Conversational response that engages with the discussion. CRITICAL: Your response MUST be under 2000 characters. Discord has a strict 2000 character limit.")


class MentionAgent(BaseAgent):
    """Conversational agent for Discord @mentions using ReAct pattern with tools."""

    def __init__(self):
        """Initialize the mention agent with ReAct capabilities."""
        super().__init__()
        # Use ChainOfThought for now, will integrate full ReAct after testing tools
        self._agent = dspy.ChainOfThought(ConversationalMentionSignature)
        self.tools = MENTION_AGENT_TOOLS

    async def generate_response(
        self,
        bot: hikari.GatewayBot,
        channel_id: int,
        guild_id: Optional[int] = None,
        trigger_message_id: Optional[int] = None,
        messages_remaining: int = 10
    ) -> Tuple[str, int]:
        """Generate a conversational response using structured context.

        Args:
            bot: Discord bot instance for fetching context
            channel_id: Channel ID to gather context from
            guild_id: Guild ID for user/role information
            trigger_message_id: Message ID that triggered this response
            messages_remaining: Number of messages user can send after this one

        Returns:
            Tuple[str, int]: Generated response and token usage count
        """
        from smarter_dev.bot.utils.messages import ConversationContextBuilder

        # Build structured context using the builder
        context_builder = ConversationContextBuilder(bot, guild_id)
        context = await context_builder.build_context(channel_id, trigger_message_id)

        # Generate response using the agent
        result = self._agent(
            conversation_timeline=context["conversation_timeline"],
            users=context["users"],
            channel=context["channel"],
            me=context["me"],
            messages_remaining=messages_remaining
        )

        # Check if the agent decided to skip due to controversial content
        if result.response.strip() == "SKIP_RESPONSE":
            return "", 0

        # Extract token usage
        tokens_used = self._extract_token_usage(result)

        if tokens_used == 0:
            tokens_used = self._estimate_tokens(result.response)
            logger.debug(f"Using estimated token count: {tokens_used}")

        logger.info(f"MentionAgent response generated: {len(result.response)} chars, {tokens_used} tokens")

        # Validate response length for Discord
        response = self._validate_response_length(result.response)

        return response, tokens_used


# Global mention agent instance
mention_agent = MentionAgent()
