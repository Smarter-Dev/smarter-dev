"""Tests for the Discord bot help agent system.

This module provides comprehensive tests for the help agent, including LLM-based
evaluation tests that use Gemini 2.5 Flash to assess response quality.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, AsyncMock, patch
from typing import List

from smarter_dev.bot.agent import (
    HelpAgent, 
    DiscordMessage, 
    RateLimiter,
    rate_limiter
)


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
        
        # Mock old requests (beyond window)
        old_time = datetime.now() - timedelta(minutes=31)
        self.limiter.user_requests[user_id] = [old_time] * 10
        
        # Should be allowed after cleanup
        assert self.limiter.check_user_limit(user_id) is True
        assert self.limiter.get_user_remaining_requests(user_id) == 10
    
    def test_token_limit_check(self):
        """Test token usage limit checking."""
        assert self.limiter.check_token_limit(1000) is True
        
        # Add many token usage entries (timestamp, tokens) tuples
        now = datetime.now()
        self.limiter.token_usage = [(now, 1000)] * 500  # 500k tokens (500 * 1000)
        
        assert self.limiter.check_token_limit(1000) is False
    
    def test_cleanup_expired_entries(self):
        """Test that expired entries are cleaned up."""
        user_id = "user123"
        now = datetime.now()
        old_time = now - timedelta(hours=2)  # Use hours to ensure it's definitely expired
        
        # Add old entries
        self.limiter.user_requests[user_id] = [old_time, old_time]
        self.limiter.token_usage = [(old_time, 1000), (old_time, 500)]  # (timestamp, tokens) tuples
        
        self.limiter.cleanup_expired_entries()
        
        assert user_id not in self.limiter.user_requests
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
    
    def test_agent_initialization(self):
        """Test that help agent initializes correctly."""
        assert self.agent._agent is not None
    
    @patch('smarter_dev.bot.agent.dspy.ChainOfThought')
    def test_generate_response_no_context(self, mock_chain):
        """Test response generation without context."""
        # Mock the DSPy chain
        mock_result = Mock()
        mock_result.response = "Test response"
        mock_result._completions = []  # No token usage
        mock_chain.return_value.return_value = mock_result
        
        agent = HelpAgent()
        response, tokens = agent.generate_response("How do I send bytes?")
        
        assert response == "Test response"
        assert tokens == 0  # No token usage recorded
        mock_chain.return_value.assert_called_once()
    
    @patch('smarter_dev.bot.agent.dspy.ChainOfThought')
    def test_generate_response_with_context(self, mock_chain):
        """Test response generation with message context."""
        # Mock the DSPy chain
        mock_result = Mock()
        mock_result.response = "Contextual response"
        mock_result._completions = []  # No token usage
        mock_chain.return_value.return_value = mock_result
        
        # Create context messages
        context = [
            DiscordMessage(
                author="User1",
                timestamp=datetime.now(timezone.utc),
                content="I'm new here"
            ),
            DiscordMessage(
                author="User2",
                timestamp=datetime.now(timezone.utc),
                content="Welcome!"
            )
        ]
        
        agent = HelpAgent()
        response, tokens = agent.generate_response("How do I get started?", context)
        
        assert response == "Contextual response"
        assert tokens == 0  # No token usage recorded
        
        # Verify context was formatted and passed with new XML format
        call_args = mock_chain.return_value.call_args
        context_arg = call_args[1]["context_messages"]
        assert "<author>User1</author>" in context_arg
        assert "<author>User2</author>" in context_arg
        assert "<content>I'm new here</content>" in context_arg
        assert "<content>Welcome!</content>" in context_arg
        assert "<history>" in context_arg and "</history>" in context_arg


# LLM-based evaluation tests (marked to be skipped in normal runs)
@pytest.mark.llm
class TestHelpAgentLLMEvaluation:
    """LLM-based tests that evaluate help agent response quality using Gemini 2.5."""
    
    def setup_method(self):
        """Setup for LLM tests."""
        self.agent = HelpAgent()
        # Initialize evaluator LLM (Gemini 2.5 Flash)
        import dspy
        from smarter_dev.llm_config import get_llm_model
        
        self.evaluator_lm = get_llm_model("judge")
    
    def evaluate_response_quality(self, question: str, response: str, context: str = "") -> dict:
        """Evaluate response quality using Gemini 2.5 Flash.
        
        Args:
            question: Original user question
            response: Agent's response
            context: Conversation context
            
        Returns:
            dict: Evaluation results with scores and feedback
        """
        evaluation_prompt = f"""
        Evaluate the quality of this Discord bot help response on a scale of 1-10 for each criterion:
        
        QUESTION: {question}
        CONTEXT: {context}
        RESPONSE: {response}
        
        Rate the response on:
        1. ACCURACY (1-10): Is the information correct and complete?
        2. HELPFULNESS (1-10): Does it actually help the user?
        3. CLARITY (1-10): Is it easy to understand?
        4. DISCORD_APPROPRIATE (1-10): Is it suitable for Discord (plain text, good formatting)?
        5. CONTEXT_AWARE (1-10): Does it use the conversation context appropriately?
        
        Respond in this exact format:
        ACCURACY: [score]
        HELPFULNESS: [score]
        CLARITY: [score]
        DISCORD_APPROPRIATE: [score]
        CONTEXT_AWARE: [score]
        OVERALL: [average score]
        FEEDBACK: [brief explanation of strengths and weaknesses]
        """
        
        # Use evaluator LLM
        evaluation = self.evaluator_lm.generate(evaluation_prompt)
        
        # Parse the evaluation
        lines = evaluation.split('\n')
        results = {}
        
        for line in lines:
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip().lower()
                value = value.strip()
                
                if key in ['accuracy', 'helpfulness', 'clarity', 'discord_appropriate', 'context_aware', 'overall']:
                    try:
                        results[key] = float(value)
                    except ValueError:
                        results[key] = 0.0
                elif key == 'feedback':
                    results[key] = value
        
        return results
    
    @pytest.mark.skip(reason="LLM test - run only when needed")
    def test_bytes_balance_question(self):
        """Test response to bytes balance question."""
        question = "How do I check my bytes balance?"
        response, _ = self.agent.generate_response(question)  # Unpack tuple
        
        evaluation = self.evaluate_response_quality(question, response)
        
        # Assert minimum quality thresholds
        assert evaluation.get('accuracy', 0) >= 7, f"Accuracy too low: {evaluation.get('feedback', '')}"
        assert evaluation.get('helpfulness', 0) >= 7, f"Helpfulness too low: {evaluation.get('feedback', '')}"
        assert evaluation.get('overall', 0) >= 7, f"Overall quality too low: {evaluation.get('feedback', '')}"
    
    @pytest.mark.skip(reason="LLM test - run only when needed")
    def test_squad_joining_question(self):
        """Test response to squad joining question."""
        question = "How do I join a squad? What does it cost?"
        response, _ = self.agent.generate_response(question)
        
        evaluation = self.evaluate_response_quality(question, response)
        
        assert evaluation.get('accuracy', 0) >= 7, f"Accuracy too low: {evaluation.get('feedback', '')}"
        assert evaluation.get('helpfulness', 0) >= 7, f"Helpfulness too low: {evaluation.get('feedback', '')}"
    
    @pytest.mark.skip(reason="LLM test - run only when needed")
    def test_context_aware_response(self):
        """Test context-aware response generation."""
        context_messages = [
            DiscordMessage(
                author="NewUser",
                timestamp=datetime.now(timezone.utc),
                content="I just joined this server"
            ),
            DiscordMessage(
                author="Helper",
                timestamp=datetime.now(timezone.utc),
                content="Welcome! The bot here has some cool features"
            )
        ]
        
        question = "What can the bot do?"
        response, _ = self.agent.generate_response(question, context_messages)
        
        context_str = "NewUser: I just joined this server\nHelper: Welcome! The bot here has some cool features"
        evaluation = self.evaluate_response_quality(question, response, context_str)
        
        assert evaluation.get('context_aware', 0) >= 6, f"Not context-aware enough: {evaluation.get('feedback', '')}"
        assert evaluation.get('helpfulness', 0) >= 7, f"Not helpful enough: {evaluation.get('feedback', '')}"
    
    @pytest.mark.skip(reason="LLM test - run only when needed")
    def test_error_troubleshooting_response(self):
        """Test response to error troubleshooting question."""
        question = "I'm getting a cooldown error when trying to send bytes. What's wrong?"
        response, _ = self.agent.generate_response(question)
        
        evaluation = self.evaluate_response_quality(question, response)
        
        assert evaluation.get('accuracy', 0) >= 7, f"Troubleshooting accuracy too low: {evaluation.get('feedback', '')}"
        assert evaluation.get('helpfulness', 0) >= 8, f"Troubleshooting not helpful enough: {evaluation.get('feedback', '')}"
    
    @pytest.mark.skip(reason="LLM test - run only when needed")
    def test_multiple_command_question(self):
        """Test response to question about multiple commands."""
        question = "What's the difference between /bytes history and /bytes leaderboard?"
        response, _ = self.agent.generate_response(question)
        
        evaluation = self.evaluate_response_quality(question, response)
        
        assert evaluation.get('accuracy', 0) >= 7, f"Comparison accuracy too low: {evaluation.get('feedback', '')}"
        assert evaluation.get('clarity', 0) >= 7, f"Comparison not clear enough: {evaluation.get('feedback', '')}"


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
        ctx.user = Mock(id=67890, username="TestUser")
        ctx.channel_id = 11111
        ctx.options = Mock(question="How do I send bytes?")
        ctx.respond = AsyncMock()
        return ctx
    
    @pytest.mark.asyncio
    async def test_help_command_basic_flow(self, mock_context):
        """Test the basic flow of the help command."""
        # Import here to avoid circular imports in testing
        from smarter_dev.bot.plugins.help import help_command, gather_message_context
        
        with patch('smarter_dev.bot.plugins.help.gather_message_context') as mock_gather:
            with patch('smarter_dev.bot.plugins.help.generate_help_response') as mock_generate:
                mock_gather.return_value = []
                mock_generate.return_value = "Test response"
                
                await help_command(mock_context)
                
                mock_generate.assert_called_once()
                mock_context.respond.assert_called_once_with(
                    "Test response", 
                    flags=pytest.approx(64)  # EPHEMERAL flag
                )
    
    @pytest.mark.asyncio
    async def test_rate_limiting_integration(self):
        """Test that rate limiting is properly integrated."""
        from smarter_dev.bot.plugins.help import generate_help_response
        
        user_id = "test_user_123"
        
        # Fill up the rate limit
        for _ in range(10):
            rate_limiter.record_request(user_id, 100)  # Add token count
        
        # Next request should be rate limited
        response = await generate_help_response(user_id, "test question")
        
        assert "rate limit" in response.lower()
        assert "10 questions per 30 minutes" in response
    
    def test_global_rate_limiter_persistence(self):
        """Test that the global rate limiter persists state."""
        from smarter_dev.bot.agent import rate_limiter as imported_limiter
        
        user_id = "persistence_test"
        imported_limiter.record_request(user_id, 150)  # Add token count
        
        # Should have 9 remaining requests
        assert imported_limiter.get_user_remaining_requests(user_id) == 9