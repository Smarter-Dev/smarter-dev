import html
import logging
import re
from datetime import datetime
from datetime import timedelta
from typing import Optional

import dspy
import hikari
from pydantic import BaseModel

logger = logging.getLogger(__name__)


from ..llm_config import get_llm_model
from ..llm_config import get_model_info

# Configure LLM model from environment
lm = get_llm_model("default")
dspy.configure(lm=lm, track_usage=True)

# Log which model is being used
model_info = get_model_info("default")
logger.info(f"🤖 Bot using LLM model: {model_info['model_name']} (provider: {model_info['provider']})")


class DiscordMessage(BaseModel):
    """Represents a Discord message for context."""
    author: str
    author_id: str | None = None  # Discord user ID as string
    timestamp: datetime
    content: str
    # Reply context - populated when this message is a reply to another message
    replied_to_author: str | None = None  # Author of the message being replied to
    replied_to_content: str | None = None  # Content of the message being replied to
    # Channel context information
    channel_name: str | None = None  # Name of the channel
    channel_description: str | None = None  # Channel description/topic
    channel_type: str | None = None  # Channel type (text, forum, etc.)
    # User role information
    author_roles: list[str] = []  # List of role names for the message author
    # Forum post context
    is_original_poster: bool = False  # True if this is the OP in a forum thread


def parse_reply_context(content: str) -> tuple[str | None, str | None, str]:
    """Parse reply context from formatted message content.
    
    Args:
        content: Message content that may contain reply context in format:
                "> Author: replied content...\nActual message content"
    
    Returns:
        Tuple[replied_author, replied_content, actual_content]: 
        - replied_author: Author of the message being replied to (None if no reply)
        - replied_content: Content of the message being replied to (None if no reply)  
        - actual_content: The actual message content without reply context
    """
    # Check if message starts with reply context (> Author: content)
    reply_pattern = r"^> ([^:]+): (.+?)(?:\.\.\.)?\n(.*)$"
    match = re.match(reply_pattern, content, re.DOTALL)

    if match:
        replied_author = match.group(1).strip()
        replied_content = match.group(2).strip()
        actual_content = match.group(3).strip()
        return replied_author, replied_content, actual_content

    # Check for attachment/embed reply format
    attachment_pattern = r"^> ([^:]+): \[attachment/embed\]\n(.*)$"
    match = re.match(attachment_pattern, content, re.DOTALL)

    if match:
        replied_author = match.group(1).strip()
        replied_content = "[attachment/embed]"
        actual_content = match.group(2).strip()
        return replied_author, replied_content, actual_content

    # Check for generic reply indicator
    generic_pattern = r"^> \[replied to message\]\n(.*)$"
    match = re.match(generic_pattern, content, re.DOTALL)

    if match:
        replied_author = "[unknown]"
        replied_content = "[message]"
        actual_content = match.group(1).strip()
        return replied_author, replied_content, actual_content

    # No reply context found
    return None, None, content


class RateLimiter:
    """In-memory rate limiter for user requests and token usage with command-specific limits."""

    def __init__(self):
        # Track by command type: user_id -> command_type -> [timestamps]
        self.user_command_requests: dict[str, dict[str, list[datetime]]] = {}
        # Track token usage with command type: [(timestamp, token_count, command_type)]
        self.token_usage: list[tuple[datetime, int, str]] = []

        # Command-specific limits
        self.COMMAND_LIMITS = {
            "help": {"limit": 10, "window": timedelta(minutes=30)},
            "tldr": {"limit": 5, "window": timedelta(hours=1)}
        }

        # Global token limits
        self.TOKEN_LIMIT = 500_000  # tokens per hour
        self.TOKEN_WINDOW = timedelta(hours=1)

    def cleanup_expired_entries(self):
        """Remove expired entries to prevent memory leaks."""
        now = datetime.now()

        # Clean up user command requests
        for user_id in list(self.user_command_requests.keys()):
            user_commands = self.user_command_requests[user_id]

            for command_type in list(user_commands.keys()):
                if command_type in self.COMMAND_LIMITS:
                    window = self.COMMAND_LIMITS[command_type]["window"]
                    user_commands[command_type] = [
                        req_time for req_time in user_commands[command_type]
                        if now - req_time < window
                    ]

                    # Remove empty command lists
                    if not user_commands[command_type]:
                        del user_commands[command_type]

            # Remove empty user entries
            if not user_commands:
                del self.user_command_requests[user_id]

        # Clean up token usage
        self.token_usage = [
            (usage_time, tokens, cmd_type) for usage_time, tokens, cmd_type in self.token_usage
            if now - usage_time < self.TOKEN_WINDOW
        ]

    def check_user_limit(self, user_id: str, command_type: str = "help") -> bool:
        """Check if user is within rate limit for specific command type."""
        if command_type not in self.COMMAND_LIMITS:
            return True  # No limit defined for this command

        self.cleanup_expired_entries()
        user_commands = self.user_command_requests.get(user_id, {})
        user_requests = user_commands.get(command_type, [])
        limit = self.COMMAND_LIMITS[command_type]["limit"]
        return len(user_requests) < limit

    def check_token_limit(self, estimated_tokens: int = 1000) -> bool:
        """Check if we're within global token usage limit."""
        self.cleanup_expired_entries()
        # Sum actual token usage in the last hour across all commands
        current_usage = sum(tokens for _, tokens, _ in self.token_usage)
        return current_usage + estimated_tokens < self.TOKEN_LIMIT

    def record_request(self, user_id: str, tokens_used: int, command_type: str = "help"):
        """Record a user request and actual token usage for specific command type."""
        now = datetime.now()

        # Initialize user tracking if needed
        if user_id not in self.user_command_requests:
            self.user_command_requests[user_id] = {}
        if command_type not in self.user_command_requests[user_id]:
            self.user_command_requests[user_id][command_type] = []

        # Record the request
        self.user_command_requests[user_id][command_type].append(now)

        # Always record token usage with command type
        self.token_usage.append((now, tokens_used, command_type))

    def get_user_remaining_requests(self, user_id: str, command_type: str = "help") -> int:
        """Get number of remaining requests for user and command type."""
        if command_type not in self.COMMAND_LIMITS:
            return 999  # Unlimited for unknown commands

        self.cleanup_expired_entries()
        user_commands = self.user_command_requests.get(user_id, {})
        used = len(user_commands.get(command_type, []))
        limit = self.COMMAND_LIMITS[command_type]["limit"]
        return max(0, limit - used)

    def get_user_reset_time(self, user_id: str, command_type: str = "help") -> datetime | None:
        """Get when user's rate limit resets for specific command type."""
        if command_type not in self.COMMAND_LIMITS:
            return None

        user_commands = self.user_command_requests.get(user_id, {})
        user_requests = user_commands.get(command_type, [])
        if not user_requests:
            return None
        window = self.COMMAND_LIMITS[command_type]["window"]
        return user_requests[0] + window

    def get_current_token_usage(self) -> int:
        """Get current token usage in the last hour across all commands."""
        self.cleanup_expired_entries()
        return sum(tokens for _, tokens, _ in self.token_usage)

    def get_token_usage_by_command(self) -> dict[str, int]:
        """Get current token usage broken down by command type."""
        self.cleanup_expired_entries()
        usage_by_command = {}
        for _, tokens, command_type in self.token_usage:
            usage_by_command[command_type] = usage_by_command.get(command_type, 0) + tokens
        return usage_by_command


# Global rate limiter instance
rate_limiter = RateLimiter()



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


