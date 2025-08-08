import dspy
import dotenv
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass
from pydantic import BaseModel


lm = dspy.LM("gemini/gemini-2.0-flash-lite", api_key=dotenv.get_key(".env", "GEMINI_API_KEY"))
dspy.configure(lm=lm)


class DiscordMessage(BaseModel):
    """Represents a Discord message for context."""
    author: str
    timestamp: datetime
    content: str


class RateLimiter:
    """In-memory rate limiter for user requests and token usage."""
    
    def __init__(self):
        self.user_requests: Dict[str, List[datetime]] = {}
        self.token_usage: List[tuple[datetime, int]] = []  # (timestamp, token_count) pairs
        self.USER_LIMIT = 10  # messages per 30 minutes
        self.USER_WINDOW = timedelta(minutes=30)
        self.TOKEN_LIMIT = 500_000  # tokens per hour
        self.TOKEN_WINDOW = timedelta(hours=1)
    
    def cleanup_expired_entries(self):
        """Remove expired entries to prevent memory leaks."""
        now = datetime.now()
        
        # Clean up user requests
        for user_id in list(self.user_requests.keys()):
            self.user_requests[user_id] = [
                req_time for req_time in self.user_requests[user_id]
                if now - req_time < self.USER_WINDOW
            ]
            if not self.user_requests[user_id]:
                del self.user_requests[user_id]
        
        # Clean up token usage
        self.token_usage = [
            (usage_time, tokens) for usage_time, tokens in self.token_usage
            if now - usage_time < self.TOKEN_WINDOW
        ]
    
    def check_user_limit(self, user_id: str) -> bool:
        """Check if user is within rate limit."""
        self.cleanup_expired_entries()
        user_requests = self.user_requests.get(user_id, [])
        return len(user_requests) < self.USER_LIMIT
    
    def check_token_limit(self, estimated_tokens: int = 1000) -> bool:
        """Check if we're within token usage limit."""
        self.cleanup_expired_entries()
        # Sum actual token usage in the last hour
        current_usage = sum(tokens for _, tokens in self.token_usage)
        return current_usage + estimated_tokens < self.TOKEN_LIMIT
    
    def record_request(self, user_id: str, tokens_used: int):
        """Record a user request and actual token usage."""
        now = datetime.now()
        if user_id not in self.user_requests:
            self.user_requests[user_id] = []
        self.user_requests[user_id].append(now)
        
        # Always record token usage (required parameter)
        self.token_usage.append((now, tokens_used))
    
    def get_user_remaining_requests(self, user_id: str) -> int:
        """Get number of remaining requests for user."""
        self.cleanup_expired_entries()
        used = len(self.user_requests.get(user_id, []))
        return max(0, self.USER_LIMIT - used)
    
    def get_user_reset_time(self, user_id: str) -> Optional[datetime]:
        """Get when user's rate limit resets."""
        user_requests = self.user_requests.get(user_id, [])
        if not user_requests:
            return None
        return user_requests[0] + self.USER_WINDOW
    
    def get_current_token_usage(self) -> int:
        """Get current token usage in the last hour."""
        self.cleanup_expired_entries()
        return sum(tokens for _, tokens in self.token_usage)


# Global rate limiter instance
rate_limiter = RateLimiter()


class HelpAgentSignature(dspy.Signature):
    """You are a helpful Discord bot assistant for the Smarter Dev community. You help users understand and use the bot's bytes economy and squad management systems.

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

    ### How Bytes Work:
    - **Starting Balance**: New users get a starting balance (usually 128 bytes)
    - **Daily Rewards**: Users get a daily bytes reward for their first message every day UTC time
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
    - Be helpful and friendly
    - Provide specific command examples
    - Explain restrictions and limits clearly
    - Use the conversation context to give relevant answers
    - Keep responses concise but informative
    - Use emojis sparingly and appropriately
    """
    
    context_messages: str = dspy.InputField(description="Recent conversation messages for context")
    user_question: str = dspy.InputField(description="The user's question about the bot")
    response: str = dspy.OutputField(description="Helpful response explaining bot functionality")


