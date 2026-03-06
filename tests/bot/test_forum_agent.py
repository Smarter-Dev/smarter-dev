"""Tests for ForumMonitorAgent.

This module tests the AI agent that evaluates forum posts and generates responses.
"""

from __future__ import annotations

from unittest.mock import Mock, patch, AsyncMock, MagicMock
from contextlib import contextmanager

import pytest


class MockDiscordMessage:
    """Mock Discord message for testing."""

    def __init__(self, author: str = "TestUser", content: str = "Test content"):
        self.author = author
        self.content = content


@contextmanager
def mock_dspy_for_forum_agent():
    """Context manager that mocks dspy at the module level for ForumMonitorAgent imports."""
    with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
        # Mock ChainOfThought to return a callable Mock
        mock_agent = Mock()
        mock_dspy.ChainOfThought.return_value = mock_agent

        # Mock dspy.context as a context manager
        mock_ctx = MagicMock()
        mock_dspy.context.return_value = mock_ctx

        yield mock_dspy, mock_agent


class TestForumMonitorAgent:
    """Test ForumMonitorAgent functionality."""

    @pytest.fixture
    def mock_dspy_agent(self):
        """Mock DSPy agent for testing."""
        agent = Mock()
        agent.return_value = Mock(
            decision="Should respond to this question",
            confidence=0.85,
            response="Here's a helpful response to your question."
        )
        return agent

    @pytest.fixture
    def forum_agent(self, mock_dspy_agent):
        """Create ForumMonitorAgent instance with mocked DSPy."""
        with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
            mock_dspy.ChainOfThought.return_value = mock_dspy_agent
            mock_dspy.Signature = type('Signature', (), {})
            mock_dspy.InputField = Mock()
            mock_dspy.OutputField = Mock()
            from smarter_dev.bot.agents.forum_agent import ForumMonitorAgent
            agent = ForumMonitorAgent()
            # Replace the internal agent with our mock for direct testing
            agent._agent = mock_dspy_agent
            return agent

    async def test_forum_agent_initialization(self, forum_agent):
        """Test that ForumMonitorAgent initializes correctly."""
        assert forum_agent is not None
        assert hasattr(forum_agent, '_agent')

    async def test_evaluate_post_basic_functionality(self, forum_agent, mock_dspy_agent):
        """Test basic post evaluation functionality."""
        # Mock the DSPy result
        mock_result = Mock()
        mock_result.decision = "Should respond - this is a coding question that needs help"
        mock_result.confidence = 0.85
        mock_result.response = "Here's how you can solve this coding problem..."

        # Mock asyncify to return an async function that returns our mock result
        async def mock_async_agent(**kwargs):
            return mock_result

        system_prompt = "You are a helpful coding assistant. Respond to programming questions."
        post_title = "How to fix Python import error?"
        post_content = "I'm getting ImportError when trying to import my module"
        author_display_name = "NewDeveloper"
        post_tags = ["python", "error"]
        attachment_names = ["error_screenshot.png"]

        with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)
            mock_dspy.asyncify.return_value = mock_async_agent

            with patch.object(forum_agent, '_extract_token_usage', return_value=150):
                decision, confidence, response, tokens = await forum_agent.evaluate_post(
                    system_prompt, post_title, post_content, author_display_name, post_tags, attachment_names
                )

        assert "Should respond" in decision
        assert confidence == 0.85
        assert "solve this coding problem" in response
        assert tokens == 150

    async def test_evaluate_post_no_response_needed(self, forum_agent, mock_dspy_agent):
        """Test post evaluation when no response is needed."""
        mock_result = Mock()
        mock_result.decision = "No response needed - this is spam"
        mock_result.confidence = 0.2
        mock_result.response = ""

        async def mock_async_agent(**kwargs):
            return mock_result

        with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)
            mock_dspy.asyncify.return_value = mock_async_agent

            with patch.object(forum_agent, '_extract_token_usage', return_value=75):
                decision, confidence, response, tokens = await forum_agent.evaluate_post(
                    "Ignore spam posts and only respond to genuine questions.",
                    "BUY CRYPTO NOW!!!",
                    "Click this link to get rich quick!",
                    "SpamBot",
                    [],
                    []
                )

        assert "No response needed" in decision
        assert confidence == 0.2
        assert response == ""
        assert tokens == 75

    async def test_evaluate_post_with_context_formatting(self, forum_agent, mock_dspy_agent):
        """Test that post context is properly formatted for the AI."""
        mock_result = Mock()
        mock_result.decision = "Should respond"
        mock_result.confidence = 0.9
        mock_result.response = "Great question about React!"

        captured_kwargs = {}

        async def mock_async_agent(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_result

        with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)
            mock_dspy.asyncify.return_value = mock_async_agent

            with patch.object(forum_agent, '_extract_token_usage', return_value=200):
                await forum_agent.evaluate_post(
                    "Help with React questions",
                    "React useState hook question",
                    "How do I update state properly in React?",
                    "ReactLearner",
                    ["react", "hooks", "javascript"],
                    ["component.jsx", "error.png"]
                )

        # Verify the agent was called with properly formatted context
        assert "system_prompt" in captured_kwargs
        assert captured_kwargs["system_prompt"] == "Help with React questions"

        assert "post_context" in captured_kwargs
        post_context = captured_kwargs["post_context"]
        assert "React useState hook question" in post_context
        assert "ReactLearner" in post_context
        assert "react" in post_context
        assert "hooks" in post_context
        assert "component.jsx" in post_context

    async def test_evaluate_post_token_usage_extraction(self, forum_agent, mock_dspy_agent):
        """Test token usage extraction from results."""
        mock_result = Mock()
        mock_result.decision = "Should respond"
        mock_result.confidence = 0.8
        mock_result.response = "Helpful response"

        async def mock_async_agent(**kwargs):
            return mock_result

        with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)
            mock_dspy.asyncify.return_value = mock_async_agent

            with patch.object(forum_agent, '_extract_token_usage', return_value=175):
                decision, confidence, response, tokens = await forum_agent.evaluate_post(
                    "Test prompt", "Title", "Content", "Author", [], []
                )

        assert tokens == 175

    async def test_evaluate_post_no_token_usage_info(self, forum_agent, mock_dspy_agent):
        """Test handling when no token usage information is available (falls back to estimation)."""
        mock_result = Mock()
        mock_result.decision = "Should respond"
        mock_result.confidence = 0.8
        mock_result.response = "Helpful response"

        async def mock_async_agent(**kwargs):
            return mock_result

        with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)
            mock_dspy.asyncify.return_value = mock_async_agent

            # Return 0 to trigger fallback estimation
            with patch.object(forum_agent, '_extract_token_usage', return_value=0):
                decision, confidence, response, tokens = await forum_agent.evaluate_post(
                    "Test prompt", "Title", "Content", "Author", [], []
                )

        # When _extract_token_usage returns 0, the code estimates from text length
        assert tokens > 0  # Should have a fallback estimation

    async def test_evaluate_post_error_handling(self, forum_agent, mock_dspy_agent):
        """Test error handling during post evaluation."""
        async def mock_async_agent_error(**kwargs):
            raise Exception("AI service unavailable")

        with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)
            mock_dspy.asyncify.return_value = mock_async_agent_error

            with pytest.raises(Exception) as exc_info:
                await forum_agent.evaluate_post(
                    "Test prompt", "Title", "Content", "Author", [], []
                )

            assert "AI service unavailable" in str(exc_info.value)

    async def test_evaluate_post_empty_inputs(self, forum_agent, mock_dspy_agent):
        """Test evaluation with empty or minimal inputs."""
        mock_result = Mock()
        mock_result.decision = "Need more information"
        mock_result.confidence = 0.1
        mock_result.response = ""

        async def mock_async_agent(**kwargs):
            return mock_result

        with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)
            mock_dspy.asyncify.return_value = mock_async_agent

            with patch.object(forum_agent, '_extract_token_usage', return_value=20):
                decision, confidence, response, tokens = await forum_agent.evaluate_post(
                    "Basic prompt", "", "", "", [], []
                )

        assert "Need more information" in decision
        assert confidence == 0.1
        assert response == ""
        assert tokens == 20

    async def test_evaluate_post_long_content(self, forum_agent, mock_dspy_agent):
        """Test evaluation with very long post content."""
        mock_result = Mock()
        mock_result.decision = "Should respond with detailed analysis"
        mock_result.confidence = 0.95
        mock_result.response = "This is a comprehensive response to your detailed question."

        async def mock_async_agent(**kwargs):
            return mock_result

        # Create very long content
        long_content = "This is a very detailed question. " * 100
        long_tags = ["tag1", "tag2", "tag3", "tag4", "tag5"]
        many_attachments = [f"file{i}.txt" for i in range(10)]

        with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
            mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
            mock_dspy.context.return_value.__exit__ = Mock(return_value=None)
            mock_dspy.asyncify.return_value = mock_async_agent

            with patch.object(forum_agent, '_extract_token_usage', return_value=500):
                decision, confidence, response, tokens = await forum_agent.evaluate_post(
                    "Detailed analysis prompt",
                    "Complex technical question with many details",
                    long_content,
                    "DetailedAsker",
                    long_tags,
                    many_attachments
                )

        assert confidence == 0.95
        assert tokens == 500
        assert "comprehensive response" in response

    async def test_dspy_configuration(self):
        """Test that DSPy is properly configured at module level."""
        # The module uses get_llm_model("medium") at import time, not dspy.configure/dspy.LM directly
        # Verify the module-level configuration by checking the agent's LM setting
        from smarter_dev.bot.agents.forum_agent import FORUM_AGENT_LM
        assert FORUM_AGENT_LM is not None

    async def test_signature_validation(self, forum_agent):
        """Test that the DSPy signature has required fields."""
        # Import the signature class
        from smarter_dev.bot.agents.forum_agent import ForumMonitorSignature

        # Verify expected fields exist in the signature
        assert hasattr(ForumMonitorSignature, 'model_fields') or hasattr(ForumMonitorSignature, '__annotations__')

    async def test_confidence_score_bounds(self, forum_agent, mock_dspy_agent):
        """Test that confidence scores are properly bounded between 0 and 1."""
        test_cases = [
            (1.5, 1.0),   # Above 1.0 should be clamped to 1.0
            (-0.2, 0.0),  # Below 0.0 should be clamped to 0.0
            (0.5, 0.5),   # Normal values should pass through
            (0.0, 0.0),   # Boundary values should work
            (1.0, 1.0),   # Boundary values should work
        ]

        for input_confidence, expected_confidence in test_cases:
            mock_result = Mock()
            mock_result.decision = "Test decision"
            mock_result.confidence = input_confidence
            mock_result.response = "Test response"

            async def mock_async_agent(**kwargs):
                return mock_result

            with patch('smarter_dev.bot.agents.forum_agent.dspy') as mock_dspy:
                mock_dspy.context.return_value.__enter__ = Mock(return_value=None)
                mock_dspy.context.return_value.__exit__ = Mock(return_value=None)
                mock_dspy.asyncify.return_value = mock_async_agent

                with patch.object(forum_agent, '_extract_token_usage', return_value=50):
                    decision, confidence, response, tokens = await forum_agent.evaluate_post(
                        "Test", "Test", "Test", "Test", [], []
                    )

            assert 0.0 <= confidence <= 1.0, f"Confidence {confidence} out of bounds for input {input_confidence}"
            assert confidence == expected_confidence, f"Expected {expected_confidence} but got {confidence} for input {input_confidence}"
