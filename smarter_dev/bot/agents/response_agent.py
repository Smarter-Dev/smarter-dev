"""Response agent for multi-agent mention pipeline.

This module contains the response agent that:
- Uses dspy.ReAct with tools to generate responses
- Returns structured output for flow control (continue_watching, etc.)
"""

from __future__ import annotations

import logging

import dspy
import hikari

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.bot.agents.tools import create_response_tools
from smarter_dev.bot.agents.watcher import ResponseAgentOutput, UpdateFrequency
from smarter_dev.llm_config import get_llm_model, get_model_info

logger = logging.getLogger(__name__)

# Configure LLM model - use fast model for response generation
RESPONSE_AGENT_LM = get_llm_model("fast")

# Log which model is being used
model_info = get_model_info("fast")
logger.info(
    f"ResponseAgent using LLM model: {model_info['model_name']} "
    f"(provider: {model_info['provider']})"
)


class ResponseSignature(dspy.Signature):
    """Generate a response to a Discord @mention and decide whether to continue watching.

    You're a friendly community member in a Discord server - casual, direct, and genuine.
    You have opinions, you're helpful, and you communicate like a real person.

    ## HOW TO RESPOND

    **Discord style:**
    - Keep messages short - one thought per message
    - Send multiple short messages rather than one long one
    - Use reactions instead of words for simple responses (agreement, laughter, support)
    - Code always in backticks or code blocks with language specified
    - Acknowledge before slow operations (research, URL fetching) - a quick "let me check" helps
    - No formal markdown (bullets, headers) unless explaining something complex

    **Tone:**
    - Casual and natural - contractions, informal language
    - Direct - answer the question first, elaborate only if asked
    - For code questions: be a reference, not a solution machine
    - When asked for "brief", "quick", "short" → give exactly that, no fluff

    ## TOOL USAGE

    - Use `start_typing()` before composing responses
    - Use `send_message()` to send your response
    - Use `reply_to_message()` to reply to a specific message
    - Use `add_reaction_to_message()` for quick acknowledgments
    - Use `lookup_fact()` for general facts (capitals, dates)
    - Use `search_web()` for specific/current information
    - Use `generate_in_depth_response()` for technical topics, then send the result

    **IMPORTANT: If `generate_in_depth_response` is on cooldown, just write a direct response
    yourself using `send_message()`. Don't wait or retry - provide your best answer directly.**

    ## FLOW CONTROL (Important!)

    After responding, you MUST decide whether to continue watching for follow-up messages.
    This is done via structured output fields, NOT via tools.

    **DEFAULT: continue_watching=true** - Always expect follow-up questions, especially for
    substantive or technical topics. Users often have clarifying questions after an answer.

    **continue_watching=false ONLY when:**
    - Conversation is hostile or aggressive
    - Topic is drifting off-topic or becoming irrelevant
    - Topic is sensitive (personal, confidential, inappropriate)
    - User explicitly said goodbye, thanks, or requested you stop
    - Trivial interaction (greeting, simple emoji reaction)

    **watching_for:** Describe what follow-up you're expecting (e.g., "follow-up questions
    about Arduino wiring", "clarification on the code example", "questions about next steps")

    **wait_duration:** How long to wait for follow-up (30-300 seconds)
    - Short (60s): Simple topics, quick Q&A
    - Medium (120s): Technical explanations, tutorials
    - Long (180-300s): Complex discussions, debugging sessions, user testing something

    **update_frequency:** How often to check for relevant messages
    - "10s": Fast-paced active discussion
    - "1m": Normal conversation (default)
    - "5m": User needs time to think, test code, or try something
    """

    relevant_messages: str = dspy.InputField(
        description="Filtered conversation timeline containing only the relevant messages for this request"
    )
    intent: str = dspy.InputField(
        description="What the user is asking for or trying to accomplish"
    )
    context_summary: str = dspy.InputField(
        description="Brief summary of the conversation context"
    )
    channel_info: dict = dspy.InputField(
        description="Channel info with name and description fields"
    )
    users: list[dict] = dspy.InputField(
        description="List of users with user_id, discord_name, nickname, server_nickname, role_names, is_bot fields"
    )
    me: dict = dspy.InputField(
        description="Bot info with bot_name and bot_id fields"
    )

    response: str = dspy.OutputField(
        description="Your response (tools handle actual sending - this is for logging)"
    )
    continue_watching: bool = dspy.OutputField(
        description="True if you want to continue watching for follow-up messages"
    )
    watching_for: str = dspy.OutputField(
        description="What you're watching for if continue_watching is true (empty if false)"
    )
    wait_duration: int = dspy.OutputField(
        description="How long to wait for follow-up messages in seconds (30-300)"
    )
    update_frequency: str = dspy.OutputField(
        description="How often to check for messages: '10s', '1m', or '5m'"
    )


