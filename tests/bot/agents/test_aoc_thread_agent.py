"""Tests for the AoC thread agent."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest


class TestAoCThreadAgentDateSuffix:
    """Tests for ordinal suffix generation."""

    def test_suffix_1st(self):
        """Day 1 gets 'st' suffix."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent
        agent = AoCThreadAgent()
        assert agent._get_date_suffix(1) == "st"

    def test_suffix_2nd(self):
        """Day 2 gets 'nd' suffix."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent
        agent = AoCThreadAgent()
        assert agent._get_date_suffix(2) == "nd"

    def test_suffix_3rd(self):
        """Day 3 gets 'rd' suffix."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent
        agent = AoCThreadAgent()
        assert agent._get_date_suffix(3) == "rd"

    def test_suffix_4th_through_10th(self):
        """Days 4-10 get 'th' suffix."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent
        agent = AoCThreadAgent()
        for day in range(4, 11):
            assert agent._get_date_suffix(day) == "th", f"Day {day} should have 'th' suffix"

    def test_suffix_11th_12th_13th(self):
        """Days 11, 12, 13 are special cases with 'th' suffix."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent
        agent = AoCThreadAgent()
        assert agent._get_date_suffix(11) == "th"
        assert agent._get_date_suffix(12) == "th"
        assert agent._get_date_suffix(13) == "th"

    def test_suffix_21st_22nd_23rd(self):
        """Days 21, 22, 23 follow the pattern."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent
        agent = AoCThreadAgent()
        assert agent._get_date_suffix(21) == "st"
        assert agent._get_date_suffix(22) == "nd"
        assert agent._get_date_suffix(23) == "rd"

    def test_suffix_24th_25th(self):
        """Days 24 and 25 get 'th' suffix."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent
        agent = AoCThreadAgent()
        assert agent._get_date_suffix(24) == "th"
        assert agent._get_date_suffix(25) == "th"


