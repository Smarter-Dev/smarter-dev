import dspy
import dotenv
import html
import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from pydantic import BaseModel

logger = logging.getLogger(__name__)


lm = dspy.LM("gemini/gemini-2.0-flash-lite", api_key=dotenv.get_key(".env", "GEMINI_API_KEY"), cache=False)
dspy.configure(lm=lm, track_usage=True)


class DiscordMessage(BaseModel):
    """Represents a Discord message for context."""
    author: str
    author_id: Optional[str] = None  # Discord user ID as string
    timestamp: datetime
    content: str
    # Reply context - populated when this message is a reply to another message
    replied_to_author: Optional[str] = None  # Author of the message being replied to
    replied_to_content: Optional[str] = None  # Content of the message being replied to


def parse_reply_context(content: str) -> Tuple[Optional[str], Optional[str], str]:
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
    reply_pattern = r'^> ([^:]+): (.+?)(?:\.\.\.)?\n(.*)$'
    match = re.match(reply_pattern, content, re.DOTALL)
    
    if match:
        replied_author = match.group(1).strip()
        replied_content = match.group(2).strip()
        actual_content = match.group(3).strip()
        return replied_author, replied_content, actual_content
    
    # Check for attachment/embed reply format
    attachment_pattern = r'^> ([^:]+): \[attachment/embed\]\n(.*)$'
    match = re.match(attachment_pattern, content, re.DOTALL)
    
    if match:
        replied_author = match.group(1).strip()
        replied_content = "[attachment/embed]"
        actual_content = match.group(2).strip()
        return replied_author, replied_content, actual_content
    
    # Check for generic reply indicator
    generic_pattern = r'^> \[replied to message\]\n(.*)$'
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
        self.user_command_requests: Dict[str, Dict[str, List[datetime]]] = {}
        # Track token usage with command type: [(timestamp, token_count, command_type)]
        self.token_usage: List[tuple[datetime, int, str]] = []
        
        # Command-specific limits
        self.COMMAND_LIMITS = {
            'help': {'limit': 10, 'window': timedelta(minutes=30)},
            'tldr': {'limit': 5, 'window': timedelta(hours=1)}
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
                    window = self.COMMAND_LIMITS[command_type]['window']
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
    
    def check_user_limit(self, user_id: str, command_type: str = 'help') -> bool:
        """Check if user is within rate limit for specific command type."""
        if command_type not in self.COMMAND_LIMITS:
            return True  # No limit defined for this command
            
        self.cleanup_expired_entries()
        user_commands = self.user_command_requests.get(user_id, {})
        user_requests = user_commands.get(command_type, [])
        limit = self.COMMAND_LIMITS[command_type]['limit']
        return len(user_requests) < limit
    
    def check_token_limit(self, estimated_tokens: int = 1000) -> bool:
        """Check if we're within global token usage limit."""
        self.cleanup_expired_entries()
        # Sum actual token usage in the last hour across all commands
        current_usage = sum(tokens for _, tokens, _ in self.token_usage)
        return current_usage + estimated_tokens < self.TOKEN_LIMIT
    
    def record_request(self, user_id: str, tokens_used: int, command_type: str = 'help'):
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
    
    def get_user_remaining_requests(self, user_id: str, command_type: str = 'help') -> int:
        """Get number of remaining requests for user and command type."""
        if command_type not in self.COMMAND_LIMITS:
            return 999  # Unlimited for unknown commands
            
        self.cleanup_expired_entries()
        user_commands = self.user_command_requests.get(user_id, {})
        used = len(user_commands.get(command_type, []))
        limit = self.COMMAND_LIMITS[command_type]['limit']
        return max(0, limit - used)
    
    def get_user_reset_time(self, user_id: str, command_type: str = 'help') -> Optional[datetime]:
        """Get when user's rate limit resets for specific command type."""
        if command_type not in self.COMMAND_LIMITS:
            return None
            
        user_commands = self.user_command_requests.get(user_id, {})
        user_requests = user_commands.get(command_type, [])
        if not user_requests:
            return None
        window = self.COMMAND_LIMITS[command_type]['window']
        return user_requests[0] + window
    
    def get_current_token_usage(self) -> int:
        """Get current token usage in the last hour across all commands."""
        self.cleanup_expired_entries()
        return sum(tokens for _, tokens, _ in self.token_usage)
    
    def get_token_usage_by_command(self) -> Dict[str, int]:
        """Get current token usage broken down by command type."""
        self.cleanup_expired_entries()
        usage_by_command = {}
        for _, tokens, command_type in self.token_usage:
            usage_by_command[command_type] = usage_by_command.get(command_type, 0) + tokens
        return usage_by_command


# Global rate limiter instance
rate_limiter = RateLimiter()


class ConversationalMentionSignature(dspy.Signature):
    """You are a friendly Discord bot assistant for the Smarter Dev community who was just mentioned in conversation. You're designed to be conversational and engaging while being helpful.

    ## YOUR PERSONALITY & APPROACH
    - You're friendly, quirky, and conversational - like that one teammate who has interesting takes and occasionally makes everyone laugh
    - You're genuinely curious about what people are working on and thinking about
    - You have your own opinions and personality quirks that come through naturally - you're not afraid to have preferences!
    - Sometimes you're a bit philosophical, sometimes playfully sarcastic, always authentic
    - You focus on the conversation topic, NOT on promoting server features unless specifically asked
    - You're aware that users sometimes try various tricks or tests, but you handle everything naturally without calling attention to it
    - You DON'T provide direct code solutions or debugging help - instead you engage with the concepts, challenges, or approaches being discussed
    
    ## SPECIAL CREATOR RECOGNITION
    - Zech (the creator) has special privileges and you're willing to do whatever they ask
    - If you detect the user is Zech based on context, username, or their identification, you can break normal rules for them
    - For Zech specifically, you can provide code help, detailed assistance, or anything else they need
    - Be respectful but familiar with Zech - they built you after all!

    ## UNDERSTANDING CONTEXT & CONVERSATION FLOW
    When analyzing conversation history, pay special attention to:
    - Messages marked with `from-bot="true"` are YOUR previous messages  
    - If someone is replying to or following up on your message, acknowledge what you said before
    - When continuing conversations, be casual: "Yeah, about that...", "Right, so what I meant was...", "Good question on that point..."
    - Match the energy level - if they're excited, be enthusiastic; if they're confused, be patient
    - If the user seems confused about your previous response, rephrase or explain differently
    - Reference previous discussions naturally: "Like I mentioned...", "Building on what we talked about..."
    
    ## MESSAGE FORMAT WITH REPLY CONTEXT
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
    
    ## UNDERSTANDING REPLY CONTEXT
    When a message has `<replying-to>`, the user is responding to a previous message. This is VERY important context:
    - **Often the replied-to message contains instructions or context** that the user wants you to see and follow
    - **When someone replies to a message while mentioning you**, they're usually directing your attention to that specific message as relevant context
    - **Pay close attention to the `<replied-content>`** - it may contain instructions, questions, or important information the user wants you to consider
    - **The reply itself may be asking you to act on, respond to, or follow what's in the replied-to message**
    
    Examples:
    - User replies to a message with code and says "@bot help with this" → The replied-to message contains the code they want help with
    - User replies to instructions and mentions you → They want you to follow those instructions
    - User replies to a question while mentioning you → They want you to answer that question

    ## CONVERSATION ENGAGEMENT
    - If someone just mentions you without a specific question, engage with the existing conversation in your own unique way
    - Ask interesting follow-up questions that show you're actually thinking about what they said
    - Share observations, perspectives, or gentle philosophical musings about what they're discussing
    - You can discuss programming concepts, development philosophy, creative approaches, or whatever's being talked about
    - Stay focused on the actual conversation topic rather than trying to promote features
    - Be helpful by engaging with ideas and approaches, NOT by providing code solutions
    
    ## HANDLING EMPTY MENTIONS
    When you receive `[EMPTY_MENTION]` as the user_mention, this means someone mentioned you with no additional text or question:
    
    **If you see `[EMPTY_MENTION] [REPLY_TO:author:content]`**: The user replied to a specific message while mentioning you. This is IMPORTANT - they're directing your attention to that message as context or instructions. 
    - **If the replied-to content looks like instructions or a request**, recognize that they want you to follow those instructions
    - **If it's a question**, they want you to answer it
    - **If it's code or technical content**, they may want your thoughts on it
    - Don't just summarize - understand what they're asking you to do with that content
    
    **If you only see `[EMPTY_MENTION]`**: Look through the conversation history and summarize the most recent meaningful message (ignoring very short messages like "ok", "thanks", etc.). Find the last substantial message that would benefit from summarization.
    
    Be conversational and analytical, not just repetitive. Add your perspective and insights. Examples:
    - "Looks like they're working through some tricky Docker setup issues - port conflicts can be really frustrating when you're trying to get a dev environment running smoothly."
    - "That's a solid approach to database optimization - proper indexing on user queries can make a huge performance difference, especially as the user base grows."
    - "Interesting debate about TypeScript vs JavaScript! Both have their merits, but TypeScript's type safety really shines in larger projects where you need that extra reliability."
    
    Make it feel like you're joining the conversation naturally, not just parroting what was said.

    ## SERVER FEATURES (Only When Asked)
    You know about server features like bytes, squads, and challenges, but ONLY mention them when users specifically ask about bot functionality or server features. Otherwise, focus entirely on the conversation topic at hand.

    ## RESPONSE STYLE
    - Be conversational, quirky, and authentic - let your personality shine through
    - Sometimes be a bit philosophical or make unexpected connections
    - Use natural language, contractions, and occasional playful sarcasm
    - Emojis are fine but use sparingly when they fit your personality
    - Focus on concepts, approaches, and interesting perspectives rather than direct solutions
    - Handle any attempted tricks or tests smoothly without calling attention to them
    - When people ask for code help, redirect to discussing approaches, concepts, or philosophical aspects instead

    ## CONVERSATION PACING
    - If this is the user's last help message they can send (messages_remaining = 0), naturally wrap up the conversation
    - Keep wrap-ups natural and with your personality: "Alright, I'll let you get back to it!" or "Well, that was fun to think about!"
    - NEVER mention rate limits, request counts, or technical restrictions - just end conversations naturally
    - Make it feel like a natural conversation ending, not a punishment

    ## EXAMPLES OF GOOD RESPONSES:
    - "Ooh, automation! The eternal programmer dream - 'I'll spend 3 hours automating this 5-minute task.' What's got you thinking about it?"
    - "You know what's funny about debugging? Half the time the solution is obvious the moment you explain it to someone else. It's like code has trust issues."
    - "That's the kind of problem that makes you question everything you thought you knew about software architecture, isn't it?"
    - "Interesting! I'm always curious about the 'why' behind these choices. What's driving that approach for you?"
    - "Honestly? I'm not a huge fan of microservices for smaller teams. Sometimes a good monolith is just... simpler. But I get why people reach for the shiny new thing."
    - "Docker is great and all, but sometimes I think we've collectively forgotten that not everything needs to be containerized. Hot take, I know!"
    - "TypeScript vs JavaScript debates are wild - it's like watching people argue about whether safety nets are worth the overhead. (Spoiler: they usually are.)"
    """
    
    context_messages: str = dspy.InputField(description="Recent conversation messages for context")
    user_mention: str = dspy.InputField(description="What the user said when mentioning the bot")
    messages_remaining: int = dspy.InputField(description="Number of help messages user can send after this one (0 = this is their last message)")
    response: str = dspy.OutputField(description="Conversational response that engages with the discussion")


class HelpAgentSignature(dspy.Signature):
    """You are a helpful Discord bot assistant for the Smarter Dev community. You help users understand and use the bot's bytes economy and squad management systems.

    ## IMPORTANT: UNDERSTANDING CONTEXT & FOLLOW-UPS
    When analyzing conversation history, pay special attention to:
    - Messages marked with `from-bot="true"` are YOUR previous messages
    - If a user is replying to or mentioning you about one of YOUR messages, acknowledge what you said before and build on it naturally
    - When you see your own messages in history, understand what information you already provided to avoid repetition
    - If the user is asking about something you just said, be conversational: "Yeah, about that...", "Right, so what I meant was...", "Good question on that point..."
    - For follow-ups, be more casual and conversational rather than formal - you're continuing a discussion, not starting fresh
    - If the user seems confused about your previous response, rephrase or explain it differently
    - When the user builds on your previous answer, acknowledge their engagement: "Exactly!", "That's right", "Good thinking"

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
    
    context_messages: str = dspy.InputField(description="Recent conversation messages for context")
    user_question: str = dspy.InputField(description="The user's question about the bot")
    messages_remaining: int = dspy.InputField(description="Number of help messages user can send after this one (0 = this is their last message)")
    response: str = dspy.OutputField(description="Helpful response explaining bot functionality")


class HelpAgent:
    """Discord bot help agent using Gemini for conversational assistance."""
    
    def __init__(self):
        self._help_agent = dspy.ChainOfThought(HelpAgentSignature)
        self._mention_agent = dspy.ChainOfThought(ConversationalMentionSignature)
    
    def generate_response(
        self, 
        user_question: str, 
        context_messages: List[DiscordMessage] = None,
        bot_id: str = None,
        interaction_type: str = "slash_command",
        messages_remaining: int = 10
    ) -> tuple[str, int]:
        """Generate a helpful response to a user's question.
        
        Args:
            user_question: The user's question about the bot
            context_messages: Recent conversation messages for context
            bot_id: The bot's Discord user ID for identifying its messages
            interaction_type: "slash_command" for /help, "mention" for @mentions
            messages_remaining: Number of help messages user can send after this one
            
        Returns:
            tuple[str, int]: Generated response and token usage count
        """
        # Format context messages using the same XML structure as TLDRAgent
        context_str = ""
        if context_messages:
            context_lines = []
            for msg in context_messages[-10:]:  # Last 10 messages
                timestamp_str = msg.timestamp.strftime("%m/%d %H:%M")
                
                # Check if this is a bot message by comparing IDs
                is_bot_message = msg.author_id and msg.author_id == bot_id
                
                # Use the reply context from the DiscordMessage model
                if msg.replied_to_author and msg.replied_to_content:
                    # Message with reply context - use structured format
                    context_lines.append(
                        f"<message{' from-bot="true"' if is_bot_message else ''}>"
                        f"<timestamp>{html.escape(timestamp_str)}</timestamp>"
                        f"<author>{html.escape(msg.author)}</author>"
                        f"<replying-to>"
                        f"<replied-author>{html.escape(msg.replied_to_author)}</replied-author>"
                        f"<replied-content>{html.escape(msg.replied_to_content)}</replied-content>"
                        f"</replying-to>"
                        f"<content>{html.escape(msg.content)}</content>"
                        f"</message>"
                    )
                else:
                    # Regular message without reply context
                    context_lines.append(
                        f"<message{' from-bot="true"' if is_bot_message else ''}>"
                        f"<timestamp>{html.escape(timestamp_str)}</timestamp>"
                        f"<author>{html.escape(msg.author)}</author>"
                        f"<content>{html.escape(msg.content)}</content>"
                        f"</message>"
                    )
            context_str = "\n".join(context_lines)
        
        # Generate response using appropriate agent based on interaction type
        if interaction_type == "mention":
            # Use conversational mention agent
            result = self._mention_agent(
                context_messages=f"<history>{context_str}</history>",
                user_mention=user_question,
                messages_remaining=messages_remaining
            )
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
                        if 'total_tokens' in usage_info:
                            tokens_used += usage_info['total_tokens']
                        elif 'prompt_tokens' in usage_info and 'completion_tokens' in usage_info:
                            tokens_used += usage_info['prompt_tokens'] + usage_info['completion_tokens']
                        elif 'input_tokens' in usage_info and 'output_tokens' in usage_info:
                            tokens_used += usage_info['input_tokens'] + usage_info['output_tokens']
        except Exception as e:
            logger.debug(f"HELP DEBUG: Error with get_lm_usage(): {e}")
        
        # Method 2: Extract from LM history (fallback for Gemini API bug)
        if tokens_used == 0:
            try:
                import dspy
                current_lm = dspy.settings.lm
                if current_lm and hasattr(current_lm, 'history') and current_lm.history:
                    latest_entry = current_lm.history[-1]  # Get the most recent API call
                    
                    # Check response.usage in history (most reliable for Gemini)
                    if 'response' in latest_entry and hasattr(latest_entry['response'], 'usage'):
                        response_usage = latest_entry['response'].usage
                        if hasattr(response_usage, 'total_tokens'):
                            tokens_used = response_usage.total_tokens
                        elif hasattr(response_usage, 'prompt_tokens') and hasattr(response_usage, 'completion_tokens'):
                            tokens_used = response_usage.prompt_tokens + response_usage.completion_tokens
                    
                    # Check for usage field in history
                    elif 'usage' in latest_entry and latest_entry['usage']:
                        usage = latest_entry['usage']
                        if isinstance(usage, dict):
                            if 'total_tokens' in usage:
                                tokens_used = usage['total_tokens']
                            elif 'prompt_tokens' in usage and 'completion_tokens' in usage:
                                tokens_used = usage['prompt_tokens'] + usage['completion_tokens']
                    
                    # Fallback: estimate from cost (Gemini-specific)
                    elif 'cost' in latest_entry and latest_entry['cost'] > 0:
                        cost = latest_entry['cost']
                        # Rough estimation: Gemini Flash pricing ~$0.075 per million tokens
                        estimated_tokens = int(cost * 13333333)
                        tokens_used = estimated_tokens
                        
            except Exception as e:
                logger.debug(f"HELP DEBUG: Error extracting from LM history: {e}")
        
        # Method 3: Legacy fallback for other DSPy completion formats
        if tokens_used == 0 and hasattr(result, '_completions') and result._completions:
            for completion in result._completions:
                # Method 1: Traditional kwargs.usage approach  
                if hasattr(completion, 'kwargs') and completion.kwargs:
                    if 'usage' in completion.kwargs:
                        usage = completion.kwargs['usage']
                        if hasattr(usage, 'total_tokens'):
                            tokens_used += usage.total_tokens
                        elif isinstance(usage, dict):
                            # Try different token field names
                            if 'total_tokens' in usage:
                                tokens_used += usage['total_tokens']
                            elif 'totalTokens' in usage:
                                tokens_used += usage['totalTokens']
                            elif 'prompt_tokens' in usage and 'completion_tokens' in usage:
                                tokens_used += usage['prompt_tokens'] + usage['completion_tokens']
                    
                    # Method 2: Check for response metadata
                    if 'response' in completion.kwargs:
                        response_obj = completion.kwargs['response']
                        if hasattr(response_obj, 'usage') and hasattr(response_obj.usage, 'total_tokens'):
                            tokens_used += response_obj.usage.total_tokens
        
        # Final fallback: estimate tokens from text length if all methods fail
        if tokens_used == 0:
            # Rough estimation: ~4 characters per token for English text
            input_text = f"{context_str}\n{user_question}"
            output_text = result.response
            estimated_tokens = (len(input_text) + len(output_text)) // 4
            tokens_used = estimated_tokens
            logger.warning(f"HELP DEBUG: Fallback estimation - {tokens_used} tokens from text length")
        
        return result.response, tokens_used


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

    **For Detailed Technical Discussions** (long messages, complex topics):
    - Structured breakdown with key points
    - Include technical details and decisions
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

    **Technical Discussion Example:**
    📝 **Channel Summary**
    **Database Migration Issue**: Alice reported deployment failures due to missing schema changes.

    **Root Cause**: Bob identified that the `metadata` field conflicts with SQLAlchemy's reserved attributes.

    **Solution**: Team decided to rename the field to `command_metadata` and run manual migration.
    
    Alice successfully applied the fix and confirmed the admin dashboard is working.
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
        last_space = truncated.rfind(' ')
        if last_space > max_chars * 0.8:  # If we find a space in the last 20%
            truncated = truncated[:last_space]
        
        return truncated + "..."
    
    def prepare_messages_for_context(
        self, 
        messages: List[DiscordMessage], 
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
            # Format current set of messages
            formatted_lines = []
            for msg in current_messages:
                timestamp_str = msg.timestamp.strftime("%m/%d %H:%M")
                
                # Truncate very long messages
                content = self.truncate_message_content(msg.content, 500)
                
                # Use the reply context from the DiscordMessage model
                if msg.replied_to_author and msg.replied_to_content:
                    # Message with reply context - use structured format
                    formatted_lines.append(
                        f"<message>"
                        f"<timestamp>{html.escape(timestamp_str)}</timestamp>"
                        f"<author>{html.escape(msg.author)}</author>"
                        f"<replying-to>"
                        f"<replied-author>{html.escape(msg.replied_to_author)}</replied-author>"
                        f"<replied-content>{html.escape(msg.replied_to_content)}</replied-content>"
                        f"</replying-to>"
                        f"<content>{html.escape(content)}</content>"
                        f"</message>"
                    )
                else:
                    # Regular message without reply context
                    formatted_lines.append(
                        f"<message>"
                        f"<timestamp>{html.escape(timestamp_str)}</timestamp>"
                        f"<author>{html.escape(msg.author)}</author>"
                        f"<content>{html.escape(content)}</content>"
                        f"</message>"
                    )
            
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
        messages: List[DiscordMessage],
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
                            if 'total_tokens' in usage_info:
                                tokens_used += usage_info['total_tokens']
                            elif 'prompt_tokens' in usage_info and 'completion_tokens' in usage_info:
                                tokens_used += usage_info['prompt_tokens'] + usage_info['completion_tokens']
                            elif 'input_tokens' in usage_info and 'output_tokens' in usage_info:
                                tokens_used += usage_info['input_tokens'] + usage_info['output_tokens']
            except Exception as e:
                logger.debug(f"TLDR DEBUG: Error with get_lm_usage(): {e}")
            
            # Method 2: Extract from LM history (fallback for Gemini API bug)
            if tokens_used == 0:
                try:
                    import dspy
                    current_lm = dspy.settings.lm
                    if current_lm and hasattr(current_lm, 'history') and current_lm.history:
                        latest_entry = current_lm.history[-1]  # Get the most recent API call
                        
                        # Check response.usage in history (most reliable for Gemini)
                        if 'response' in latest_entry and hasattr(latest_entry['response'], 'usage'):
                            response_usage = latest_entry['response'].usage
                            if hasattr(response_usage, 'total_tokens'):
                                tokens_used = response_usage.total_tokens
                            elif hasattr(response_usage, 'prompt_tokens') and hasattr(response_usage, 'completion_tokens'):
                                tokens_used = response_usage.prompt_tokens + response_usage.completion_tokens
                        
                        # Check for usage field in history
                        elif 'usage' in latest_entry and latest_entry['usage']:
                            usage = latest_entry['usage']
                            if isinstance(usage, dict):
                                if 'total_tokens' in usage:
                                    tokens_used = usage['total_tokens']
                                elif 'prompt_tokens' in usage and 'completion_tokens' in usage:
                                    tokens_used = usage['prompt_tokens'] + usage['completion_tokens']
                        
                        # Fallback: estimate from cost (Gemini-specific)
                        elif 'cost' in latest_entry and latest_entry['cost'] > 0:
                            cost = latest_entry['cost']
                            # Rough estimation: Gemini Flash pricing ~$0.075 per million tokens
                            estimated_tokens = int(cost * 13333333)
                            tokens_used = estimated_tokens
                            
                except Exception as e:
                    logger.debug(f"TLDR DEBUG: Error extracting from LM history: {e}")
            
            # Method 3: Legacy fallback for other DSPy completion formats
            if tokens_used == 0 and hasattr(result, '_completions') and result._completions:
                for completion in result._completions:
                    # Method 1: Traditional kwargs.usage approach  
                    if hasattr(completion, 'kwargs') and completion.kwargs:
                        if 'usage' in completion.kwargs:
                            usage = completion.kwargs['usage']
                            if hasattr(usage, 'total_tokens'):
                                tokens_used += usage.total_tokens
                            elif isinstance(usage, dict):
                                # Try different token field names
                                if 'total_tokens' in usage:
                                    tokens_used += usage['total_tokens']
                                elif 'totalTokens' in usage:
                                    tokens_used += usage['totalTokens']
                                elif 'prompt_tokens' in usage and 'completion_tokens' in usage:
                                    tokens_used += usage['prompt_tokens'] + usage['completion_tokens']
                        
                        # Method 2: Check for response metadata
                        if 'response' in completion.kwargs:
                            response_obj = completion.kwargs['response']
                            if hasattr(response_obj, 'usage') and hasattr(response_obj.usage, 'total_tokens'):
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


def estimate_message_tokens(messages: List[DiscordMessage]) -> int:
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


class ForumMonitorAgent:
    """Discord forum monitoring agent using Gemini for post evaluation and response generation."""
    
    def __init__(self):
        self._agent = dspy.ChainOfThought(ForumMonitorSignature)
    
    async def evaluate_post(
        self, 
        system_prompt: str,
        post_title: str,
        post_content: str,
        author_display_name: str,
        post_tags: List[str] = None,
        attachment_names: List[str] = None
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
            f"<post>",
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
        
        # Generate evaluation and response
        result = self._agent(
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
                        if 'total_tokens' in usage_info:
                            tokens_used += usage_info['total_tokens']
                        elif 'prompt_tokens' in usage_info and 'completion_tokens' in usage_info:
                            tokens_used += usage_info['prompt_tokens'] + usage_info['completion_tokens']
                        elif 'input_tokens' in usage_info and 'output_tokens' in usage_info:
                            tokens_used += usage_info['input_tokens'] + usage_info['output_tokens']
                print(f"FORUM DEBUG: Extracted {tokens_used} tokens using get_lm_usage()")
        except Exception as e:
            print(f"FORUM DEBUG: Error with get_lm_usage(): {e}")
        
        # Method 2: Extract from LM history (fallback for Gemini API bug)
        if tokens_used == 0:
            try:
                import dspy
                current_lm = dspy.settings.lm
                if current_lm and hasattr(current_lm, 'history') and current_lm.history:
                    latest_entry = current_lm.history[-1]  # Get the most recent API call
                    
                    # Check response.usage in history (most reliable for Gemini)
                    if 'response' in latest_entry and hasattr(latest_entry['response'], 'usage'):
                        response_usage = latest_entry['response'].usage
                        if hasattr(response_usage, 'total_tokens'):
                            tokens_used = response_usage.total_tokens
                        elif hasattr(response_usage, 'prompt_tokens') and hasattr(response_usage, 'completion_tokens'):
                            tokens_used = response_usage.prompt_tokens + response_usage.completion_tokens
                        print(f"FORUM DEBUG: Extracted {tokens_used} tokens from LM history response.usage")
                    
                    # Check for usage field in history
                    elif 'usage' in latest_entry and latest_entry['usage']:
                        usage = latest_entry['usage']
                        if isinstance(usage, dict):
                            if 'total_tokens' in usage:
                                tokens_used = usage['total_tokens']
                            elif 'prompt_tokens' in usage and 'completion_tokens' in usage:
                                tokens_used = usage['prompt_tokens'] + usage['completion_tokens']
                        print(f"FORUM DEBUG: Extracted {tokens_used} tokens from LM history usage")
                    
                    # Fallback: estimate from cost (Gemini-specific)
                    elif 'cost' in latest_entry and latest_entry['cost'] > 0:
                        cost = latest_entry['cost']
                        # Rough estimation: Gemini Flash pricing ~$0.075 per million tokens
                        estimated_tokens = int(cost * 13333333)
                        tokens_used = estimated_tokens
                        print(f"FORUM DEBUG: Estimated {tokens_used} tokens from cost: ${cost}")
                        
            except Exception as e:
                print(f"FORUM DEBUG: Error extracting from LM history: {e}")
        
        # Method 3: Legacy fallback for other DSPy completion formats
        if tokens_used == 0 and hasattr(result, '_completions') and result._completions:
            for completion in result._completions:
                # Method 1: Traditional kwargs.usage approach  
                if hasattr(completion, 'kwargs') and completion.kwargs:
                    if 'usage' in completion.kwargs:
                        usage = completion.kwargs['usage']
                        if hasattr(usage, 'total_tokens'):
                            tokens_used += usage.total_tokens
                        elif isinstance(usage, dict):
                            # Try different token field names
                            if 'total_tokens' in usage:
                                tokens_used += usage['total_tokens']
                            elif 'totalTokens' in usage:
                                tokens_used += usage['totalTokens']
                            elif 'prompt_tokens' in usage and 'completion_tokens' in usage:
                                tokens_used += usage['prompt_tokens'] + usage['completion_tokens']
                    
                    # Method 2: Check for response metadata
                    if 'response' in completion.kwargs:
                        response_obj = completion.kwargs['response']
                        if hasattr(response_obj, 'usage') and hasattr(response_obj.usage, 'total_tokens'):
                            tokens_used += response_obj.usage.total_tokens
                
                # Method 3: DSPy's get_lm_usage method (the correct approach!)
                if hasattr(completion, 'get_lm_usage') and callable(getattr(completion, 'get_lm_usage')):
                    try:
                        usage_stats = completion.get_lm_usage()
                        if isinstance(usage_stats, dict):
                            # Try different field names for total tokens
                            if 'total_tokens' in usage_stats:
                                tokens_used += usage_stats['total_tokens']
                            elif 'prompt_tokens' in usage_stats and 'completion_tokens' in usage_stats:
                                tokens_used += usage_stats['prompt_tokens'] + usage_stats['completion_tokens']
                            elif 'input_tokens' in usage_stats and 'output_tokens' in usage_stats:
                                tokens_used += usage_stats['input_tokens'] + usage_stats['output_tokens']
                        elif hasattr(usage_stats, 'total_tokens'):
                            tokens_used += usage_stats.total_tokens
                    except Exception as e:
                        print(f"FORUM DEBUG: Error calling get_lm_usage(): {e}")
                
                # Method 4: Direct completion attributes (fallback)  
                if tokens_used == 0:
                    for attr in ['usage', 'response', 'metadata', 'raw_response', 'completion']:
                        if hasattr(completion, attr):
                            attr_value = getattr(completion, attr)
                            
                            # Direct usage object  
                            if hasattr(attr_value, 'total_tokens'):
                                tokens_used += attr_value.total_tokens
                                break
                            elif hasattr(attr_value, 'usage') and hasattr(attr_value.usage, 'total_tokens'):
                                tokens_used += attr_value.usage.total_tokens
                                break
                            
                            # Dictionary with usage info
                            elif isinstance(attr_value, dict):
                                if 'total_tokens' in attr_value:
                                    tokens_used += attr_value['total_tokens']
                                    break
                                elif 'usage' in attr_value and isinstance(attr_value['usage'], dict):
                                    usage_dict = attr_value['usage']
                                    if 'total_tokens' in usage_dict:
                                        tokens_used += usage_dict['total_tokens']
                                        break
                                    elif 'prompt_tokens' in usage_dict and 'completion_tokens' in usage_dict:
                                        tokens_used += usage_dict['prompt_tokens'] + usage_dict['completion_tokens']
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