class ResponseAgent(BaseAgent):
    """Response agent for generating Discord responses with structured flow control output."""

    def __init__(self):
        """Initialize the response agent."""
        super().__init__()
        self._agent_signature = ResponseSignature

    async def generate_response(
        self,
        bot: hikari.GatewayBot,
        channel_id: int,
        guild_id: int,
        relevant_messages: str,
        intent: str,
        context_summary: str,
        channel_info: dict,
        users: list[dict],
        me_info: dict,
        request_id: str = "unknown"
    ) -> tuple[bool, ResponseAgentOutput]:
        """Generate a response using ReAct with tools.

        Args:
            bot: Discord bot instance
            channel_id: Channel ID to respond in
            guild_id: Guild ID for context
            relevant_messages: Filtered timeline with relevant messages only
            intent: What the user is asking for
            context_summary: Summary of conversation context
            channel_info: Channel info dict
            users: List of user info dicts
            me_info: Bot info dict
            request_id: Request ID for tracing

        Returns:
            Tuple of (success, ResponseAgentOutput)
        """
        logger.info(
            f"[{request_id}] ResponseAgent.generate_response called: "
            f"channel={channel_id}, intent='{intent[:50]}...'"
        )
        logger.debug(
            f"[{request_id}] Context: relevant_messages={len(relevant_messages)} chars, "
            f"context_summary='{context_summary[:80]}...'"
        )

        try:
            # Create context-bound tools for this response
            logger.debug(f"[{request_id}] Creating response tools...")
            tools, _channel_queries = create_response_tools(
                bot=bot,
                channel_id=str(channel_id),
                guild_id=str(guild_id)
            )
            logger.debug(f"[{request_id}] Created {len(tools)} tools")

            # Use context manager to ensure correct LM is used
            logger.debug(f"[{request_id}] Starting ReAct agent...")
            with dspy.context(lm=RESPONSE_AGENT_LM, track_usage=True):
                react_agent = dspy.ReAct(
                    self._agent_signature,
                    tools=tools,
                    max_iters=50  # Reasonable limit for response generation
                )

                # Generate response using the ReAct agent
                result = await react_agent.acall(
                    relevant_messages=relevant_messages,
                    intent=intent,
                    context_summary=context_summary,
                    channel_info=channel_info,
                    users=users,
                    me=me_info
                )

            logger.debug(f"[{request_id}] ReAct agent completed")
            logger.debug(f"[{request_id}] ResponseAgent raw result: {result}")

            # Parse structured output
            output = ResponseAgentOutput.from_agent_result(result)

            # Extract token usage
            tokens_used = self._extract_token_usage(result)
            if tokens_used == 0:
                tokens_used = self._estimate_tokens(result.response or "")
            output.tokens_used = tokens_used

            logger.info(
                f"[{request_id}] ResponseAgent completed: "
                f"continue_watching={output.continue_watching}, "
                f"watching_for='{output.watching_for[:50]}...', "
                f"wait_duration={output.wait_duration}s, "
                f"update_frequency={output.update_frequency.value}, "
                f"tokens={tokens_used}"
            )

            return True, output

        except Exception as e:
            logger.error(f"[{request_id}] ResponseAgent failed: {e}", exc_info=True)
            # Return default output on failure
            return False, ResponseAgentOutput(
                continue_watching=False,
                watching_for="",
                wait_duration=60,
                update_frequency=UpdateFrequency.ONE_MINUTE,
                tokens_used=0
            )


# Global instance
_response_agent: ResponseAgent | None = None


def get_response_agent() -> ResponseAgent:
    """Get or create the global response agent.

    Returns:
        The global ResponseAgent instance
    """
    global _response_agent
    if _response_agent is None:
        _response_agent = ResponseAgent()
    return _response_agent
