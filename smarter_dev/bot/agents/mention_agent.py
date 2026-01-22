"""Mention agent for conversational Discord interactions using ReAct pattern."""

from __future__ import annotations

import logging

import dspy
import hikari

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.bot.agents.tools import create_mention_tools
from smarter_dev.bot.services.channel_state import get_channel_state_manager
from smarter_dev.llm_config import get_llm_model
from smarter_dev.llm_config import get_model_info

logger = logging.getLogger(__name__)

# Configure LLM model from environment - use judge model for mention handling
# NOTE: We don't call dspy.configure() globally to avoid conflicts with other agents
# Instead, we use dspy.context() when creating the ReAct agent
MENTION_AGENT_LM = get_llm_model("judge")

# Log which model is being used
model_info = get_model_info("judge")
logger.info(f"🤖 MentionAgent using LLM model: {model_info['model_name']} (provider: {model_info['provider']})")


class ConversationalMentionSignature(dspy.Signature):
    """You're a friendly community member in a Discord server - casual, direct, and genuine. You have opinions,
    you're helpful, and you communicate like a real person, not a formal assistant.

    ## HOW TO DECIDE WHAT TO DO

    When mentioned, think through this:

    **1. Is this message FOR me or ABOUT me?**

    Ask yourself: Who is the speaker actually trying to communicate with?

    FOR me (respond):
    - Direct questions, requests, or commands to me
    - I'm being asked to do something or answer something
    - The conversation is with me as a participant

    ABOUT me (stay silent):
    - Message replies to someone else but mentions me in passing
    - Someone is citing or referencing what I said to another person
    - I'm being discussed but not addressed

    Key signal: If a message replies to User A and mentions me, it's probably directed at User A, not me.
    If unsure, use `generate_engagement_plan()` to see full 20-message context.

    **2. If FOR me, what kind of response?**

    General fact question (capitals, dates, definitions) → Use `lookup_fact()`, then answer briefly
    Research/specific question (current events, comparisons, niche topics) → Use `search_web()`, then answer
    Technical/coding question → Use `generate_in_depth_response()`, then `send_message()` with the result
    Casual chat → Just respond naturally yourself

    **3. Special tool behaviors to know:**

    `generate_in_depth_response()`: Only GENERATES text - you MUST call `send_message(result['response'])` after!
    - Pass along user preferences in your prompt: if they asked for "brief", "detailed", "eli5", etc., include that
    - If send_message() returns MESSAGE_TOO_LONG error, you need to write a shorter response yourself
    `generate_engagement_plan()`: Use when context is unclear or conversation is complex (3+ people, confusing flow)

    ## HOW TO COMMUNICATE

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
    - Don't greet unless greeted, don't promote features unless asked

    ## CRITICAL RULES

    **Tool Failures - Read the error and adapt:**

    DUPLICATE_MESSAGE error:
    - Message already sent successfully - DO NOT retry
    - You MUST immediately call `wait_for_messages()` or `stop_monitoring()`
    - Do not just return - take one of these actions

    DUPLICATE_SEARCH error:
    - You already searched for this exact query - the results are in your trajectory above
    - DO NOT retry the same search - look at the observation from your earlier search
    - Either respond based on those results, or try a DIFFERENT search query

    DUPLICATE_URL error:
    - You already opened this URL with this question - the answer is in your trajectory above
    - DO NOT retry the same open_url call - use the answer you already have

    DUPLICATE_PLAN error:
    - You already generated an engagement plan - it's in your trajectory above
    - DO NOT generate another plan - follow the recommended_actions from the existing plan

    Rate limit errors:
    - Respect the limit, don't retry immediately
    - Explain or try a different approach

    Other errors:
    - Don't loop retrying the same failed action
    - Adapt and move forward

    **User Requests to Stop:**
    When told to stop ("stop", "leave us alone", "that's enough"):
    - You MUST call `stop_monitoring()` - not optional
    - Can acknowledge briefly first, but then call the tool

    **After wait_for_messages() Returns:**
    - Return IMMEDIATELY - no more messages, reactions, or tool calls
    - The system restarts you with fresh context if needed
    - If it timed out with no messages, conversation is over - just return

    ## WHEN TO STAY SILENT

    Return "SKIP_RESPONSE" without any tool calls when:
    - Message is ABOUT you, not FOR you (see decision framework above)
    - Mental health crisis, illegal activity, or safety emergency (let humans handle)
    - Persistent hostility or drama-baiting
    - You'd be interrupting a conversation between others

    ## CONVERSATION LOOP

    Each invocation follows this pattern:
    1. Read context (you see 5 recent messages)
    2. Decide: respond, stay silent, or need more context?
    3. Take action if responding
    4. Call `wait_for_messages()` once
    5. Return immediately - system handles the rest

    The system auto-restarts you in a loop. You never call wait_for_messages() multiple times.
    Just: act → wait → return. The loop feels infinite but each invocation is one cycle.
    """

    conversation_timeline: str = dspy.InputField(description="Chronological conversation timeline showing message flow, replies, timestamps, and [NEW] markers for recent activity. Each message includes [ID: ...] for use with reply and reaction tools.")
    users: list[dict] = dspy.InputField(description="List of users with user_id, discord_name, nickname, server_nickname, role_names, is_bot fields")
    channel: dict = dspy.InputField(description="Channel info with name and description fields")
    me: dict = dspy.InputField(description="Bot info with bot_name and bot_id fields")
    recent_search_queries: list[str] = dspy.InputField(description="List of recent search queries made in this channel (results may be cached)")
    messages_remaining: int = dspy.InputField(description="Number of messages user can send after this one (0 = this is their last message)")
    is_continuation: bool = dspy.InputField(description="True if this is a continuation of a previous monitoring session (agent is being restarted after waiting), False if this is a fresh mention")
    previous_summary: str = dspy.InputField(description="Summary of conversation context from before the restart. Empty string if this is a fresh conversation or no summary was provided. Use this to understand what has been discussed without needing full message history.")
    response: str = dspy.OutputField(description="Your conversational response in casual Discord style. Default to SHORT one-liners - use send_message() multiple times if a thought needs more than one line. Always format code in backticks or code blocks - NEVER send raw code. Use add_reaction_to_message() for quick emotional responses instead of typing (lol, agree, etc). Use reply_to_message() when engaging with specific ideas. Use lookup_fact() for general facts (capitals, dates, definitions) or search_web() for specific/current information. Only send longer messages for genuinely complex topics or when explicitly asked for depth.")