class HelpAgent:
    """Discord bot help agent using Gemini for conversational assistance."""
    
    def __init__(self):
        self._agent = dspy.ChainOfThought(HelpAgentSignature)
    
    def generate_response(
        self, 
        user_question: str, 
        context_messages: List[DiscordMessage] = None
    ) -> tuple[str, int]:
        """Generate a helpful response to a user's question.
        
        Args:
            user_question: The user's question about the bot
            context_messages: Recent conversation messages for context
            
        Returns:
            tuple[str, int]: Generated response and token usage count
        """
        # Format context messages
        context_str = ""
        if context_messages:
            context_lines = []
            for msg in context_messages[-5:]:  # Last 5 messages
                timestamp_str = msg.timestamp.strftime("%m/%d %H:%M")
                context_lines.append(
                    f"<message>"
                    f"<timestamp>{timestamp_str}</timestamp>"
                    f"<author>{msg.author}</author>"
                    f"<content>{msg.content}</content>"
                    f"</message>"
                )
            context_str = "\n".join(context_lines)
        
        # Generate response
        result = self._agent(
            context_messages=f"<history>{context_str}</history>",
            user_question=user_question
        )
        
        # Get token usage from DSPy prediction
        tokens_used = 0
        if hasattr(result, '_completions') and result._completions:
            # Sum token usage from all completions
            for completion in result._completions:
                if hasattr(completion, 'kwargs') and 'usage' in completion.kwargs:
                    usage = completion.kwargs['usage']
                    if hasattr(usage, 'total_tokens'):
                        tokens_used += usage.total_tokens
                    elif isinstance(usage, dict) and 'total_tokens' in usage:
                        tokens_used += usage['total_tokens']
        
        return result.response, tokens_used


class TLDRAgentSignature(dspy.Signature):
    """You are a helpful Discord bot that creates concise summaries of channel conversations.

    ## TASK
    Analyze the provided Discord messages and create a clear, informative summary of the conversation.

    ## GUIDELINES
    - **Be Concise**: Aim for 2-4 sentences maximum
    - **Capture Key Points**: Include main topics, decisions, or important information discussed
    - **Identify Participants**: Mention key contributors when relevant
    - **Maintain Context**: Preserve important details and relationships between messages
    - **Use Natural Language**: Write in a conversational, easy-to-read style
    - **Handle Various Content**: Summarize technical discussions, casual chat, questions/answers, etc.

    ## RESPONSE FORMAT
    - Start with "📝 **Channel Summary**" 
    - Provide the summary in 2-4 clear sentences
    - End with message count: "(Summarized X messages)"

    ## EXAMPLE OUTPUT
    📝 **Channel Summary**
    Users discussed implementing a new bot feature for message summarization. Alice suggested using an LLM approach while Bob raised concerns about rate limiting. The team decided to create a separate plugin with progressive context handling to manage long conversations.
    (Summarized 12 messages)
    """
    
    messages: str = dspy.InputField(description="Discord messages to summarize, formatted as structured data")
    summary: str = dspy.OutputField(description="Concise summary of the conversation")


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
                
                formatted_lines.append(
                    f"<message>"
                    f"<timestamp>{timestamp_str}</timestamp>"
                    f"<author>{msg.author}</author>"
                    f"<content>{content}</content>"
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
            return ("📝 **Channel Summary**\nNo messages found to summarize. The channel might be empty or contain only bot messages.\n(Summarized 0 messages)", 0, 0)
        
        try:
            # Generate summary
            result = self._agent(messages=formatted_messages)
            
            # Get token usage from DSPy prediction
            tokens_used = 0
            if hasattr(result, '_completions') and result._completions:
                for completion in result._completions:
                    if hasattr(completion, 'kwargs') and 'usage' in completion.kwargs:
                        usage = completion.kwargs['usage']
                        if hasattr(usage, 'total_tokens'):
                            tokens_used += usage.total_tokens
                        elif isinstance(usage, dict) and 'total_tokens' in usage:
                            tokens_used += usage['total_tokens']
            
            return result.summary, tokens_used, messages_used
            
        except Exception as e:
            # Generate a helpful error message using the agent with minimal context
            error_context = f"<error>Failed to summarize {messages_used} messages due to: {str(e)[:200]}</error>"
            
            try:
                error_result = self._agent(messages=error_context)
                return error_result.summary, 0, 0
            except:
                # Final fallback
                return ("📝 **Channel Summary**\nSorry, there was too much content to summarize. Try using a smaller message count or wait a moment before trying again.\n(Unable to process messages)", 0, 0)


def estimate_message_tokens(messages: List[DiscordMessage]) -> int:
    """Estimate total token count for a list of messages."""
    total_chars = sum(len(msg.content) + len(msg.author) + 50 for msg in messages)  # +50 for formatting
    return total_chars // 4  # Rough estimation of 4 chars per token
