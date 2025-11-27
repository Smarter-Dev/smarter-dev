"""Base agent class with shared functionality."""

import logging

logger = logging.getLogger(__name__)


class BaseAgent:
    """Base class for all Discord bot agents with shared utility methods."""

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
                total_tokens = usage_data.get("total_tokens", 0)
                completion_tokens = usage_data.get("completion_tokens", 0)
                prompt_tokens = usage_data.get("prompt_tokens", 0)

                tokens_used = total_tokens if total_tokens else (prompt_tokens + completion_tokens)
                logger.debug(f"DSPy usage data: {usage_data}, extracted tokens: {tokens_used}")
                return tokens_used
        except (AttributeError, TypeError) as e:
            logger.debug(f"DSPy get_lm_usage() not available or empty: {e}")

        # Method 2: Fallback to token counting from prediction object
        # This handles Claude/Anthropic, Gemini, and other provider-specific response structures
        try:
            if hasattr(result, "_completions") and result._completions:
                completion = result._completions[0]
                if hasattr(completion, "_usage") and completion._usage:
                    usage = completion._usage
                    total_tokens = getattr(usage, "total_tokens", None)
                    completion_tokens = getattr(usage, "completion_tokens", None)
                    prompt_tokens = getattr(usage, "prompt_tokens", None)

                    tokens_used = total_tokens if total_tokens else (
                        (prompt_tokens or 0) + (completion_tokens or 0)
                    )
                    logger.debug(f"Completion usage: {usage}, extracted tokens: {tokens_used}")
                    return tokens_used
        except (AttributeError, TypeError) as e:
            logger.debug(f"Failed to extract usage from completions: {e}")

        # Method 3: Check for Claude/Anthropic specific response structure
        try:
            if hasattr(result, "completion") and result.completion:
                # Some versions of DSPy may structure Claude responses differently
                completion_obj = result.completion
                if hasattr(completion_obj, "usage"):
                    usage = completion_obj.usage
                    tokens_used = getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0)
                    if tokens_used > 0:
                        logger.debug(f"Claude usage extracted from completion.usage: {tokens_used}")
                        return tokens_used
        except (AttributeError, TypeError) as e:
            logger.debug(f"Failed to extract Claude-specific usage: {e}")

        return tokens_used

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