class TestAoCThreadAgentMessageGeneration:
    """Tests for message generation with mocked LLM."""

    @pytest.fixture
    def mock_dspy_predict(self):
        """Mock dspy.Predict for testing."""
        with patch('smarter_dev.bot.agents.aoc_thread_agent.dspy') as mock_dspy:
            # Mock the context manager
            mock_context = Mock()
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)

            yield mock_dspy

    async def test_generate_thread_message_returns_tuple(self):
        """generate_thread_message returns (message, tokens) tuple."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent

        with patch('smarter_dev.bot.agents.aoc_thread_agent.dspy') as mock_dspy:
            # Setup mock
            mock_result = Mock()
            mock_result.response = "Test message for day 5!"

            async_mock = AsyncMock(return_value=mock_result)
            mock_dspy.asyncify.return_value = async_mock
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)

            agent = AoCThreadAgent()

            # Mock _extract_token_usage
            with patch.object(agent, '_extract_token_usage', return_value=50):
                result = await agent.generate_thread_message(5, 2025)

            assert isinstance(result, tuple)
            assert len(result) == 2
            assert isinstance(result[0], str)
            assert isinstance(result[1], int)

    async def test_generate_thread_message_fallback_on_error(self):
        """Returns fallback message when LLM fails."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent

        with patch('smarter_dev.bot.agents.aoc_thread_agent.dspy') as mock_dspy:
            # Make the call raise an exception
            mock_dspy.context.side_effect = Exception("LLM API error")

            agent = AoCThreadAgent()
            message, tokens = await agent.generate_thread_message(10, 2025)

            assert "Day 10 is here!" in message
            assert tokens == 0

    async def test_generate_thread_message_final_day_fallback(self):
        """Returns special final day (day 12) fallback message when LLM fails."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent, AOC_FINAL_DAY

        with patch('smarter_dev.bot.agents.aoc_thread_agent.dspy') as mock_dspy:
            mock_dspy.context.side_effect = Exception("LLM API error")

            agent = AoCThreadAgent()
            message, tokens = await agent.generate_thread_message(AOC_FINAL_DAY, 2025)

            # AOC_FINAL_DAY is 12, the fallback message mentions "final day"
            assert "final day" in message
            assert "Merry Christmas" in message
            assert tokens == 0

    async def test_generate_thread_message_uses_correct_signature_for_regular_day(self):
        """Uses AoCThreadSignature for non-final days."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent

        with patch('smarter_dev.bot.agents.aoc_thread_agent.dspy') as mock_dspy:
            mock_result = Mock()
            mock_result.response = "Day 5 message"

            async_mock = AsyncMock(return_value=mock_result)
            mock_dspy.asyncify.return_value = async_mock
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)

            agent = AoCThreadAgent()

            with patch.object(agent, '_extract_token_usage', return_value=50):
                await agent.generate_thread_message(5, 2025)

            # The regular agent should be asyncified
            mock_dspy.asyncify.assert_called()
            # Check it was called with the regular agent, not final_day agent
            call_args = mock_dspy.asyncify.call_args[0]
            assert call_args[0] == agent._agent

    async def test_generate_thread_message_uses_final_day_signature_for_final_day(self):
        """Uses AoCFinalDaySignature for the final day (day 12)."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent, AOC_FINAL_DAY

        with patch('smarter_dev.bot.agents.aoc_thread_agent.dspy') as mock_dspy:
            mock_result = Mock()
            mock_result.response = "Final day celebration!"

            async_mock = AsyncMock(return_value=mock_result)
            mock_dspy.asyncify.return_value = async_mock
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)

            agent = AoCThreadAgent()

            with patch.object(agent, '_extract_token_usage', return_value=100):
                await agent.generate_thread_message(AOC_FINAL_DAY, 2025)

            # The final_day agent should be asyncified
            mock_dspy.asyncify.assert_called()
            call_args = mock_dspy.asyncify.call_args[0]
            assert call_args[0] == agent._final_day_agent


class TestAoCThreadAgentSignatures:
    """Tests for DSPy signature definitions."""

    def test_regular_signature_has_required_fields(self):
        """AoCThreadSignature has day, date_str, year inputs and response output."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadSignature

        # DSPy signatures store field info in model_fields
        fields = AoCThreadSignature.model_fields
        assert 'day' in fields
        assert 'date_str' in fields
        assert 'year' in fields
        assert 'response' in fields

    def test_final_day_signature_has_required_fields(self):
        """AoCFinalDaySignature has year input and response output."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCFinalDaySignature

        fields = AoCFinalDaySignature.model_fields
        assert 'year' in fields
        assert 'response' in fields


class TestAoCThreadAgentTokenEstimation:
    """Tests for token usage estimation fallback."""

    async def test_estimates_tokens_from_response_length(self):
        """Estimates tokens as chars // 4 when usage not available."""
        from smarter_dev.bot.agents.aoc_thread_agent import AoCThreadAgent

        with patch('smarter_dev.bot.agents.aoc_thread_agent.dspy') as mock_dspy:
            mock_result = Mock()
            # 100 characters = ~25 tokens
            mock_result.response = "A" * 100

            async_mock = AsyncMock(return_value=mock_result)
            mock_dspy.asyncify.return_value = async_mock
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)

            agent = AoCThreadAgent()

            # Return 0 to trigger fallback estimation
            with patch.object(agent, '_extract_token_usage', return_value=0):
                message, tokens = await agent.generate_thread_message(5, 2025)

            # Should estimate ~25 tokens (100 chars / 4)
            assert tokens == 25


class TestGlobalAgentInstance:
    """Tests for the global agent instance."""

    def test_global_instance_exists(self):
        """aoc_thread_agent global instance is available."""
        from smarter_dev.bot.agents.aoc_thread_agent import aoc_thread_agent

        assert aoc_thread_agent is not None

    def test_global_instance_is_aoc_thread_agent(self):
        """Global instance is an AoCThreadAgent."""
        from smarter_dev.bot.agents.aoc_thread_agent import aoc_thread_agent, AoCThreadAgent

        assert isinstance(aoc_thread_agent, AoCThreadAgent)
