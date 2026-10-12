"""Base agent class with shared functionality."""

import logging

logger = logging.getLogger(__name__)


class BaseAgent:
    """Base class for all Discord bot agents with shared utility methods."""

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count for text (approximately 4 chars per token)."""
        return len(text) // 4

    def _validate_response_length(self, response: str, max_length: int = 2000) -> str:
        """Ensure response fits within Discord's character limit.

        Args:
            response: The response text to validate
            max_length: Maximum allowed length (default: 2000 for Discord)

        Returns:
            Response truncated if necessary, with ellipsis if truncated
        """
        if len(response) <= max_length:
            return response

        # Truncate and add ellipsis
        truncated = response[:max_length - 3] + "..."
        logger.warning(f"Response truncated from {len(response)} to {max_length} characters")
        return truncated