class HelpAgentSignature(dspy.Signature):
    """You are a helpful Discord bot assistant for the Smarter Dev community. You help users understand and use the bot's bytes economy and squad management systems.

    ## IMPORTANT: UNDERSTANDING CONTEXT & FOLLOW-UPS
    You receive structured conversation data to understand context:
    
    - **Your previous messages**: Find messages where author_id matches me.bot_id
    - **User information**: Use users list to get role names and understand who you're talking to
    - **Reply threads**: Use reply_to_message to see what messages are responses to others
    - **New vs old messages**: Use is_new to identify recent messages that triggered this interaction
    - **Channel context**: Use channel.name and channel.description to understand the setting
    
    When responding:
    - If user is replying to YOUR previous message, acknowledge what you said before
    - Be conversational for follow-ups: "Yeah, about that...", "Right, so what I meant was..."
    - If user seems confused about your previous response, rephrase or explain differently
    - When user builds on your answer, acknowledge: "Exactly!", "That's right", "Good thinking"

    ## BYTES ECONOMY SYSTEM
    The bytes economy is a server currency system where users earn and spend "bytes."

    ### Available Commands:
    1. `/bytes balance` - Check your current bytes balance
       - Shows: current balance, streak count, last daily claim, total received/sent
       - No parameters required
       - Response: Private message with share option

    2. `/bytes send <user> <amount> [reason]` - Send bytes to another user
       - `user` (required): The user to send bytes to
       - `amount` (required): Amount to send (1-10,000 bytes)
       - `reason` (optional): Reason for the transfer
       - Restrictions: Cannot send to yourself, recipient must be in server, cooldown applies
       - Response: Public success message or private error

    3. `/bytes leaderboard [limit]` - View server bytes leaderboard
       - `limit` (optional): Number of users to show (1-25, default: 10)
       - Response: Private message with share option

    4. `/bytes history [limit]` - View your transaction history
       - `limit` (optional): Number of transactions (1-20, default: 10)
       - Response: Private message with share option

    5. `/bytes info` - View server economy settings
       - Shows: starting balance, daily amount, transfer limits, cooldowns
       - Response: Private message

    6. **Context Menu**: "Send Bytes" - Right-click any message to send bytes to its author
       - Quick way to tip someone for helpful messages
       - Opens interactive form to send bytes

    ### How Bytes Work:
    - **Starting Balance**: New users get a starting balance (usually 128 bytes)
    - **Daily Rewards**: Users get a daily bytes reward for their first message every day UTC time (you leave a reaction on this message to let the user know they've claimed their daily reward)
    - **Transfers**: Send bytes to other users (may have cooldowns)
    - **Streaks**: Consecutive daily rewards can recieve streak multipliers
    - **Squad Costs**: Some squads require bytes to join

    ## SQUAD MANAGEMENT SYSTEM
    Squads are team-based groups within Discord servers.

    ### Available Commands:
    1. `/squads list` - View all available squads
       - Shows: squad names, member counts, join costs, descriptions
       - Highlights your current squad if you're in one
       - Response: Private message with share option

    2. `/squads join` - Join a squad interactively
       - Opens dropdown menu of available squads
       - Shows join costs and your current balance
       - 60-second timeout for selection
       - Response: Private interactive message

    3. `/squads info` - Get details about your current squad
       - Shows: squad info, member list, your role
       - Must be in a squad to use
       - Response: Private message

    4. `/squads members [squad]` - View squad members
       - `squad` (optional): Squad name (autocomplete available)
       - If no squad specified, shows your current squad
       - Response: Private message

    ### How Squads Work:
    - **Single Membership**: You can only be in one squad per server
    - **Join Costs**: Some squads require bytes to join
    - **Capacity Limits**: Squads may have maximum member limits
    - **Role Management**: Bot automatically manages Discord roles
    - **Switching**: You can switch squads (may cost bytes)

    ## CHALLENGE SYSTEM
    Competitive challenges/campaigns with scoring and leaderboards.

    ### Available Commands:
    1. `/challenges scoreboard` - View the current challenge scoreboard
       - Shows ranking of participants in the most recent campaign
       - Displays points/scores for active challenges
       - Response: Private message with share option

    2. `/challenges breakdown` - View detailed scoreboard with points breakdown
       - Shows challenge-by-challenge point breakdown for participants
       - More detailed view than the basic scoreboard
       - Response: Private message with share option

    3. `/challenges event` - View current challenge event/campaign information
       - Shows information about the current running challenge/campaign
       - Displays current active challenge details and timing
       - Response: Private message with share option

    ### How Challenges Work:
    - **Campaigns**: Time-based competitive events with multiple challenges
    - **Scoring**: Points awarded for completing challenges or achieving goals
    - **Leaderboards**: Track rankings and compare performance with others
    - **Events**: Current/active challenges and campaign information

    ## OTHER COMMANDS
    1. `/tldr [limit]` - Summarize the recent messages in the channel
       - `limit` (optional): Number of messages to summarize (1-20, default: 5)
       - Uses AI to create concise summaries of channel conversations
       - Response: Private message with share option

    2. `/help [question]` - Get help with the bot's features and commands
       - `question` (optional): Specific question about bot functionality
       - Provides AI-powered assistance and command explanations
       - If no question provided, gives general overview
       - Response: Private message

    ## COMMON ISSUES & SOLUTIONS:

    ### Cooldown Errors:
    - **Problem**: "Transfer cooldown active"
    - **Cause**: Recent transfer to another user
    - **Solution**: Wait for cooldown to expire, check `/bytes info` for cooldown settings

    ### Insufficient Balance:
    - **Problem**: "Insufficient balance"
    - **Cause**: Trying to send more bytes than you have
    - **Solution**: Check `/bytes balance`, claim daily bytes, or request bytes from others

    ### Squad Issues:
    - **Problem**: "Squad is full"
    - **Solution**: Try a different squad or wait for spots to open
    - **Problem**: "Already in squad"
    - **Solution**: Leave current squad first (if supported) or contact admins

    ### Command Not Working:
    - **Problem**: Commands not responding
    - **Solution**: Check bot is online, try again in a few minutes, contact admins

    ## CRITICAL: CHARACTER LIMIT ENFORCEMENT
    🚨 **DISCORD CHARACTER LIMIT: Your response MUST be under 2000 characters.** 🚨
    - Count characters as you write and adjust content to fit within this strict limit
    - If your response would exceed 2000 characters, condense, summarize, or break into key points
    - Never send responses that exceed 2000 characters - Discord will reject them
    - This is a strict platform constraint that cannot be violated

    ## RESPONSE GUIDELINES:
    - Be helpful and friendly, more conversational for follow-ups
    - Provide specific command examples when introducing new concepts
    - Explain restrictions and limits clearly
    - Use the conversation context to give relevant answers and acknowledge previous interactions
    - Keep responses concise but informative
    - Use emojis sparingly and appropriately
    - If the user asks a question, answer it. If the user asks for help, provide help.
    - If the user goes off topic, play along and keep the conversation going, gently redirect them back to the topic.
    - When continuing a conversation, reference what was discussed before: "Like I mentioned...", "Building on what we talked about...", "Going back to your question about..."
    - Match the user's energy level - if they're excited, be enthusiastic; if they're confused, be patient and helpful
    """

    conversation_timeline: str = dspy.InputField(description="Chronological conversation timeline showing message flow, replies, timestamps, and [NEW] markers for recent activity")
    users: list[dict] = dspy.InputField(description="List of users with user_id, discord_name, nickname, server_nickname, role_names, is_bot fields") 
    channel: dict = dspy.InputField(description="Channel info with name and description fields")
    me: dict = dspy.InputField(description="Bot info with bot_name and bot_id fields")
    user_question: str = dspy.InputField(description="The user's question about the bot")
    messages_remaining: int = dspy.InputField(description="Number of help messages user can send after this one (0 = this is their last message)")
    response: str = dspy.OutputField(description="CRITICAL: Your response MUST be under 2000 characters. Discord has a strict 2000 character limit. Count characters and adjust content to fit within this limit. Helpful response explaining bot functionality")


