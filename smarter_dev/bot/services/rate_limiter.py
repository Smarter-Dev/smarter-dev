"""Rate limiting service for bot commands and token usage."""

import logging
from datetime import datetime
from datetime import timedelta

logger = logging.getLogger(__name__)


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
