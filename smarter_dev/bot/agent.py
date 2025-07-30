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
