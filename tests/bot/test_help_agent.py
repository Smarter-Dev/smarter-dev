"""Tests for the Discord bot help agent system.

The model call itself is covered in tests/bot/agents/test_utility_agents.py.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, AsyncMock, patch
from typing import List

from smarter_dev.bot.agents.help_agent import HelpAgent
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.bot.services.rate_limiter import RateLimiter, rate_limiter


class TestDiscordMessage:
    """Tests for DiscordMessage model."""

    def test_discord_message_creation(self):
        """Test creating a DiscordMessage."""
        now = datetime.now(timezone.utc)
        message = DiscordMessage(
            author="TestUser",
            timestamp=now,
            content="How do I send bytes?"
        )

        assert message.author == "TestUser"
        assert message.timestamp == now
        assert message.content == "How do I send bytes?"


class TestRateLimiter:
    """Tests for the rate limiting system."""

    def setup_method(self):
        """Setup for each test."""
        self.limiter = RateLimiter()

    def test_user_within_limit_initially(self):
        """Test that users start within rate limits."""
        assert self.limiter.check_user_limit("user123") is True
        assert self.limiter.get_user_remaining_requests("user123") == 10

    def test_user_limit_enforcement(self):
        """Test that user rate limits are enforced."""
        user_id = "user123"

        # Make 10 requests (at limit)
        for _ in range(10):
            self.limiter.record_request(user_id, 100)  # Add token count

        assert self.limiter.check_user_limit(user_id) is False
        assert self.limiter.get_user_remaining_requests(user_id) == 0

    def test_user_limit_resets_after_window(self):
        """Test that user limits reset after time window."""
        user_id = "user123"

        # Mock old requests (beyond window) using the actual attribute structure:
        # user_command_requests is dict[str, dict[str, list[datetime]]]
        old_time = datetime.now() - timedelta(minutes=31)
        self.limiter.user_command_requests[user_id] = {"help": [old_time] * 10}

        # Should be allowed after cleanup (cleanup happens inside check_user_limit)
        assert self.limiter.check_user_limit(user_id) is True
        assert self.limiter.get_user_remaining_requests(user_id) == 10

    def test_token_limit_check(self):
        """Test token usage limit checking."""
        assert self.limiter.check_token_limit(1000) is True

        # Add many token usage entries: (timestamp, tokens, command_type) tuples
        now = datetime.now()
        self.limiter.token_usage = [(now, 1000, "help")] * 500  # 500k tokens (500 * 1000)

        assert self.limiter.check_token_limit(1000) is False

    def test_cleanup_expired_entries(self):
        """Test that expired entries are cleaned up."""
        user_id = "user123"
        now = datetime.now()
        old_time = now - timedelta(hours=2)  # Use hours to ensure it's definitely expired

        # Add old entries using the actual attribute structure
        self.limiter.user_command_requests[user_id] = {"help": [old_time, old_time]}
        self.limiter.token_usage = [(old_time, 1000, "help"), (old_time, 500, "help")]

        self.limiter.cleanup_expired_entries()

        assert user_id not in self.limiter.user_command_requests
        assert len(self.limiter.token_usage) == 0

    def test_token_usage_tracking(self):
        """Test that actual token usage is tracked correctly."""
        user_id = "user123"

        # Record requests with actual token usage
        self.limiter.record_request(user_id, 1000)
        self.limiter.record_request(user_id, 1500)

        # Check token usage
        assert self.limiter.get_current_token_usage() == 2500
        assert self.limiter.check_token_limit(1000) is True  # Should still be under limit

        # Add large token usage to approach limit
        self.limiter.record_request(user_id, 497000)  # Total: 500,500

        # Should now be at capacity
        assert self.limiter.check_token_limit(1000) is False

    def test_token_usage_edge_cases(self):
        """Test edge cases for token usage tracking."""
        user_id = "user123"

        # Record request with zero tokens
        self.limiter.record_request(user_id, 0)

        # User should still be tracked
        assert self.limiter.get_user_remaining_requests(user_id) == 9

        # Token usage should be 0
        assert self.limiter.get_current_token_usage() == 0

        # Check that zero tokens don't affect limit
        assert self.limiter.check_token_limit(1000) is True


class TestHelpAgent:
    """Tests for the help agent functionality."""

    def setup_method(self):
        """Setup for each test."""
        self.agent = HelpAgent()

    def test_agent_is_help_agent(self):
        """Test that help agent is the correct instance type."""
        agent = HelpAgent()
        assert isinstance(agent, HelpAgent)

    def test_response_validation(self):
        """Test that agent validates response length."""
        agent = HelpAgent()
        # Test with a short response
        response = "Short test response"
        validated = agent._validate_response_length(response)
        assert validated == response

        # Test with a response exceeding Discord limit
        long_response = "a" * 2500
        validated = agent._validate_response_length(long_response)
        assert len(validated) <= 2000
        assert validated.endswith("...")


# Integration tests for the complete help system
class TestHelpSystemIntegration:
    """Integration tests for the complete help system."""

    @pytest.fixture
    def mock_bot(self):
        """Create a mock Discord bot."""
        bot = Mock()
        bot.rest = AsyncMock()
        bot.get_me.return_value = Mock(id=12345)
        return bot

    @pytest.fixture
    def mock_context(self, mock_bot):
        """Create a mock command context."""
        ctx = Mock()
        ctx.bot = mock_bot
        ctx.user = Mock(id=67890, username="TestUser", display_name="TestUser")
        ctx.channel_id = 11111
        ctx.guild_id = 99999
        ctx.options = Mock(question="How do I send bytes?")
        ctx.respond = AsyncMock()
        ctx.edit_last_response = AsyncMock()
        return ctx

    @pytest.mark.asyncio
    async def test_help_command_basic_flow(self, mock_context):
        """Test the basic flow of the help command."""
        from smarter_dev.bot.plugins.help import help_command

        with patch('smarter_dev.bot.plugins.help.generate_help_response', new_callable=AsyncMock) as mock_generate:
            mock_generate.return_value = "Test response"

            await help_command(mock_context)

            # The help command defers first, then edits last response
            mock_generate.assert_called_once()
            # Verify deferred response was sent
            mock_context.respond.assert_called_once()
            # Verify actual response was sent via edit_last_response
            mock_context.edit_last_response.assert_called_once_with("Test response")

    @pytest.mark.asyncio
    async def test_rate_limiting_integration(self, mock_context):
        """Test that rate limiting is properly integrated."""
        from smarter_dev.bot.plugins.help import generate_help_response

        user_id = "test_rate_limit_user_" + str(id(self))

        # Fill up the rate limit
        for _ in range(10):
            rate_limiter.record_request(user_id, 100)  # Add token count

        # Next request should be rate limited
        # generate_help_response requires (user_id, user_question, bot, ...)
        mock_bot = Mock()
        mock_bot.rest = AsyncMock()
        response = await generate_help_response(
            user_id=user_id,
            user_question="test question",
            bot=mock_bot,
            guild_id="123",
            channel_id="456",
            user_username="TestUser",
            interaction_type="slash_command"
        )

        assert "rate limit" in response.lower()
        assert "10" in response  # Mentions the limit
        assert "30 minutes" in response

    def test_global_rate_limiter_persistence(self):
        """Test that the global rate limiter persists state."""
        from smarter_dev.bot.services.rate_limiter import rate_limiter as imported_limiter

        user_id = "persistence_test_" + str(id(self))
        imported_limiter.record_request(user_id, 150)  # Add token count

        # Should have 9 remaining requests
        assert imported_limiter.get_user_remaining_requests(user_id) == 9
