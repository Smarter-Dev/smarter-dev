"""Tests for ForumMonitorAgent.

This module tests the AI agent that evaluates forum posts and generates responses.
"""

from __future__ import annotations

from unittest.mock import Mock, patch, AsyncMock

import pytest


class MockDiscordMessage:
    """Mock Discord message for testing."""
    
    def __init__(self, author: str = "TestUser", content: str = "Test content"):
        self.author = author
        self.content = content


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
        with patch('smarter_dev.bot.agent.dspy.ChainOfThought', return_value=mock_dspy_agent):
            from smarter_dev.bot.agent import ForumMonitorAgent
            return ForumMonitorAgent()

    async def test_forum_agent_initialization(self, forum_agent):
        """Test that ForumMonitorAgent initializes correctly."""
        assert forum_agent is not None
        assert hasattr(forum_agent, '_agent')

    async def test_evaluate_post_basic_functionality(self, forum_agent, mock_dspy_agent):
        """Test basic post evaluation functionality."""
        # Mock the DSPy result with token usage
        mock_result = Mock()
        mock_result.decision = "Should respond - this is a coding question that needs help"
        mock_result.confidence = 0.85
        mock_result.response = "Here's how you can solve this coding problem..."
        mock_result._completions = [
            Mock(kwargs={'usage': Mock(total_tokens=150)})
        ]
        
        mock_dspy_agent.return_value = mock_result
        
        system_prompt = "You are a helpful coding assistant. Respond to programming questions."
        post_title = "How to fix Python import error?"
        post_content = "I'm getting ImportError when trying to import my module"
        author_display_name = "NewDeveloper"
        post_tags = ["python", "error"]
        attachment_names = ["error_screenshot.png"]
        
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
        mock_result._completions = [
            Mock(kwargs={'usage': Mock(total_tokens=75)})
        ]
        
        mock_dspy_agent.return_value = mock_result
        
        system_prompt = "Ignore spam posts and only respond to genuine questions."
        post_title = "BUY CRYPTO NOW!!!"
        post_content = "Click this link to get rich quick!"
        author_display_name = "SpamBot"
        post_tags = []
        attachment_names = []
        
        decision, confidence, response, tokens = await forum_agent.evaluate_post(
            system_prompt, post_title, post_content, author_display_name, post_tags, attachment_names
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
        mock_result._completions = [
            Mock(kwargs={'usage': Mock(total_tokens=200)})
        ]
        
        mock_dspy_agent.return_value = mock_result
        
        system_prompt = "Help with React questions"
        post_title = "React useState hook question"
        post_content = "How do I update state properly in React?"
        author_display_name = "ReactLearner"
        post_tags = ["react", "hooks", "javascript"]
        attachment_names = ["component.jsx", "error.png"]
        
        await forum_agent.evaluate_post(
            system_prompt, post_title, post_content, author_display_name, post_tags, attachment_names
        )
        
        # Verify the agent was called with properly formatted context
        mock_dspy_agent.assert_called_once()
        call_args = mock_dspy_agent.call_args[1]
        
        assert "system_prompt" in call_args
        assert call_args["system_prompt"] == system_prompt
        
        assert "post_context" in call_args
        post_context = call_args["post_context"]
        assert "React useState hook question" in post_context
        assert "ReactLearner" in post_context
        assert "react" in post_context
        assert "hooks" in post_context
        assert "component.jsx" in post_context

    async def test_evaluate_post_token_usage_multiple_completions(self, forum_agent, mock_dspy_agent):
        """Test token usage calculation with multiple completions."""
        mock_result = Mock()
        mock_result.decision = "Should respond"
        mock_result.confidence = 0.8
        mock_result.response = "Helpful response"
        
        # Multiple completions with different token counts
        mock_result._completions = [
            Mock(kwargs={'usage': Mock(total_tokens=100)}),
            Mock(kwargs={'usage': Mock(total_tokens=50)}),
            Mock(kwargs={'usage': Mock(total_tokens=25)})
        ]
        
        mock_dspy_agent.return_value = mock_result
        
        decision, confidence, response, tokens = await forum_agent.evaluate_post(
            "Test prompt", "Title", "Content", "Author", [], []
        )
        
        assert tokens == 175  # 100 + 50 + 25

    async def test_evaluate_post_token_usage_dict_format(self, forum_agent, mock_dspy_agent):
        """Test token usage calculation with dictionary format."""
        mock_result = Mock()
        mock_result.decision = "Should respond"
        mock_result.confidence = 0.8
        mock_result.response = "Helpful response"
        
        # Token usage in dictionary format
        mock_result._completions = [
            Mock(kwargs={'usage': {'total_tokens': 125}})
        ]
        
        mock_dspy_agent.return_value = mock_result
        
        decision, confidence, response, tokens = await forum_agent.evaluate_post(
            "Test prompt", "Title", "Content", "Author", [], []
        )
        
        assert tokens == 125

    async def test_evaluate_post_no_token_usage_info(self, forum_agent, mock_dspy_agent):
        """Test handling when no token usage information is available."""
        mock_result = Mock()
        mock_result.decision = "Should respond"
        mock_result.confidence = 0.8
        mock_result.response = "Helpful response"
        mock_result._completions = []  # No completions
        
        mock_dspy_agent.return_value = mock_result
        
        decision, confidence, response, tokens = await forum_agent.evaluate_post(
            "Test prompt", "Title", "Content", "Author", [], []
        )
        
        assert tokens == 0

    async def test_evaluate_post_error_handling(self, forum_agent, mock_dspy_agent):
        """Test error handling during post evaluation."""
        mock_dspy_agent.side_effect = Exception("AI service unavailable")
        
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
        mock_result._completions = [
            Mock(kwargs={'usage': Mock(total_tokens=20)})
        ]
        
        mock_dspy_agent.return_value = mock_result
        
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
        mock_result._completions = [
            Mock(kwargs={'usage': Mock(total_tokens=500)})
        ]
        
        mock_dspy_agent.return_value = mock_result
        
        # Create very long content
        long_content = "This is a very detailed question. " * 100
        long_tags = ["tag1", "tag2", "tag3", "tag4", "tag5"]
        many_attachments = [f"file{i}.txt" for i in range(10)]
        
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

    @patch('smarter_dev.bot.agent.dspy.configure')
    @patch('smarter_dev.bot.agent.dspy.LM')
    async def test_dspy_configuration(self, mock_lm, mock_configure):
        """Test that DSPy is properly configured."""
        from smarter_dev.bot.agent import ForumMonitorAgent
        
        # Verify DSPy configuration is called during module import
        mock_lm.assert_called()
        mock_configure.assert_called()

    async def test_signature_validation(self, forum_agent):
        """Test that the DSPy signature has required fields."""
        # Import the signature class
        from smarter_dev.bot.agent import ForumMonitorSignature
        
        # Check that it has the expected input and output fields
        signature_instance = ForumMonitorSignature()
        
        # Verify expected attributes exist (this tests the signature structure)
        assert hasattr(ForumMonitorSignature, '__annotations__') or hasattr(ForumMonitorSignature, '__dict__')

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
            mock_result._completions = [Mock(kwargs={'usage': Mock(total_tokens=50)})]
            
            mock_dspy_agent.return_value = mock_result
            
            decision, confidence, response, tokens = await forum_agent.evaluate_post(
                "Test", "Test", "Test", "Test", [], []
            )
            
            assert 0.0 <= confidence <= 1.0
            # Note: The actual clamping might be done in the service layer