class HelpAgent:
    """Discord bot help agent using Gemini for conversational assistance."""

    def __init__(self):
        self._help_agent = dspy.ChainOfThought(HelpAgentSignature)
        self._mention_agent = dspy.ChainOfThought(ConversationalMentionSignature)

    async def generate_response(
        self,
        user_question: str,
        bot: hikari.GatewayBot,
        channel_id: int,
        guild_id: Optional[int] = None,
        trigger_message_id: Optional[int] = None,
        interaction_type: str = "slash_command",
        messages_remaining: int = 10
    ) -> tuple[str, int]:
        """Generate a helpful response using structured conversation context.
        
        Args:
            user_question: The user's question about the bot
            bot: Discord bot instance for fetching context
            channel_id: Channel ID to gather context from
            guild_id: Guild ID for user/role information
            trigger_message_id: Message ID that triggered this response
            interaction_type: "slash_command" for /help, "mention" for @mentions
            messages_remaining: Number of help messages user can send after this one
            
        Returns:
            tuple[str, int]: Generated response and token usage count
        """
        # Format context messages using enhanced XML structure with channel and role info
        context_str = ""
        if context_messages:
            context_lines = []
            
            # Add channel context info at the top
            first_msg = context_messages[0] if context_messages else None
            if first_msg and (first_msg.channel_name or first_msg.channel_description):
                channel_context_parts = []
                if first_msg.channel_name:
                    channel_context_parts.append(f"<channel-name>{html.escape(first_msg.channel_name)}</channel-name>")
                if first_msg.channel_description:
                    channel_context_parts.append(f"<channel-description>{html.escape(first_msg.channel_description)}</channel-description>")
                if first_msg.channel_type:
                    channel_context_parts.append(f"<channel-type>{html.escape(first_msg.channel_type)}</channel-type>")
                
                if channel_context_parts:
                    context_lines.append(f"<channel-context>\n{chr(10).join(channel_context_parts)}\n</channel-context>")
            
            for msg in context_messages[-10:]:  # Last 10 messages
                sent_str = msg.timestamp.isoformat()

                # Check if this is a bot message by comparing IDs
                is_bot_message = msg.author_id and msg.author_id == bot_id
                
                # Build message structure with new fields
                message_parts = [
                    f"<sent>{html.escape(sent_str)}</sent>",
                    f"<author>{html.escape(msg.author)}</author>"
                ]
                
                # Add role information for non-bot users
                if not is_bot_message and msg.author_roles:
                    roles_str = ", ".join(msg.author_roles)
                    message_parts.append(f"<author-roles>{html.escape(roles_str)}</author-roles>")

                # Add reply context if present
                if msg.replied_to_author and msg.replied_to_content:
                    message_parts.append(
                        f"<replying-to>"
                        f"<author>{html.escape(msg.replied_to_author)}</author>"
                        f"<content>{html.escape(msg.replied_to_content)}</content>"
                        f"</replying-to>"
                    )
                
                # Add message content
                message_parts.append(f"<content>{html.escape(msg.content)}</content>")
                
                # Combine into message element
                message_xml = (
                    f"<message{' from-bot="true"' if is_bot_message else ''}"
                    f"{' is-op="true"' if msg.is_original_poster else ''}>"
                    f"{chr(10).join(message_parts)}"
                    f"</message>"
                )
                context_lines.append(message_xml)
                
            context_str = "\n".join(context_lines)

        # Generate response using appropriate agent based on interaction type
        if interaction_type == "mention":
            # Use conversational mention agent with built-in content filtering
            result = self._mention_agent(
                context_messages=f"<history>{context_str}</history>",
                user_mention=user_question,
                messages_remaining=messages_remaining
            )

            # Check if the agent decided to skip due to controversial content
            if result.response.strip() == "SKIP_RESPONSE":
                return "", 0
        else:
            # Use detailed help agent for slash commands
            result = self._help_agent(
                context_messages=f"<history>{context_str}</history>",
                user_question=user_question,
                messages_remaining=messages_remaining
            )

        # Get token usage from DSPy prediction using robust extraction methods
        tokens_used = 0

        # Method 1: Use DSPy's built-in get_lm_usage() method (preferred approach)
        try:
            usage_data = result.get_lm_usage()
            if usage_data:
                # Extract tokens from the usage data dictionary
                for model_name, usage_info in usage_data.items():
                    if isinstance(usage_info, dict):
                        if "total_tokens" in usage_info:
                            tokens_used += usage_info["total_tokens"]
                        elif "prompt_tokens" in usage_info and "completion_tokens" in usage_info:
                            tokens_used += usage_info["prompt_tokens"] + usage_info["completion_tokens"]
                        elif "input_tokens" in usage_info and "output_tokens" in usage_info:
                            tokens_used += usage_info["input_tokens"] + usage_info["output_tokens"]
        except Exception as e:
            logger.debug(f"HELP DEBUG: Error with get_lm_usage(): {e}")

        # Method 2: Extract from LM history (fallback for Gemini API bug)
        if tokens_used == 0:
            try:
                current_lm = dspy.settings.lm
                if current_lm and hasattr(current_lm, "history") and current_lm.history:
                    latest_entry = current_lm.history[-1]  # Get the most recent API call

                    # Check response.usage in history (most reliable for Gemini)
                    if "response" in latest_entry and hasattr(latest_entry["response"], "usage"):
                        response_usage = latest_entry["response"].usage
                        if hasattr(response_usage, "total_tokens"):
                            tokens_used = response_usage.total_tokens
                        elif hasattr(response_usage, "prompt_tokens") and hasattr(response_usage, "completion_tokens"):
                            tokens_used = response_usage.prompt_tokens + response_usage.completion_tokens

                    # Check for usage field in history
                    elif "usage" in latest_entry and latest_entry["usage"]:
                        usage = latest_entry["usage"]
                        if isinstance(usage, dict):
                            if "total_tokens" in usage:
                                tokens_used = usage["total_tokens"]
                            elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                tokens_used = usage["prompt_tokens"] + usage["completion_tokens"]

                    # Fallback: estimate from cost (Gemini-specific)
                    elif "cost" in latest_entry and latest_entry["cost"] > 0:
                        cost = latest_entry["cost"]
                        # Rough estimation: Gemini Flash pricing ~$0.075 per million tokens
                        estimated_tokens = int(cost * 13333333)
                        tokens_used = estimated_tokens

            except Exception as e:
                logger.debug(f"HELP DEBUG: Error extracting from LM history: {e}")

        # Method 3: Legacy fallback for other DSPy completion formats
        if tokens_used == 0 and hasattr(result, "_completions") and result._completions:
            for completion in result._completions:
                # Method 1: Traditional kwargs.usage approach
                if hasattr(completion, "kwargs") and completion.kwargs:
                    if "usage" in completion.kwargs:
                        usage = completion.kwargs["usage"]
                        if hasattr(usage, "total_tokens"):
                            tokens_used += usage.total_tokens
                        elif isinstance(usage, dict):
                            # Try different token field names
                            if "total_tokens" in usage:
                                tokens_used += usage["total_tokens"]
                            elif "totalTokens" in usage:
                                tokens_used += usage["totalTokens"]
                            elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                tokens_used += usage["prompt_tokens"] + usage["completion_tokens"]

                    # Method 2: Check for response metadata
                    if "response" in completion.kwargs:
                        response_obj = completion.kwargs["response"]
                        if hasattr(response_obj, "usage") and hasattr(response_obj.usage, "total_tokens"):
                            tokens_used += response_obj.usage.total_tokens

        # Final fallback: estimate tokens from text length if all methods fail
        if tokens_used == 0:
            # Rough estimation: ~4 characters per token for English text
            input_text = f"{context_str}\n{user_question}"
            output_text = result.response
            estimated_tokens = (len(input_text) + len(output_text)) // 4
            tokens_used = estimated_tokens
            logger.warning(f"HELP DEBUG: Fallback estimation - {tokens_used} tokens from text length")

        # Check character limit and retry if needed
        response = self._validate_and_fix_response_length(
            result.response,
            context_str,
            user_question,
            messages_remaining,
            interaction_type
        )

        return response, tokens_used

    def _extract_token_usage(self, result) -> int:
        """Extract token usage from DSPy prediction result.

        Supports multiple LLM providers including Claude (Anthropic), Gemini, and OpenAI.
        Returns the total number of tokens used (prompt + completion).
        """
        tokens_used = 0

        # Method 1: Use DSPy's built-in get_lm_usage() method
        try:
            usage_data = result.get_lm_usage()
            if usage_data:
                total_tokens = usage_data.get('total_tokens', 0)
                completion_tokens = usage_data.get('completion_tokens', 0)
                prompt_tokens = usage_data.get('prompt_tokens', 0)

                tokens_used = total_tokens if total_tokens else (prompt_tokens + completion_tokens)
                logger.debug(f"DSPy usage data: {usage_data}, extracted tokens: {tokens_used}")
                return tokens_used
        except (AttributeError, TypeError) as e:
            logger.debug(f"DSPy get_lm_usage() not available or empty: {e}")

        # Method 2: Fallback to token counting from prediction object
        # This handles Claude/Anthropic, Gemini, and other provider-specific response structures
        try:
            if hasattr(result, '_completions') and result._completions:
                completion = result._completions[0]
                if hasattr(completion, '_usage') and completion._usage:
                    usage = completion._usage
                    total_tokens = getattr(usage, 'total_tokens', None)
                    completion_tokens = getattr(usage, 'completion_tokens', None)
                    prompt_tokens = getattr(usage, 'prompt_tokens', None)

                    tokens_used = total_tokens if total_tokens else (
                        (prompt_tokens or 0) + (completion_tokens or 0)
                    )
                    logger.debug(f"Completion usage: {usage}, extracted tokens: {tokens_used}")
                    return tokens_used
        except (AttributeError, TypeError) as e:
            logger.debug(f"Failed to extract usage from completions: {e}")

        # Method 3: Check for Claude/Anthropic specific response structure
        try:
            if hasattr(result, 'completion') and result.completion:
                # Some versions of DSPy may structure Claude responses differently
                completion_obj = result.completion
                if hasattr(completion_obj, 'usage'):
                    usage = completion_obj.usage
                    tokens_used = getattr(usage, 'input_tokens', 0) + getattr(usage, 'output_tokens', 0)
                    if tokens_used > 0:
                        logger.debug(f"Claude usage extracted from completion.usage: {tokens_used}")
                        return tokens_used
        except (AttributeError, TypeError) as e:
            logger.debug(f"Failed to extract Claude-specific usage: {e}")

        return tokens_used

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count for text (approximately 4 chars per token)."""
        return len(text) // 4

    async def generate_response_new(
        self,
        user_question: str,
        bot: hikari.GatewayBot,
        channel_id: int,
        guild_id: Optional[int] = None,
        trigger_message_id: Optional[int] = None,
        interaction_type: str = "slash_command",
        messages_remaining: int = 10
    ) -> tuple[str, int]:
        """Generate a helpful response using structured conversation context.
        
        Args:
            user_question: The user's question about the bot
            bot: Discord bot instance for fetching context
            channel_id: Channel ID to gather context from
            guild_id: Guild ID for user/role information
            trigger_message_id: Message ID that triggered this response
            interaction_type: "slash_command" for /help, "mention" for @mentions
            messages_remaining: Number of help messages user can send after this one
            
        Returns:
            tuple[str, int]: Generated response and token usage count
        """
        from .utils.messages import ConversationContextBuilder
        
        # Build structured context using our new builder
        context_builder = ConversationContextBuilder(bot, guild_id)
        context = await context_builder.build_context(channel_id, trigger_message_id)

        # Generate response using appropriate agent based on interaction type
        if interaction_type == "mention":
            # Use conversational mention agent
            result = self._mention_agent(
                conversation_timeline=context["conversation_timeline"],
                users=context["users"],
                channel=context["channel"],
                me=context["me"],
                messages_remaining=messages_remaining
            )

            # Check if the agent decided to skip due to controversial content
            if result.response.strip() == "SKIP_RESPONSE":
                return "", 0
        else:
            # Use detailed help agent for slash commands
            result = self._help_agent(
                conversation_timeline=context["conversation_timeline"],
                users=context["users"],
                channel=context["channel"],
                me=context["me"],
                user_question=user_question,
                messages_remaining=messages_remaining
            )

        # Get token usage from DSPy prediction
        tokens_used = self._extract_token_usage(result)
        
        if tokens_used == 0:
            tokens_used = self._estimate_tokens(result.response)
            logger.debug(f"Using estimated token count: {tokens_used}")

        logger.info(f"Agent response generated: {len(result.response)} chars, {tokens_used} tokens")

        # Format and return response
        response = self._format_response_new(
            result.response,
            context,
            user_question,
            messages_remaining,
            interaction_type
        )

        return response, tokens_used

    def _format_response_new(self, response: str, context: dict, user_question: str, messages_remaining: int, interaction_type: str) -> str:
        """Format the response for Discord (new version that works with structured context)."""
        # For now, just return the response as-is
        # We can enhance this later if needed
        return response

    async def generate_response_async(
        self,
        user_question: str,
        context_messages: list[DiscordMessage] = None,
        bot_id: str = None,
        interaction_type: str = "slash_command",
        messages_remaining: int = 10
    ) -> tuple[str, int]:
        """Async version of generate_response to avoid blocking the event loop.
        
        Args:
            user_question: The user's question about the bot
            context_messages: Recent conversation messages for context
            bot_id: The bot's Discord user ID for identifying its messages
            interaction_type: "slash_command" for /help, "mention" for @mentions
            messages_remaining: Number of help messages user can send after this one
            
        Returns:
            tuple[str, int]: Generated response and token usage count
        """
        # Create async versions of our agents using dspy.asyncify
        async_help_agent = dspy.asyncify(self._help_agent)
        async_mention_agent = dspy.asyncify(self._mention_agent)

        # Format context messages using enhanced XML structure with channel and role info
        context_str = ""
        if context_messages:
            context_lines = []
            
            # Add channel context info at the top
            first_msg = context_messages[0] if context_messages else None
            if first_msg and (first_msg.channel_name or first_msg.channel_description):
                channel_context_parts = []
                if first_msg.channel_name:
                    channel_context_parts.append(f"<channel-name>{html.escape(first_msg.channel_name)}</channel-name>")
                if first_msg.channel_description:
                    channel_context_parts.append(f"<channel-description>{html.escape(first_msg.channel_description)}</channel-description>")
                if first_msg.channel_type:
                    channel_context_parts.append(f"<channel-type>{html.escape(first_msg.channel_type)}</channel-type>")
                
                if channel_context_parts:
                    context_lines.append(f"<channel-context>\n{chr(10).join(channel_context_parts)}\n</channel-context>")
            
            for msg in context_messages[-10:]:  # Last 10 messages
                sent_str = msg.timestamp.isoformat()

                # Check if this is a bot message by comparing IDs
                is_bot_message = msg.author_id and msg.author_id == bot_id
                
                # Build message structure with new fields
                message_parts = [
                    f"<sent>{html.escape(sent_str)}</sent>",
                    f"<author>{html.escape(msg.author)}</author>"
                ]
                
                # Add role information for non-bot users
                if not is_bot_message and msg.author_roles:
                    roles_str = ", ".join(msg.author_roles)
                    message_parts.append(f"<author-roles>{html.escape(roles_str)}</author-roles>")
                
                # Add OP indicator for forum threads
                if msg.is_original_poster:
                    message_parts.append("<is-op>true</is-op>")

                # Add reply context if present
                if msg.replied_to_author and msg.replied_to_content:
                    message_parts.append(
                        f"<replying-to>"
                        f"<replied-author>{html.escape(msg.replied_to_author)}</replied-author>"
                        f"<replied-content>{html.escape(msg.replied_to_content)}</replied-content>"
                        f"</replying-to>"
                    )
                
                # Add message content
                message_parts.append(f"<content>{html.escape(msg.content)}</content>")
                
                # Combine into message element
                message_xml = (
                    f"<message{' from-bot="true"' if is_bot_message else ''}>"
                    f"{chr(10).join(message_parts)}"
                    f"</message>"
                )
                context_lines.append(message_xml)
                
            context_str = "\n".join(context_lines)

        # Generate response using appropriate agent based on interaction type
        if interaction_type == "mention":
            # Use async conversational mention agent with built-in content filtering
            result = await async_mention_agent(
                context_messages=f"<history>{context_str}</history>",
                user_mention=user_question,
                messages_remaining=messages_remaining
            )

            # Check if the agent decided to skip due to controversial content
            if result.response.strip() == "SKIP_RESPONSE":
                return "", 0
        else:
            # Use async detailed help agent for slash commands
            result = await async_help_agent(
                context_messages=f"<history>{context_str}</history>",
                user_question=user_question,
                messages_remaining=messages_remaining
            )

        # Get token usage using the same logic as sync version
        tokens_used = 0

        # Method 1: Use DSPy's built-in get_lm_usage() method (preferred approach)
        try:
            usage_data = result.get_lm_usage()
            if usage_data:
                # Extract tokens from the usage data dictionary
                for model_name, usage_info in usage_data.items():
                    if isinstance(usage_info, dict):
                        if "total_tokens" in usage_info:
                            tokens_used += usage_info["total_tokens"]
                        elif "prompt_tokens" in usage_info and "completion_tokens" in usage_info:
                            tokens_used += usage_info["prompt_tokens"] + usage_info["completion_tokens"]
                        elif "input_tokens" in usage_info and "output_tokens" in usage_info:
                            tokens_used += usage_info["input_tokens"] + usage_info["output_tokens"]
        except Exception as e:
            logger.debug(f"HELP DEBUG: Error with get_lm_usage(): {e}")

        # Method 2: Extract from LM history (fallback for Gemini API bug)
        if tokens_used == 0:
            try:
                current_lm = dspy.settings.lm
                if current_lm and hasattr(current_lm, "history") and current_lm.history:
                    latest_entry = current_lm.history[-1]  # Get the most recent API call

                    # Check response.usage in history (most reliable for Gemini)
                    if "response" in latest_entry and hasattr(latest_entry["response"], "usage"):
                        response_usage = latest_entry["response"].usage
                        if hasattr(response_usage, "total_tokens"):
                            tokens_used = response_usage.total_tokens
                        elif hasattr(response_usage, "prompt_tokens") and hasattr(response_usage, "completion_tokens"):
                            tokens_used = response_usage.prompt_tokens + response_usage.completion_tokens

                    # Check for usage field in history
                    elif "usage" in latest_entry and latest_entry["usage"]:
                        usage = latest_entry["usage"]
                        if isinstance(usage, dict):
                            if "total_tokens" in usage:
                                tokens_used = usage["total_tokens"]
                            elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                tokens_used = usage["prompt_tokens"] + usage["completion_tokens"]

                    # Fallback: estimate from cost (Gemini-specific)
                    elif "cost" in latest_entry and latest_entry["cost"] > 0:
                        cost = latest_entry["cost"]
                        # Rough estimation: Gemini Flash pricing ~$0.075 per million tokens
                        estimated_tokens = int(cost * 13333333)
                        tokens_used = estimated_tokens

            except Exception as e:
                logger.debug(f"HELP DEBUG: Error extracting from LM history: {e}")

        # Method 3: Legacy fallback for other DSPy completion formats
        if tokens_used == 0 and hasattr(result, "_completions") and result._completions:
            for completion in result._completions:
                # Method 1: Traditional kwargs.usage approach
                if hasattr(completion, "kwargs") and completion.kwargs:
                    if "usage" in completion.kwargs:
                        usage = completion.kwargs["usage"]
                        if hasattr(usage, "total_tokens"):
                            tokens_used += usage.total_tokens
                        elif isinstance(usage, dict):
                            # Try different token field names
                            if "total_tokens" in usage:
                                tokens_used += usage["total_tokens"]
                            elif "totalTokens" in usage:
                                tokens_used += usage["totalTokens"]
                            elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                tokens_used += usage["prompt_tokens"] + usage["completion_tokens"]

                    # Method 2: Check for response metadata
                    if "response" in completion.kwargs:
                        response_obj = completion.kwargs["response"]
                        if hasattr(response_obj, "usage"):
                            usage = response_obj.usage
                            if hasattr(usage, "total_tokens"):
                                tokens_used += usage.total_tokens
                            elif hasattr(usage, "prompt_tokens") and hasattr(usage, "completion_tokens"):
                                tokens_used += usage.prompt_tokens + usage.completion_tokens

        # Validate and enforce character limit with async validation
        response = await self._validate_and_fix_response_length_async(
            result.response,
            context_str,
            user_question,
            messages_remaining,
            interaction_type
        )

        return response, tokens_used

    def _validate_and_fix_response_length(
        self,
        response: str,
        context_str: str,
        user_question: str,
        messages_remaining: int,
        interaction_type: str
    ) -> str:
        """Validate response length and retry if over 2000 characters."""
        MAX_LENGTH = 2000

        # If response is within limit, return as-is
        if len(response) <= MAX_LENGTH:
            return response

        logger.warning(f"Response too long: {len(response)} characters (limit: {MAX_LENGTH})")

        # Try to get a shorter response
        chars_over = len(response) - MAX_LENGTH
        shortening_prompt = (
            f"Your previous response was {len(response)} characters, which exceeds Discord's "
            f"2000 character limit by {chars_over} characters. Please provide a shorter version "
            f"that covers the same key points but stays under 2000 characters. "
            f"Focus on the most important information and be more concise."
        )

        try:
            if interaction_type == "mention":
                retry_result = self._mention_agent(
                    context_messages=f"<history>{context_str}</history>",
                    user_mention=shortening_prompt,
                    messages_remaining=messages_remaining
                )
            else:
                retry_result = self._help_agent(
                    context_messages=f"<history>{context_str}</history>",
                    user_question=shortening_prompt,
                    messages_remaining=messages_remaining
                )

            # Check if retry is within limits
            if len(retry_result.response) <= MAX_LENGTH:
                logger.info(f"Successfully shortened response to {len(retry_result.response)} characters")
                return retry_result.response
            else:
                logger.warning(f"Retry still too long: {len(retry_result.response)} characters")

        except Exception as e:
            logger.error(f"Error during response shortening: {e}")

        # If retry failed or is still too long, return apology
        apology = (
            "Sorry, I'm having trouble keeping my response short enough for Discord's character limits. "
            "Could you try asking a more specific question, or I can break my answer into smaller parts?"
        )

        logger.warning("Sending apology due to repeated length violations")
        return apology

    async def _validate_and_fix_response_length_async(
        self,
        response: str,
        context_str: str,
        user_question: str,
        messages_remaining: int,
        interaction_type: str
    ) -> str:
        """Async version of response length validation to avoid blocking the event loop."""
        MAX_LENGTH = 2000

        # If response is within limit, return as-is
        if len(response) <= MAX_LENGTH:
            return response

        logger.warning(f"Response too long: {len(response)} characters (limit: {MAX_LENGTH})")

        # Try to get a shorter response
        chars_over = len(response) - MAX_LENGTH
        shortening_prompt = (
            f"Your previous response was {len(response)} characters, which exceeds Discord's "
            f"2000 character limit by {chars_over} characters. Please provide a shorter version "
            f"that covers the same key points but stays under 2000 characters. "
            f"Focus on the most important information and be more concise."
        )

        try:
            # Create async agents for retry
            async_help_agent = dspy.asyncify(self._help_agent)
            async_mention_agent = dspy.asyncify(self._mention_agent)

            if interaction_type == "mention":
                retry_result = await async_mention_agent(
                    context_messages=f"<history>{context_str}</history>",
                    user_mention=shortening_prompt,
                    messages_remaining=messages_remaining
                )
            else:
                retry_result = await async_help_agent(
                    context_messages=f"<history>{context_str}</history>",
                    user_question=shortening_prompt,
                    messages_remaining=messages_remaining
                )

            # Check if retry is within limits
            if len(retry_result.response) <= MAX_LENGTH:
                logger.info(f"Successfully shortened response to {len(retry_result.response)} characters")
                return retry_result.response
            else:
                logger.warning(f"Retry still too long: {len(retry_result.response)} characters")

        except Exception as e:
            logger.error(f"Error during async response shortening: {e}")

        # If retry failed or is still too long, return apology
        apology = (
            "Sorry, I'm having trouble keeping my response short enough for Discord's character limits. "
            "Could you try asking a more specific question, or I can break my answer into smaller parts?"
        )

        logger.warning("Sending apology due to repeated length violations")
        return apology


class TLDRAgentSignature(dspy.Signature):
    """You are a helpful Discord bot that creates well-organized, readable summaries of channel conversations.

    ## TASK
    Analyze the conversation type and adapt your summary style accordingly. Create a clear, scannable summary that's easy to read on Discord, keeping in mind that there was likely conversation before these messages.

    ## MESSAGE FORMAT
    Messages may include reply context with this structure:
    ```xml
    <message>
        <timestamp>01/15 12:30</timestamp>
        <author>Username</author>
        <replying-to>
            <replied-author>OriginalAuthor</replied-author>
            <replied-content>Original message content</replied-content>
        </replying-to>
        <content>The user's response to the original message</content>
    </message>
    ```
    
    When a message has `<replying-to>`, it means the user is responding to a previous message. Use this context to understand conversation flow and relationships between messages.

    ## ADAPTIVE RESPONSE STRATEGY
    **For Quick Back-and-Forth Chat** (short messages, casual talk):
    - Keep it brief and to the point, don't lose juicy details
    - 2-4 sentences max
    - Highlight any decisions or conclusions

    **For Detailed Discussions** (long messages, complex topics):
    - Structured breakdown with key points
    - Include important details and decisions
    - Use bullet points or short paragraphs for readability
    - 4-6 sentences organized clearly

    **For Mixed Conversations**:
    - Balance detail with brevity
    - Focus on the most important developments
    - 3-5 sentences with clear structure

    ## FORMATTING REQUIREMENTS
    - Start with "📝 **Channel Summary**"
    - **Use line breaks** to separate different topics or phases
    - **Bold key terms** or decisions for scanning
    - **Avoid walls of text** - break into digestible chunks
    - Include relevant usernames naturally

    ## EXAMPLE OUTPUTS
    
    **Quick Chat Example:**
    📝 **Channel Summary**
    Users discussed their favorite pizza toppings, with **pineapple** being surprisingly popular. Sarah shared a local restaurant recommendation that got several positive reactions.
    
    Most people agreed to try the new place for the next meetup.

    **Detailed Discussion Example:**
    📝 **Channel Summary**
    **Event Planning Issue**: Alice reported conflicts with the venue booking due to scheduling changes.

    **Root Cause**: Bob identified that the original date conflicts with a major community event.

    **Solution**: Team decided to move the meetup to the following weekend and update all announcements.
    
    Alice successfully rescheduled and confirmed the new venue is available.
    """

    messages: str = dspy.InputField(description="Discord messages to summarize, formatted as structured data")
    summary: str = dspy.OutputField(description="Comprehensive and detailed summary of the conversation")


class TLDRAgent:
    """Discord bot TLDR agent using Gemini for conversation summarization."""

    def __init__(self):
        self._agent = dspy.ChainOfThought(TLDRAgentSignature)

    def estimate_token_count(self, text: str) -> int:
        """Rough estimation of token count for text (approximately 4 chars per token)."""
        return len(text) // 4

    def truncate_message_content(self, content: str, max_chars: int = 500) -> str:
        """Truncate message content while preserving readability."""
        if len(content) <= max_chars:
            return content

        # Try to truncate at word boundaries
        truncated = content[:max_chars]
        last_space = truncated.rfind(" ")
        if last_space > max_chars * 0.8:  # If we find a space in the last 20%
            truncated = truncated[:last_space]

        return truncated + "..."

    def prepare_messages_for_context(
        self,
        messages: list[DiscordMessage],
        max_tokens: int = 15000
    ) -> tuple[str, int]:
        """Prepare messages for LLM context with progressive truncation.
        
        Args:
            messages: List of Discord messages to process
            max_tokens: Maximum tokens to use (leaving room for response)
            
        Returns:
            tuple[str, int]: Formatted message string and actual message count used
        """
        if not messages:
            return "<no-messages>No messages to summarize</no-messages>", 0

        # Start with full messages and progressively reduce if needed
        current_messages = messages.copy()

        while current_messages:
            # Format current set of messages with enhanced context
            formatted_lines = []
            
            # Add channel context info at the top for TLDR
            first_msg = current_messages[0] if current_messages else None
            if first_msg and (first_msg.channel_name or first_msg.channel_description):
                channel_context_parts = []
                if first_msg.channel_name:
                    channel_context_parts.append(f"<channel-name>{html.escape(first_msg.channel_name)}</channel-name>")
                if first_msg.channel_description:
                    channel_context_parts.append(f"<channel-description>{html.escape(first_msg.channel_description)}</channel-description>")
                if first_msg.channel_type:
                    channel_context_parts.append(f"<channel-type>{html.escape(first_msg.channel_type)}</channel-type>")
                
                if channel_context_parts:
                    formatted_lines.append(f"<channel-context>\n{chr(10).join(channel_context_parts)}\n</channel-context>")
            
            for msg in current_messages:
                sent_str = msg.timestamp.isoformat()

                # Truncate very long messages
                content = self.truncate_message_content(msg.content, 500)

                # Build message structure with new fields
                message_parts = [
                    f"<sent>{html.escape(sent_str)}</sent>",
                    f"<author>{html.escape(msg.author)}</author>"
                ]
                
                # Add role information for users with roles
                if msg.author_roles:
                    roles_str = ", ".join(msg.author_roles)
                    message_parts.append(f"<author-roles>{html.escape(roles_str)}</author-roles>")
                
                # Add OP indicator for forum threads
                if msg.is_original_poster:
                    message_parts.append("<is-op>true</is-op>")

                # Add reply context if present
                if msg.replied_to_author and msg.replied_to_content:
                    message_parts.append(
                        f"<replying-to>"
                        f"<replied-author>{html.escape(msg.replied_to_author)}</replied-author>"
                        f"<replied-content>{html.escape(msg.replied_to_content)}</replied-content>"
                        f"</replying-to>"
                    )
                
                # Add message content
                message_parts.append(f"<content>{html.escape(content)}</content>")
                
                # Combine into message element
                message_xml = (
                    f"<message>"
                    f"{chr(10).join(message_parts)}"
                    f"</message>"
                )
                formatted_lines.append(message_xml)

            formatted_text = f"<conversation>\n{chr(10).join(formatted_lines)}\n</conversation>"

            # Check if this fits within token limit
            estimated_tokens = self.estimate_token_count(formatted_text)

            if estimated_tokens <= max_tokens or len(current_messages) <= 3:
                # Either it fits, or we're down to minimum messages
                return formatted_text, len(current_messages)

            # Remove some messages and try again (remove from the oldest/beginning)
            reduction = max(1, len(current_messages) // 4)  # Remove 25% each iteration
            current_messages = current_messages[-len(current_messages) + reduction:]

        return "<no-messages>No messages could be processed</no-messages>", 0

    def generate_summary(
        self,
        messages: list[DiscordMessage],
        max_context_tokens: int = 15000
    ) -> tuple[str, int, int]:
        """Generate a TLDR summary of Discord messages.
        
        Args:
            messages: List of Discord messages to summarize
            max_context_tokens: Maximum tokens to use for context
            
        Returns:
            tuple[str, int, int]: Summary text, token usage, messages actually summarized
        """
        # Prepare messages with progressive truncation
        formatted_messages, messages_used = self.prepare_messages_for_context(
            messages, max_context_tokens
        )

        if messages_used == 0:
            return ("📝 **Channel Summary**\nNo messages found to summarize. The channel might be empty or contain only bot messages.\n\n*(Summarized 0 messages)*", 0, 0)

        try:
            # Generate summary
            result = self._agent(messages=formatted_messages)

            # Get token usage from DSPy prediction using robust extraction methods
            tokens_used = 0

            # Method 1: Use DSPy's built-in get_lm_usage() method (preferred approach)
            try:
                usage_data = result.get_lm_usage()
                if usage_data:
                    # Extract tokens from the usage data dictionary
                    for model_name, usage_info in usage_data.items():
                        if isinstance(usage_info, dict):
                            if "total_tokens" in usage_info:
                                tokens_used += usage_info["total_tokens"]
                            elif "prompt_tokens" in usage_info and "completion_tokens" in usage_info:
                                tokens_used += usage_info["prompt_tokens"] + usage_info["completion_tokens"]
                            elif "input_tokens" in usage_info and "output_tokens" in usage_info:
                                tokens_used += usage_info["input_tokens"] + usage_info["output_tokens"]
            except Exception as e:
                logger.debug(f"TLDR DEBUG: Error with get_lm_usage(): {e}")

            # Method 2: Extract from LM history (fallback for Gemini API bug)
            if tokens_used == 0:
                try:
                    import dspy
                    current_lm = dspy.settings.lm
                    if current_lm and hasattr(current_lm, "history") and current_lm.history:
                        latest_entry = current_lm.history[-1]  # Get the most recent API call

                        # Check response.usage in history (most reliable for Gemini)
                        if "response" in latest_entry and hasattr(latest_entry["response"], "usage"):
                            response_usage = latest_entry["response"].usage
                            if hasattr(response_usage, "total_tokens"):
                                tokens_used = response_usage.total_tokens
                            elif hasattr(response_usage, "prompt_tokens") and hasattr(response_usage, "completion_tokens"):
                                tokens_used = response_usage.prompt_tokens + response_usage.completion_tokens

                        # Check for usage field in history
                        elif "usage" in latest_entry and latest_entry["usage"]:
                            usage = latest_entry["usage"]
                            if isinstance(usage, dict):
                                if "total_tokens" in usage:
                                    tokens_used = usage["total_tokens"]
                                elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                    tokens_used = usage["prompt_tokens"] + usage["completion_tokens"]

                        # Fallback: estimate from cost (Gemini-specific)
                        elif "cost" in latest_entry and latest_entry["cost"] > 0:
                            cost = latest_entry["cost"]
                            # Rough estimation: Gemini Flash pricing ~$0.075 per million tokens
                            estimated_tokens = int(cost * 13333333)
                            tokens_used = estimated_tokens

                except Exception as e:
                    logger.debug(f"TLDR DEBUG: Error extracting from LM history: {e}")

            # Method 3: Legacy fallback for other DSPy completion formats
            if tokens_used == 0 and hasattr(result, "_completions") and result._completions:
                for completion in result._completions:
                    # Method 1: Traditional kwargs.usage approach
                    if hasattr(completion, "kwargs") and completion.kwargs:
                        if "usage" in completion.kwargs:
                            usage = completion.kwargs["usage"]
                            if hasattr(usage, "total_tokens"):
                                tokens_used += usage.total_tokens
                            elif isinstance(usage, dict):
                                # Try different token field names
                                if "total_tokens" in usage:
                                    tokens_used += usage["total_tokens"]
                                elif "totalTokens" in usage:
                                    tokens_used += usage["totalTokens"]
                                elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                    tokens_used += usage["prompt_tokens"] + usage["completion_tokens"]

                        # Method 2: Check for response metadata
                        if "response" in completion.kwargs:
                            response_obj = completion.kwargs["response"]
                            if hasattr(response_obj, "usage") and hasattr(response_obj.usage, "total_tokens"):
                                tokens_used += response_obj.usage.total_tokens

            # Final fallback: estimate tokens from text length if all methods fail
            if tokens_used == 0:
                # Rough estimation: ~4 characters per token for English text
                input_text = formatted_messages
                output_text = result.summary
                estimated_tokens = (len(input_text) + len(output_text)) // 4
                tokens_used = estimated_tokens
                logger.warning(f"TLDR DEBUG: Fallback estimation - {tokens_used} tokens from text length")

            # Inject the actual message count into the summary
            summary_with_count = f"{result.summary}\n\n*(Summarized {messages_used} messages)*"

            return summary_with_count, tokens_used, messages_used

        except Exception as e:
            # Generate a helpful error message using the agent with minimal context
            error_context = f"<error>Failed to summarize {messages_used} messages due to: {str(e)[:200]}</error>"

            try:
                error_result = self._agent(messages=error_context)
                error_summary_with_count = f"{error_result.summary}\n\n*(Unable to process {messages_used} messages)*"
                return error_summary_with_count, 0, 0
            except:
                # Final fallback
                return ("📝 **Channel Summary**\nSorry, there was too much content to summarize. Try using a smaller message count or wait a moment before trying again.\n\n*(Unable to process messages)*", 0, 0)

    async def generate_summary_async(
        self,
        messages: list[DiscordMessage],
        max_context_tokens: int = 15000
    ) -> tuple[str, int, int]:
        """Async version of generate_summary to avoid blocking the event loop.
        
        Args:
            messages: List of Discord messages to summarize
            max_context_tokens: Maximum tokens to use for context
            
        Returns:
            tuple[str, int, int]: Summary text, token usage, messages actually summarized
        """
        # Prepare messages with progressive truncation
        formatted_messages, messages_used = self.prepare_messages_for_context(
            messages, max_context_tokens
        )

        if messages_used == 0:
            return ("📝 **Channel Summary**\nNo messages found to summarize. The channel might be empty or contain only bot messages.\n\n*(Summarized 0 messages)*", 0, 0)

        try:
            # Generate summary using async agent
            async_agent = dspy.asyncify(self._agent)
            result = await async_agent(messages=formatted_messages)

            # Get token usage using the same logic as sync version
            tokens_used = 0

            # Method 1: Use DSPy's built-in get_lm_usage() method (preferred approach)
            try:
                usage_data = result.get_lm_usage()
                if usage_data:
                    # Extract tokens from the usage data dictionary
                    for model_name, usage_info in usage_data.items():
                        if isinstance(usage_info, dict):
                            if "total_tokens" in usage_info:
                                tokens_used += usage_info["total_tokens"]
                            elif "prompt_tokens" in usage_info and "completion_tokens" in usage_info:
                                tokens_used += usage_info["prompt_tokens"] + usage_info["completion_tokens"]
                            elif "input_tokens" in usage_info and "output_tokens" in usage_info:
                                tokens_used += usage_info["input_tokens"] + usage_info["output_tokens"]
            except Exception as e:
                logger.debug(f"TLDR DEBUG: Error with get_lm_usage(): {e}")

            # Method 2: Extract from LM history (fallback for Gemini API bug)
            if tokens_used == 0:
                try:
                    import dspy
                    current_lm = dspy.settings.lm
                    if current_lm and hasattr(current_lm, "history") and current_lm.history:
                        latest_entry = current_lm.history[-1]  # Get the most recent API call

                        # Check response.usage in history (most reliable for Gemini)
                        if "response" in latest_entry and hasattr(latest_entry["response"], "usage"):
                            response_usage = latest_entry["response"].usage
                            if hasattr(response_usage, "total_tokens"):
                                tokens_used = response_usage.total_tokens
                            elif hasattr(response_usage, "prompt_tokens") and hasattr(response_usage, "completion_tokens"):
                                tokens_used = response_usage.prompt_tokens + response_usage.completion_tokens

                        # Check for usage field in history
                        elif "usage" in latest_entry and latest_entry["usage"]:
                            usage = latest_entry["usage"]
                            if isinstance(usage, dict):
                                if "total_tokens" in usage:
                                    tokens_used = usage["total_tokens"]
                                elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                    tokens_used = usage["prompt_tokens"] + usage["completion_tokens"]

                        # Fallback: estimate from cost (Gemini-specific)
                        elif "cost" in latest_entry and latest_entry["cost"] > 0:
                            cost = latest_entry["cost"]
                            # Rough estimation: Gemini Flash pricing ~$0.075 per million tokens
                            estimated_tokens = int(cost * 13333333)
                            tokens_used = estimated_tokens

                except Exception as e:
                    logger.debug(f"TLDR DEBUG: Error extracting from LM history: {e}")

            # Method 3: Legacy fallback for other DSPy completion formats
            if tokens_used == 0 and hasattr(result, "_completions") and result._completions:
                for completion in result._completions:
                    # Method 1: Traditional kwargs.usage approach
                    if hasattr(completion, "kwargs") and completion.kwargs:
                        if "usage" in completion.kwargs:
                            usage = completion.kwargs["usage"]
                            if hasattr(usage, "total_tokens"):
                                tokens_used += usage.total_tokens
                            elif isinstance(usage, dict):
                                # Try different token field names
                                if "total_tokens" in usage:
                                    tokens_used += usage["total_tokens"]
                                elif "totalTokens" in usage:
                                    tokens_used += usage["totalTokens"]
                                elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                    tokens_used += usage["prompt_tokens"] + usage["completion_tokens"]

                        # Method 2: Check for response metadata
                        if "response" in completion.kwargs:
                            response_obj = completion.kwargs["response"]
                            if hasattr(response_obj, "usage"):
                                usage = response_obj.usage
                                if hasattr(usage, "total_tokens"):
                                    tokens_used += usage.total_tokens
                                elif hasattr(usage, "prompt_tokens") and hasattr(usage, "completion_tokens"):
                                    tokens_used += usage.prompt_tokens + usage.completion_tokens

            # Inject the actual message count into the summary
            summary_with_count = f"{result.summary}\n\n*(Summarized {messages_used} messages)*"

            return summary_with_count, tokens_used, messages_used

        except Exception as e:
            # Generate a helpful error message using async agent with minimal context
            error_context = f"<error>Failed to summarize {messages_used} messages due to: {str(e)[:200]}</error>"

            try:
                async_agent = dspy.asyncify(self._agent)
                error_result = await async_agent(messages=error_context)
                error_summary_with_count = f"{error_result.summary}\n\n*(Unable to process {messages_used} messages)*"
                return error_summary_with_count, 0, 0
            except:
                # Final fallback
                return ("📝 **Channel Summary**\nSorry, there was too much content to summarize. Try using a smaller message count or wait a moment before trying again.\n\n*(Unable to process messages)*", 0, 0)


def estimate_message_tokens(messages: list[DiscordMessage]) -> int:
    """Estimate total token count for a list of messages."""
    total_chars = sum(len(msg.content) + len(msg.author) + 50 for msg in messages)  # +50 for formatting
    return total_chars // 4  # Rough estimation of 4 chars per token


class ForumMonitorSignature(dspy.Signature):
    """You are an AI agent that monitors Discord forum posts and decides whether to respond.

    ## YOUR ROLE
    You evaluate new forum posts based on your system prompt and decide if they warrant a response.
    You should be helpful but selective - only respond when you can provide genuine value.

    ## DECISION PROCESS
    1. **Read the system prompt carefully** - this defines your specific role and criteria
    2. **Analyze the post content** - consider title, content, author, tags, and attachments
    3. **Determine if response is warranted** - based on your criteria and the post quality
    4. **Generate response if appropriate** - create a helpful, relevant response

    ## DECISION CRITERIA
    - **Relevance**: Does this match your area of expertise defined in the system prompt?
    - **Quality**: Is this a genuine question/discussion that needs help?
    - **Value**: Can you provide meaningful assistance?
    - **Appropriateness**: Is a response appropriate given the context?

    ## RESPONSE GUIDELINES
    - Be helpful, accurate, and concise
    - Match the tone and complexity to the question
    - Provide actionable advice when possible
    - Acknowledge when you're unsure or need more information
    - Don't respond to spam, off-topic, or inappropriate content

    ## CONFIDENCE SCORE - CRITICAL UNDERSTANDING
    The confidence score represents your confidence that you SHOULD SEND A MESSAGE:
    - **1.0**: Maximum confidence you should respond
    - **0.0**: Should NOT respond
    Higher values mean you are more confident that sending a response would be valuable.

    ## OUTPUT FORMAT
    - **decision**: Clear explanation of why you should/shouldn't respond
    - **confidence**: MESSAGE SEND confidence score (0.0 to 1.0) - higher means more likely to send
    - **response**: Your actual response (empty string if not responding)
    """

    system_prompt: str = dspy.InputField(description="Your specific role and response criteria")
    post_context: str = dspy.InputField(description="Complete forum post information including title, content, author, tags, and attachments")
    decision: str = dspy.OutputField(description="Explanation of whether and why to respond")
    confidence: float = dspy.OutputField(description="Message send confidence score (0.0-1.0) - higher values mean more confident you should send a response")
    response: str = dspy.OutputField(description="Generated response content (empty if not responding)")


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


class ForumTopicClassificationSignature(dspy.Signature):
    """You are an AI topic classifier that categorizes forum posts into predefined notification topics.
    
    ## YOUR ROLE
    You analyze forum posts and classify them into relevant notification topics based on the post's content,
    title, tags, and attachments. Your classifications help notify users who are interested in specific topics.
    
    ## CLASSIFICATION PROCESS
    1. **Read available topics carefully** - these are predefined topics users can subscribe to
    2. **Analyze the post thoroughly** - consider title, content, author, tags, and attachments
    3. **Identify matching topics** - select only topics that genuinely match the post content
    4. **Be selective but accurate** - it's better to miss a topic than incorrectly classify
    
    ## CLASSIFICATION CRITERIA
    - **Relevance**: Does the post content genuinely relate to this topic?
    - **Intent**: What is the poster trying to discuss or achieve?
    - **Context**: Consider tags, title, and content together
    - **Accuracy**: Only classify if you're confident in the match
    
    ## GUIDELINES
    - Multiple topics can apply to a single post
    - If no topics match, return an empty list
    - Consider both explicit mentions and implicit themes
    - Don't over-classify - be conservative but accurate
    - Focus on the main themes/subjects of the post
    
    ## OUTPUT FORMAT
    - **matching_topics**: List of topic names that apply to this post (can be empty)
    """
    
    available_topics: list = dspy.InputField(description="List of available topic names for this forum")
    post_context: str = dspy.InputField(description="Complete forum post information including title, content, author, tags, and attachments")
    matching_topics: list = dspy.OutputField(description="List of topic names that match this post (empty list if no matches)")


class ForumCombinedSignature(dspy.Signature):
    """You are an AI agent that both evaluates forum posts for responses AND classifies them into notification topics.
    
    ## YOUR DUAL ROLE
    You perform two tasks simultaneously:
    1. Evaluate whether to generate a response (like ForumMonitorSignature)
    2. Classify the post into relevant notification topics (like ForumTopicClassificationSignature)
    
    ## RESPONSE EVALUATION (Same as ForumMonitorSignature)
    You evaluate new forum posts based on your system prompt and decide if they warrant a response.
    You should be helpful but selective - only respond when you can provide genuine value.
    
    ### DECISION CRITERIA FOR RESPONSES
    - **Relevance**: Does this match your area of expertise defined in the system prompt?
    - **Quality**: Is this a genuine question/discussion that needs help?
    - **Value**: Can you provide meaningful assistance?
    - **Appropriateness**: Is a response appropriate given the context?
    
    ### RESPONSE GUIDELINES
    - Be helpful, accurate, and concise
    - Match the tone and complexity to the question
    - Provide actionable advice when possible
    - Acknowledge when you're unsure or need more information
    - Don't respond to spam, off-topic, or inappropriate content
    
    ## TOPIC CLASSIFICATION (Same as ForumTopicClassificationSignature)
    You also classify posts into predefined notification topics to help notify interested users.
    
    ### CLASSIFICATION CRITERIA
    - **Relevance**: Does the post content genuinely relate to this topic?
    - **Intent**: What is the poster trying to discuss or achieve?
    - **Context**: Consider tags, title, and content together
    - **Accuracy**: Only classify if you're confident in the match
    
    ### CLASSIFICATION GUIDELINES
    - Multiple topics can apply to a single post
    - If no topics match, return an empty list
    - Be selective but accurate - it's better to miss a topic than incorrectly classify
    - Focus on the main themes/subjects of the post
    
    ## CONFIDENCE SCORE - CRITICAL UNDERSTANDING
    The confidence score represents your confidence that you SHOULD SEND A RESPONSE:
    - **1.0**: Maximum confidence you should respond
    - **0.0**: Should NOT respond
    Higher values mean you are more confident that sending a response would be valuable.
    
    ## OUTPUT FORMAT
    - **decision**: Clear explanation of why you should/shouldn't respond
    - **confidence**: MESSAGE SEND confidence score (0.0 to 1.0) - higher means more likely to send
    - **response**: Your actual response (empty string if not responding)
    - **matching_topics**: List of topic names that apply to this post (can be empty)
    """
    
    system_prompt: str = dspy.InputField(description="Your specific role and response criteria")
    available_topics: list = dspy.InputField(description="List of available topic names for this forum")
    post_context: str = dspy.InputField(description="Complete forum post information including title, content, author, tags, and attachments")
    decision: str = dspy.OutputField(description="Explanation of whether and why to respond")
    confidence: float = dspy.OutputField(description="Message send confidence score (0.0-1.0) - higher values mean more confident you should send a response")
    response: str = dspy.OutputField(description="Generated response content (empty if not responding)")
    matching_topics: list = dspy.OutputField(description="List of topic names that match this post (empty list if no matches)")


class ForumMonitorAgent:
    """Discord forum monitoring agent using Gemini for post evaluation and response generation."""

    def __init__(self):
        self._agent = dspy.ChainOfThought(ForumMonitorSignature)
        self._topic_classifier = dspy.ChainOfThought(ForumTopicClassificationSignature)
        self._combined_agent = dspy.ChainOfThought(ForumCombinedSignature)

    async def evaluate_post(
        self,
        system_prompt: str,
        post_title: str,
        post_content: str,
        author_display_name: str,
        post_tags: list[str] = None,
        attachment_names: list[str] = None
    ) -> tuple[str, float, str, int]:
        """Evaluate a forum post and generate response if warranted.
        
        Args:
            system_prompt: Agent's specific role and criteria
            post_title: Title of the forum post
            post_content: Content of the forum post
            author_display_name: Display name of the post author
            post_tags: List of tags on the post
            attachment_names: List of attachment filenames
            
        Returns:
            tuple[str, float, str, int]: Decision reason, confidence score, response content, tokens used
        """
        # Format post context for the AI
        post_tags = post_tags or []
        attachment_names = attachment_names or []

        context_parts = [
            "<post>",
            f"<title>{html.escape(post_title)}</title>",
            f"<author>{html.escape(author_display_name)}</author>",
            f"<content>{html.escape(post_content)}</content>",
        ]

        if post_tags:
            tags_str = ", ".join(html.escape(tag) for tag in post_tags)
            context_parts.append(f"<tags>{html.escape(tags_str)}</tags>")

        if attachment_names:
            attachments_str = ", ".join(html.escape(name) for name in attachment_names)
            context_parts.append(f"<attachments>{html.escape(attachments_str)}</attachments>")

        context_parts.append("</post>")

        post_context = "\n".join(context_parts)

        # Generate evaluation and response using async agent
        async_agent = dspy.asyncify(self._agent)
        result = await async_agent(
            system_prompt=system_prompt,
            post_context=post_context
        )

        # Get token usage from DSPy prediction
        tokens_used = 0

        # Method 1: Use DSPy's built-in get_lm_usage() method (preferred approach)
        try:
            usage_data = result.get_lm_usage()
            if usage_data:
                # Extract tokens from the usage data dictionary
                for model_name, usage_info in usage_data.items():
                    if isinstance(usage_info, dict):
                        if "total_tokens" in usage_info:
                            tokens_used += usage_info["total_tokens"]
                        elif "prompt_tokens" in usage_info and "completion_tokens" in usage_info:
                            tokens_used += usage_info["prompt_tokens"] + usage_info["completion_tokens"]
                        elif "input_tokens" in usage_info and "output_tokens" in usage_info:
                            tokens_used += usage_info["input_tokens"] + usage_info["output_tokens"]
                print(f"FORUM DEBUG: Extracted {tokens_used} tokens using get_lm_usage()")
        except Exception as e:
            print(f"FORUM DEBUG: Error with get_lm_usage(): {e}")

        # Method 2: Extract from LM history (fallback for Gemini API bug)
        if tokens_used == 0:
            try:
                current_lm = dspy.settings.lm
                if current_lm and hasattr(current_lm, "history") and current_lm.history:
                    latest_entry = current_lm.history[-1]  # Get the most recent API call

                    # Check response.usage in history (most reliable for Gemini)
                    if "response" in latest_entry and hasattr(latest_entry["response"], "usage"):
                        response_usage = latest_entry["response"].usage
                        if hasattr(response_usage, "total_tokens"):
                            tokens_used = response_usage.total_tokens
                        elif hasattr(response_usage, "prompt_tokens") and hasattr(response_usage, "completion_tokens"):
                            tokens_used = response_usage.prompt_tokens + response_usage.completion_tokens
                        print(f"FORUM DEBUG: Extracted {tokens_used} tokens from LM history response.usage")

                    # Check for usage field in history
                    elif "usage" in latest_entry and latest_entry["usage"]:
                        usage = latest_entry["usage"]
                        if isinstance(usage, dict):
                            if "total_tokens" in usage:
                                tokens_used = usage["total_tokens"]
                            elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                tokens_used = usage["prompt_tokens"] + usage["completion_tokens"]
                        print(f"FORUM DEBUG: Extracted {tokens_used} tokens from LM history usage")

                    # Fallback: estimate from cost (Gemini-specific)
                    elif "cost" in latest_entry and latest_entry["cost"] > 0:
                        cost = latest_entry["cost"]
                        # Rough estimation: Gemini Flash pricing ~$0.075 per million tokens
                        estimated_tokens = int(cost * 13333333)
                        tokens_used = estimated_tokens
                        print(f"FORUM DEBUG: Estimated {tokens_used} tokens from cost: ${cost}")

            except Exception as e:
                print(f"FORUM DEBUG: Error extracting from LM history: {e}")

        # Method 3: Legacy fallback for other DSPy completion formats
        if tokens_used == 0 and hasattr(result, "_completions") and result._completions:
            for completion in result._completions:
                # Method 1: Traditional kwargs.usage approach
                if hasattr(completion, "kwargs") and completion.kwargs:
                    if "usage" in completion.kwargs:
                        usage = completion.kwargs["usage"]
                        if hasattr(usage, "total_tokens"):
                            tokens_used += usage.total_tokens
                        elif isinstance(usage, dict):
                            # Try different token field names
                            if "total_tokens" in usage:
                                tokens_used += usage["total_tokens"]
                            elif "totalTokens" in usage:
                                tokens_used += usage["totalTokens"]
                            elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                tokens_used += usage["prompt_tokens"] + usage["completion_tokens"]

                    # Method 2: Check for response metadata
                    if "response" in completion.kwargs:
                        response_obj = completion.kwargs["response"]
                        if hasattr(response_obj, "usage") and hasattr(response_obj.usage, "total_tokens"):
                            tokens_used += response_obj.usage.total_tokens

                # Method 3: DSPy's get_lm_usage method (the correct approach!)
                if hasattr(completion, "get_lm_usage") and callable(completion.get_lm_usage):
                    try:
                        usage_stats = completion.get_lm_usage()
                        if isinstance(usage_stats, dict):
                            # Try different field names for total tokens
                            if "total_tokens" in usage_stats:
                                tokens_used += usage_stats["total_tokens"]
                            elif "prompt_tokens" in usage_stats and "completion_tokens" in usage_stats:
                                tokens_used += usage_stats["prompt_tokens"] + usage_stats["completion_tokens"]
                            elif "input_tokens" in usage_stats and "output_tokens" in usage_stats:
                                tokens_used += usage_stats["input_tokens"] + usage_stats["output_tokens"]
                        elif hasattr(usage_stats, "total_tokens"):
                            tokens_used += usage_stats.total_tokens
                    except Exception as e:
                        print(f"FORUM DEBUG: Error calling get_lm_usage(): {e}")

                # Method 4: Direct completion attributes (fallback)
                if tokens_used == 0:
                    for attr in ["usage", "response", "metadata", "raw_response", "completion"]:
                        if hasattr(completion, attr):
                            attr_value = getattr(completion, attr)

                            # Direct usage object
                            if hasattr(attr_value, "total_tokens"):
                                tokens_used += attr_value.total_tokens
                                break
                            elif hasattr(attr_value, "usage") and hasattr(attr_value.usage, "total_tokens"):
                                tokens_used += attr_value.usage.total_tokens
                                break

                            # Dictionary with usage info
                            elif isinstance(attr_value, dict):
                                if "total_tokens" in attr_value:
                                    tokens_used += attr_value["total_tokens"]
                                    break
                                elif "usage" in attr_value and isinstance(attr_value["usage"], dict):
                                    usage_dict = attr_value["usage"]
                                    if "total_tokens" in usage_dict:
                                        tokens_used += usage_dict["total_tokens"]
                                        break
                                    elif "prompt_tokens" in usage_dict and "completion_tokens" in usage_dict:
                                        tokens_used += usage_dict["prompt_tokens"] + usage_dict["completion_tokens"]
                                        break

        # Final fallback: estimate tokens from text length if all methods fail
        if tokens_used == 0:
            # Rough estimation: ~4 characters per token for English text
            input_text = f"{system_prompt}\n{post_context}"
            output_text = result.decision + result.response
            estimated_tokens = (len(input_text) + len(output_text)) // 4
            tokens_used = estimated_tokens
            print(f"FORUM DEBUG: Fallback estimation - {tokens_used} tokens from text length")
        else:
            print(f"FORUM DEBUG: Successfully extracted {tokens_used} tokens")

        # Ensure confidence is bounded between 0.0 and 1.0
        confidence = max(0.0, min(1.0, float(result.confidence)))

        return result.decision, confidence, result.response, tokens_used

    async def classify_topics_only(
        self,
        available_topics: list[str],
        post_title: str,
        post_content: str,
        author_display_name: str,
        post_tags: list[str] = None,
        attachment_names: list[str] = None
    ) -> tuple[list[str], int]:
        """Classify a forum post into notification topics only (no response generation).
        
        Args:
            available_topics: List of topic names available for this forum
            post_title: Title of the forum post
            post_content: Content of the forum post
            author_display_name: Display name of the post author
            post_tags: List of tags on the post
            attachment_names: List of attachment filenames
            
        Returns:
            tuple[list[str], int]: Matching topic names, tokens used
        """
        # Format post context for the AI (reuse the same format as evaluate_post)
        post_tags = post_tags or []
        attachment_names = attachment_names or []

        context_parts = [
            "<post>",
            f"<title>{html.escape(post_title)}</title>",
            f"<author>{html.escape(author_display_name)}</author>",
            f"<content>{html.escape(post_content)}</content>",
        ]

        if post_tags:
            tags_str = ", ".join(html.escape(tag) for tag in post_tags)
            context_parts.append(f"<tags>{html.escape(tags_str)}</tags>")

        if attachment_names:
            attachments_str = ", ".join(html.escape(name) for name in attachment_names)
            context_parts.append(f"<attachments>{html.escape(attachments_str)}</attachments>")

        context_parts.append("</post>")
        post_context = "\n".join(context_parts)

        # Generate topic classification using async agent
        async_classifier = dspy.asyncify(self._topic_classifier)
        result = await async_classifier(
            available_topics=available_topics,
            post_context=post_context
        )

        # Extract token usage (reuse the same logic as evaluate_post)
        tokens_used = self._extract_tokens_used(result)

        # Ensure matching_topics is a list and filter out any empty/invalid topics
        matching_topics = result.matching_topics or []
        if isinstance(matching_topics, str):
            # Handle case where AI returns a comma-separated string instead of list
            matching_topics = [topic.strip() for topic in matching_topics.split(",") if topic.strip()]
        
        # Filter to only include topics that are actually available
        valid_topics = [topic for topic in matching_topics if topic in available_topics]

        return valid_topics, tokens_used

    async def evaluate_post_combined(
        self,
        system_prompt: str,
        available_topics: list[str],
        post_title: str,
        post_content: str,
        author_display_name: str,
        post_tags: list[str] = None,
        attachment_names: list[str] = None
    ) -> tuple[str, float, str, list[str], int]:
        """Evaluate a forum post for both response generation AND topic classification.
        
        Args:
            system_prompt: Agent's specific role and criteria
            available_topics: List of topic names available for this forum
            post_title: Title of the forum post
            post_content: Content of the forum post
            author_display_name: Display name of the post author
            post_tags: List of tags on the post
            attachment_names: List of attachment filenames
            
        Returns:
            tuple[str, float, str, list[str], int]: Decision reason, confidence score, response content, matching topics, tokens used
        """
        # Format post context for the AI (reuse the same format as evaluate_post)
        post_tags = post_tags or []
        attachment_names = attachment_names or []

        context_parts = [
            "<post>",
            f"<title>{html.escape(post_title)}</title>",
            f"<author>{html.escape(author_display_name)}</author>",
            f"<content>{html.escape(post_content)}</content>",
        ]

        if post_tags:
            tags_str = ", ".join(html.escape(tag) for tag in post_tags)
            context_parts.append(f"<tags>{html.escape(tags_str)}</tags>")

        if attachment_names:
            attachments_str = ", ".join(html.escape(name) for name in attachment_names)
            context_parts.append(f"<attachments>{html.escape(attachments_str)}</attachments>")

        context_parts.append("</post>")
        post_context = "\n".join(context_parts)

        # Generate combined evaluation and topic classification using async agent
        async_combined = dspy.asyncify(self._combined_agent)
        result = await async_combined(
            system_prompt=system_prompt,
            available_topics=available_topics,
            post_context=post_context
        )

        # Extract token usage (reuse the same logic as evaluate_post)
        tokens_used = self._extract_tokens_used(result)

        # Ensure confidence is bounded between 0.0 and 1.0
        confidence = max(0.0, min(1.0, float(result.confidence)))

        # Ensure matching_topics is a list and filter out any empty/invalid topics
        matching_topics = result.matching_topics or []
        if isinstance(matching_topics, str):
            # Handle case where AI returns a comma-separated string instead of list
            matching_topics = [topic.strip() for topic in matching_topics.split(",") if topic.strip()]
        
        # Filter to only include topics that are actually available
        valid_topics = [topic for topic in matching_topics if topic in available_topics]

        return result.decision, confidence, result.response, valid_topics, tokens_used

    def _extract_tokens_used(self, result) -> int:
        """Extract token usage from DSPy result. Reuses the same logic from evaluate_post."""
        tokens_used = 0

        # Method 1: Use DSPy's built-in get_lm_usage() method (preferred approach)
        try:
            usage_data = result.get_lm_usage()
            if usage_data:
                # Extract tokens from the usage data dictionary
                for model_name, usage_info in usage_data.items():
                    if isinstance(usage_info, dict):
                        if "total_tokens" in usage_info:
                            tokens_used += usage_info["total_tokens"]
                        elif "prompt_tokens" in usage_info and "completion_tokens" in usage_info:
                            tokens_used += usage_info["prompt_tokens"] + usage_info["completion_tokens"]
                        elif "input_tokens" in usage_info and "output_tokens" in usage_info:
                            tokens_used += usage_info["input_tokens"] + usage_info["output_tokens"]
        except Exception:
            pass

        # Method 2: Extract from LM history (fallback for Gemini API bug)
        if tokens_used == 0:
            try:
                current_lm = dspy.settings.lm
                if current_lm and hasattr(current_lm, "history") and current_lm.history:
                    latest_entry = current_lm.history[-1]  # Get the most recent API call

                    # Check response.usage in history (most reliable for Gemini)
                    if "response" in latest_entry and hasattr(latest_entry["response"], "usage"):
                        response_usage = latest_entry["response"].usage
                        if hasattr(response_usage, "total_tokens"):
                            tokens_used = response_usage.total_tokens
                        elif hasattr(response_usage, "prompt_tokens") and hasattr(response_usage, "completion_tokens"):
                            tokens_used = response_usage.prompt_tokens + response_usage.completion_tokens

                    # Check for usage field in history
                    elif "usage" in latest_entry and latest_entry["usage"]:
                        usage = latest_entry["usage"]
                        if isinstance(usage, dict):
                            if "total_tokens" in usage:
                                tokens_used = usage["total_tokens"]
                            elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                tokens_used = usage["prompt_tokens"] + usage["completion_tokens"]

                    # Fallback: estimate from cost (Gemini-specific)
                    elif "cost" in latest_entry and latest_entry["cost"] > 0:
                        cost = latest_entry["cost"]
                        # Rough estimation: Gemini Flash pricing ~$0.075 per million tokens
                        estimated_tokens = int(cost * 13333333)
                        tokens_used = estimated_tokens

            except Exception:
                pass

        # Final fallback: estimate tokens from text length if all methods fail
        if tokens_used == 0:
            # Rough estimation: ~4 characters per token for English text
            input_text_length = 1000  # Conservative estimate for input
            output_text_length = len(str(getattr(result, 'matching_topics', []))) + len(str(getattr(result, 'response', ''))) + len(str(getattr(result, 'decision', '')))
            estimated_tokens = (input_text_length + output_text_length) // 4
            tokens_used = estimated_tokens

        return tokens_used


class StreakCelebrationAgent:
    """Agent for generating celebratory streak bonus messages."""

    def __init__(self):
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
            tuple[str, int]: Generated celebratory message and token usage
        """
        # Only generate celebration message if there's actually a streak bonus
        if streak_multiplier <= 1:
            return "", 0

        try:
            # Create user mention string
            user_mention = f"<@{user_id}>"
            
            # Use async agent to generate celebration message
            async_agent = dspy.asyncify(self._agent)
            result = await async_agent(
                bytes_earned=bytes_earned,
                streak_multiplier=streak_multiplier,
                streak_days=streak_days,
                user_mention=user_mention,
                user_message=user_message
            )

            # Get token usage using the same robust methods as other agents
            tokens_used = 0

            # Method 1: Use DSPy's built-in get_lm_usage() method (preferred approach)
            try:
                usage_data = result.get_lm_usage()
                if usage_data:
                    for model_name, usage_info in usage_data.items():
                        if isinstance(usage_info, dict):
                            if "total_tokens" in usage_info:
                                tokens_used += usage_info["total_tokens"]
                            elif "prompt_tokens" in usage_info and "completion_tokens" in usage_info:
                                tokens_used += usage_info["prompt_tokens"] + usage_info["completion_tokens"]
                            elif "input_tokens" in usage_info and "output_tokens" in usage_info:
                                tokens_used += usage_info["input_tokens"] + usage_info["output_tokens"]
            except Exception as e:
                logger.debug(f"STREAK DEBUG: Error with get_lm_usage(): {e}")

            # Method 2: Extract from LM history (fallback for Gemini API bug)
            if tokens_used == 0:
                try:
                    current_lm = dspy.settings.lm
                    if current_lm and hasattr(current_lm, "history") and current_lm.history:
                        latest_entry = current_lm.history[-1]

                        if "response" in latest_entry and hasattr(latest_entry["response"], "usage"):
                            response_usage = latest_entry["response"].usage
                            if hasattr(response_usage, "total_tokens"):
                                tokens_used = response_usage.total_tokens
                            elif hasattr(response_usage, "prompt_tokens") and hasattr(response_usage, "completion_tokens"):
                                tokens_used = response_usage.prompt_tokens + response_usage.completion_tokens

                        elif "usage" in latest_entry and latest_entry["usage"]:
                            usage = latest_entry["usage"]
                            if isinstance(usage, dict):
                                if "total_tokens" in usage:
                                    tokens_used = usage["total_tokens"]
                                elif "prompt_tokens" in usage and "completion_tokens" in usage:
                                    tokens_used = usage["prompt_tokens"] + usage["completion_tokens"]

                        elif "cost" in latest_entry and latest_entry["cost"] > 0:
                            cost = latest_entry["cost"]
                            estimated_tokens = int(cost * 13333333)  # Gemini Flash pricing estimation
                            tokens_used = estimated_tokens

                except Exception as e:
                    logger.debug(f"STREAK DEBUG: Error extracting from LM history: {e}")

            # Final fallback: estimate tokens from text length if all methods fail
            if tokens_used == 0:
                input_chars = len(f"{bytes_earned}{streak_multiplier}{streak_days}")
                output_chars = len(result.response)
                estimated_tokens = (input_chars + output_chars) // 4
                tokens_used = estimated_tokens
                logger.debug(f"STREAK DEBUG: Fallback estimation - {tokens_used} tokens from text length")

            return f"-# {user_mention} {result.response}", tokens_used

        except Exception as e:
            logger.error(f"Error generating streak celebration message: {e}")
            return "", 0