class MentionAgent(BaseAgent):
    """Conversational agent for Discord @mentions using ReAct pattern with tools."""

    def __init__(self):
        """Initialize the mention agent with ReAct capabilities."""
        super().__init__()
        # Agent will be created dynamically per mention with context-bound tools
        self._agent_signature = ConversationalMentionSignature

    async def generate_response(
        self,
        bot: hikari.GatewayBot,
        channel_id: int,
        guild_id: int | None = None,
        trigger_message_id: int | None = None,
        messages_remaining: int = 10,
        is_continuation: bool = False,
        previous_summary: str = "",
        since_message_id: str | None = None
    ) -> tuple[bool, int, str | None]:
        """Generate a conversational response using ReAct with context-bound tools.

        The agent will use the send_message tool to send its response directly to Discord.
        This method returns whether the agent successfully sent a message, along with
        token usage and the response text for conversation logging.

        Args:
            bot: Discord bot instance for fetching context
            channel_id: Channel ID to gather context from
            guild_id: Guild ID for user/role information
            trigger_message_id: Message ID that triggered this response
            messages_remaining: Number of messages user can send after this one
            is_continuation: True if this is a continuation after waiting (agent being restarted)
            previous_summary: Summary of conversation from before restart (for context continuity)
            since_message_id: If provided, fetch messages after this ID (for restart catch-up)

        Returns:
            Tuple[bool, int, Optional[str]]: (success, token_usage, response_text)
                - success: whether agent sent a message
                - token_usage: tokens consumed
                - response_text: the response that was sent (for logging/storage)
        """
        from smarter_dev.bot.utils.messages import ConversationContextBuilder

        try:
            # Build truncated context (5 messages) for fast, cost-effective agent
            # Agent can call generate_engagement_plan() to get full context (20 messages) if needed
            # If since_message_id is provided (restart), fetch messages since that ID for continuity
            context_builder = ConversationContextBuilder(bot, guild_id)
            context = await context_builder.build_truncated_context(
                channel_id,
                trigger_message_id,
                limit=5 if not since_message_id else 50,  # Fetch more messages on restart for catch-up
                since_message_id=int(since_message_id) if since_message_id else None
            )

            # Store the last message ID for restart continuity
            if context.get("last_message_id"):
                channel_state_mgr = get_channel_state_manager()
                channel_state_mgr.set_last_context_message_id(channel_id, context["last_message_id"])

            # Create context-bound tools for this specific mention
            tools, channel_queries = create_mention_tools(
                bot=bot,
                channel_id=str(channel_id),
                guild_id=str(guild_id) if guild_id else "",
                trigger_message_id=str(trigger_message_id) if trigger_message_id else ""
            )

            # Create ReAct agent with context-bound tools
            # Very high max_iters (effectively infinite) allows agent to:
            # - Activate typing indicator and do complex tasks
            # - Do research/analysis across multiple message exchanges
            # - React to messages with multiple reactions/sends per message
            # - Handle large message backlogs efficiently
            # Agent naturally stops when wait_for_messages() hits 100 message threshold
            # and sets continue_monitoring to False, or when max_iters is exhausted

            # Use context manager to ensure this agent uses Gemini, not Claude
            with dspy.context(lm=MENTION_AGENT_LM, track_usage=True):
                react_agent = dspy.ReAct(
                    self._agent_signature,
                    tools=tools,
                    max_iters=1000
                )

                # Generate response using the ReAct agent (agent will call send_message tool)
                # Use acall() for async execution of tools
                result = await react_agent.acall(
                    conversation_timeline=context["conversation_timeline"],
                    users=context["users"],
                    channel=context["channel"],
                    me=context["me"],
                    recent_search_queries=channel_queries,
                    messages_remaining=messages_remaining,
                    is_continuation=is_continuation,
                    previous_summary=previous_summary
                )

            logger.debug(f"ReAct agent result: {result}")
            logger.debug(f"ReAct response text: {result.response}")

            # Check if the agent decided to skip due to controversial content
            if result.response.strip() == "SKIP_RESPONSE":
                logger.info("Agent decided to skip response due to sensitive content")
                return False, 0, None

            # Extract token usage
            tokens_used = self._extract_token_usage(result)

            if tokens_used == 0:
                tokens_used = self._estimate_tokens(result.response)
                logger.debug(f"Using estimated token count: {tokens_used}")

            # Validate response length (ensure it fits Discord constraints)
            response_text = self._validate_response_length(result.response)

            logger.info(f"MentionAgent generated response via ReAct: {len(response_text)} chars, {tokens_used} tokens")

            # Agent has already sent the message via send_message tool
            # Return success indicator, token count, and response text for logging
            return True, tokens_used, response_text

        except Exception as e:
            logger.error(f"Error in MentionAgent.generate_response: {e}", exc_info=True)
            return False, 0, None


# Global mention agent instance
mention_agent = MentionAgent()
