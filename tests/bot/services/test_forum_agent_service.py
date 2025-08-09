"""Tests for ForumAgentService.

This module provides comprehensive tests for the ForumAgentService including
agent loading, post evaluation, response generation, and token tracking.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from smarter_dev.bot.services.exceptions import (
    APIError,
    ResourceNotFoundError,
    ServiceError,
    ValidationError
)


class MockForumPost:
    """Mock forum post for testing."""
    
    def __init__(self, title: str = "Test Post", content: str = "Test content", 
                 author_display_name: str = "TestUser", tags: list = None, 
                 attachments: list = None):
        self.title = title
        self.content = content
        self.author_display_name = author_display_name
        self.tags = tags or []
        self.attachments = attachments or []
        self.channel_id = "123456789"
        self.thread_id = "987654321"
        self.guild_id = "555555555"


class MockForumAgent:
    """Mock forum agent for testing."""
    
    def __init__(self, id=None, name="Test Agent", system_prompt="Test prompt", 
                 monitored_forums=None, response_threshold=0.7, 
                 max_responses_per_hour=5, is_active=True):
        self.id = id or uuid4()
        self.name = name
        self.system_prompt = system_prompt
        self.monitored_forums = monitored_forums or ["123456789"]
        self.response_threshold = response_threshold
        self.max_responses_per_hour = max_responses_per_hour
        self.is_active = is_active
        self.guild_id = "555555555"


class TestForumAgentService:
    """Test ForumAgentService functionality."""

    @pytest.fixture
    def mock_api_client(self):
        """Create a mock API client."""
        client = AsyncMock()
        client.get = AsyncMock()
        client.post = AsyncMock()
        return client

    @pytest.fixture
    def mock_cache_manager(self):
        """Create a mock cache manager."""
        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()
        return cache

    @pytest.fixture
    def forum_agent_service(self, mock_api_client, mock_cache_manager):
        """Create ForumAgentService instance with mocked dependencies."""
        # Import here to avoid issues during collection
        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        return ForumAgentService(mock_api_client, mock_cache_manager)

    async def test_forum_agent_service_initialization(self, forum_agent_service):
        """Test that ForumAgentService initializes correctly."""
        assert forum_agent_service is not None
        assert hasattr(forum_agent_service, '_api_client')
        assert hasattr(forum_agent_service, '_cache_manager')

    async def test_load_guild_agents_success(self, forum_agent_service, mock_api_client):
        """Test successfully loading active agents for a guild."""
        guild_id = "555555555"
        mock_agents = [
            {
                "id": str(uuid4()),
                "name": "Agent 1",
                "system_prompt": "Test prompt 1",
                "monitored_forums": ["123456789"],
                "response_threshold": 0.7,
                "max_responses_per_hour": 5,
                "is_active": True
            },
            {
                "id": str(uuid4()),
                "name": "Agent 2",
                "system_prompt": "Test prompt 2",
                "monitored_forums": ["987654321"],
                "response_threshold": 0.8,
                "max_responses_per_hour": 3,
                "is_active": True
            }
        ]
        
        mock_api_client.get.return_value = mock_agents
        
        agents = await forum_agent_service.load_guild_agents(guild_id)
        
        assert len(agents) == 2
        assert agents[0]["name"] == "Agent 1"
        assert agents[1]["name"] == "Agent 2"
        mock_api_client.get.assert_called_once_with(f"/guilds/{guild_id}/forum-agents")

    async def test_load_guild_agents_empty_result(self, forum_agent_service, mock_api_client):
        """Test loading agents when none exist for guild."""
        guild_id = "555555555"
        mock_api_client.get.return_value = []
        
        agents = await forum_agent_service.load_guild_agents(guild_id)
        
        assert agents == []
        mock_api_client.get.assert_called_once_with(f"/guilds/{guild_id}/forum-agents")

    async def test_load_guild_agents_api_error(self, forum_agent_service, mock_api_client):
        """Test handling API errors when loading agents."""
        guild_id = "555555555"
        mock_api_client.get.side_effect = APIError("API failed")
        
        with pytest.raises(APIError):
            await forum_agent_service.load_guild_agents(guild_id)

    async def test_should_agent_monitor_forum_true(self, forum_agent_service):
        """Test agent should monitor forum when channel is in monitored list."""
        agent = {
            "id": str(uuid4()),
            "name": "Test Agent",
            "system_prompt": "Test prompt",
            "monitored_forums": ["123456789", "987654321"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "is_active": True,
            "guild_id": "555555555"
        }
        
        result = forum_agent_service.should_agent_monitor_forum(agent, "123456789")
        
        assert result is True

    async def test_should_agent_monitor_forum_false(self, forum_agent_service):
        """Test agent should not monitor forum when channel not in list."""
        agent = {
            "id": str(uuid4()),
            "name": "Test Agent",
            "system_prompt": "Test prompt",
            "monitored_forums": ["111111111", "222222222"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "is_active": True,
            "guild_id": "555555555"
        }
        
        result = forum_agent_service.should_agent_monitor_forum(agent, "123456789")
        
        assert result is False

    async def test_should_agent_monitor_forum_inactive(self, forum_agent_service):
        """Test inactive agent should not monitor any forum."""
        agent = {
            "id": str(uuid4()),
            "name": "Test Agent",
            "system_prompt": "Test prompt",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "is_active": False,
            "guild_id": "555555555"
        }
        
        result = forum_agent_service.should_agent_monitor_forum(agent, "123456789")
        
        assert result is False

    @patch('smarter_dev.bot.services.forum_agent_service.ForumMonitorAgent')
    async def test_evaluate_post_high_confidence_should_respond(self, mock_agent_class, forum_agent_service):
        """Test post evaluation with high confidence should trigger response."""
        mock_agent_instance = AsyncMock()
        mock_agent_instance.evaluate_post.return_value = ("Should respond", 0.9, "My helpful response", 150)
        mock_agent_class.return_value = mock_agent_instance
        
        agent = {
            "id": str(uuid4()),
            "name": "Test Agent",
            "system_prompt": "Test prompt",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "is_active": True,
            "guild_id": "555555555"
        }
        post = MockForumPost()
        
        decision, confidence, response_content, tokens_used = await forum_agent_service.evaluate_post(agent, post)
        
        assert decision == "Should respond"
        assert confidence == 0.9
        assert response_content == "My helpful response"
        assert tokens_used == 150
        assert confidence >= agent["response_threshold"]

    @patch('smarter_dev.bot.services.forum_agent_service.ForumMonitorAgent')
    async def test_evaluate_post_low_confidence_should_not_respond(self, mock_agent_class, forum_agent_service):
        """Test post evaluation with low confidence should not trigger response."""
        mock_agent_instance = AsyncMock()
        mock_agent_instance.evaluate_post.return_value = ("Not worth responding", 0.3, "", 120)
        mock_agent_class.return_value = mock_agent_instance
        
        agent = {
            "id": str(uuid4()),
            "name": "Test Agent",
            "system_prompt": "Test prompt",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "is_active": True,
            "guild_id": "555555555"
        }
        post = MockForumPost()
        
        decision, confidence, response_content, tokens_used = await forum_agent_service.evaluate_post(agent, post)
        
        assert decision == "Not worth responding"
        assert confidence == 0.3
        assert response_content == ""
        assert tokens_used == 120
        assert confidence < agent["response_threshold"]

    @patch('smarter_dev.bot.services.forum_agent_service.ForumMonitorAgent')
    async def test_evaluate_post_agent_error_handling(self, mock_agent_class, forum_agent_service):
        """Test handling of agent evaluation errors."""
        mock_agent_instance = AsyncMock()
        mock_agent_instance.evaluate_post.side_effect = Exception("AI service failed")
        mock_agent_class.return_value = mock_agent_instance
        
        agent = {
            "id": str(uuid4()),
            "name": "Test Agent",
            "system_prompt": "Test prompt",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "is_active": True,
            "guild_id": "555555555"
        }
        post = MockForumPost()
        
        with pytest.raises(ServiceError):
            await forum_agent_service.evaluate_post(agent, post)

    async def test_record_response_success(self, forum_agent_service, mock_api_client):
        """Test successfully recording agent response."""
        agent = {
            "id": str(uuid4()),
            "name": "Test Agent",
            "system_prompt": "Test prompt",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "is_active": True,
            "guild_id": "555555555"
        }
        post = MockForumPost()
        decision_reason = "High confidence response warranted"
        confidence_score = 0.85
        response_content = "Here's my helpful response"
        tokens_used = 150
        response_time_ms = 1200
        responded = True
        
        mock_api_client.post.return_value = {"id": str(uuid4())}
        
        response_id = await forum_agent_service.record_response(
            agent, post, decision_reason, confidence_score, 
            response_content, tokens_used, response_time_ms, responded
        )
        
        assert response_id is not None
        mock_api_client.post.assert_called_once()
        
        # Verify the call was made with correct data
        call_args = mock_api_client.post.call_args
        assert call_args[0][0] == f"/guilds/{post.guild_id}/forum-agents/{agent['id']}/responses"
        
        posted_data = call_args[1]['json']
        assert posted_data['channel_id'] == post.channel_id
        assert posted_data['thread_id'] == post.thread_id
        assert posted_data['post_title'] == post.title
        assert posted_data['post_content'] == post.content
        assert posted_data['author_display_name'] == post.author_display_name
        assert posted_data['decision_reason'] == decision_reason
        assert posted_data['confidence_score'] == confidence_score
        assert posted_data['response_content'] == response_content
        assert posted_data['tokens_used'] == tokens_used
        assert posted_data['response_time_ms'] == response_time_ms
        assert posted_data['responded'] == responded

    async def test_record_response_api_error(self, forum_agent_service, mock_api_client):
        """Test handling API errors when recording response."""
        agent = {
            "id": str(uuid4()),
            "name": "Test Agent",
            "system_prompt": "Test prompt",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "is_active": True,
            "guild_id": "555555555"
        }
        post = MockForumPost()
        
        mock_api_client.post.side_effect = APIError("Failed to record response")
        
        with pytest.raises(APIError):
            await forum_agent_service.record_response(
                agent, post, "Test reason", 0.8, "Test response", 100, 1000, True
            )

    async def test_check_rate_limit_within_limit(self, forum_agent_service, mock_api_client):
        """Test rate limit check when agent is within limits."""
        agent = {
            "id": str(uuid4()),
            "name": "Test Agent",
            "system_prompt": "Test prompt",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "is_active": True,
            "guild_id": "555555555"
        }
        
        # Mock API to return 3 responses in the last hour
        mock_api_client.get.return_value = {"count": 3}
        
        within_limit = await forum_agent_service.check_rate_limit(agent)
        
        assert within_limit is True
        mock_api_client.get.assert_called_once_with(
            f"/guilds/{agent['guild_id']}/forum-agents/{agent['id']}/responses/count",
            params={'hours': 1}
        )

    async def test_check_rate_limit_exceeded(self, forum_agent_service, mock_api_client):
        """Test rate limit check when agent has exceeded limits."""
        agent = {
            "id": str(uuid4()),
            "name": "Test Agent",
            "system_prompt": "Test prompt",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "is_active": True,
            "guild_id": "555555555"
        }
        
        # Mock API to return 5 responses in the last hour (at limit)
        mock_api_client.get.return_value = {"count": 5}
        
        within_limit = await forum_agent_service.check_rate_limit(agent)
        
        assert within_limit is False
        mock_api_client.get.assert_called_once()

    async def test_get_agent_analytics_success(self, forum_agent_service, mock_api_client):
        """Test successfully retrieving agent analytics."""
        agent_id = uuid4()
        
        mock_analytics = {
            "total_evaluations": 50,
            "total_responses": 15,
            "average_confidence": 0.75,
            "total_tokens_used": 7500,
            "response_rate": 0.30,
            "hourly_activity": [2, 3, 1, 4, 2]
        }
        
        mock_api_client.get.return_value = mock_analytics
        
        analytics = await forum_agent_service.get_agent_analytics(str(agent_id))
        
        assert analytics == mock_analytics
        mock_api_client.get.assert_called_once_with(f"/forum-agents/{agent_id}/analytics")

    async def test_process_forum_post_complete_workflow(self, forum_agent_service, mock_api_client):
        """Test complete forum post processing workflow."""
        guild_id = "555555555"
        post = MockForumPost()
        
        # Mock agents
        mock_agents = [
            {
                "id": str(uuid4()),
                "name": "Agent 1",
                "system_prompt": "Help with coding questions",
                "monitored_forums": ["123456789"],
                "response_threshold": 0.7,
                "max_responses_per_hour": 5,
                "is_active": True,
                "guild_id": guild_id
            }
        ]
        
        # Mock rate limit check
        mock_api_client.get.side_effect = [
            mock_agents,  # load_guild_agents call
            {"count": 2}  # check_rate_limit call
        ]
        
        # Mock response recording
        mock_api_client.post.return_value = {"id": str(uuid4())}
        
        with patch('smarter_dev.bot.services.forum_agent_service.ForumMonitorAgent') as mock_agent_class:
            mock_agent_instance = AsyncMock()
            mock_agent_instance.evaluate_post.return_value = ("Should respond", 0.85, "Helpful response", 150)
            mock_agent_class.return_value = mock_agent_instance
            
            responses = await forum_agent_service.process_forum_post(guild_id, post)
            
            assert len(responses) == 1
            assert responses[0]["agent_name"] == "Agent 1"
            assert responses[0]["should_respond"] is True
            assert responses[0]["confidence"] == 0.85
            assert responses[0]["response_content"] == "Helpful response"

    async def test_process_forum_post_no_agents(self, forum_agent_service, mock_api_client):
        """Test processing post when no agents exist for guild."""
        guild_id = "555555555"
        post = MockForumPost()
        
        mock_api_client.get.return_value = []  # No agents
        
        responses = await forum_agent_service.process_forum_post(guild_id, post)
        
        assert responses == []

    async def test_process_forum_post_agent_rate_limited(self, forum_agent_service, mock_api_client):
        """Test processing post when agent is rate limited."""
        guild_id = "555555555"
        post = MockForumPost()
        
        mock_agents = [
            {
                "id": str(uuid4()),
                "name": "Rate Limited Agent",
                "system_prompt": "Test prompt",
                "monitored_forums": ["123456789"],
                "response_threshold": 0.7,
                "max_responses_per_hour": 5,
                "is_active": True,
                "guild_id": guild_id
            }
        ]
        
        mock_api_client.get.side_effect = [
            mock_agents,  # load_guild_agents call
            {"count": 5}  # check_rate_limit call - at limit
        ]
        
        responses = await forum_agent_service.process_forum_post(guild_id, post)
        
        assert len(responses) == 1
        assert responses[0]["should_respond"] is False
        assert "rate limit" in responses[0]["decision_reason"].lower()

    async def test_health_check_success(self, forum_agent_service):
        """Test service health check when healthy."""
        health = await forum_agent_service.health_check()
        
        assert health.is_healthy is True
        assert health.service_name == "ForumAgentService"
        assert "agents" in health.details

    async def test_get_service_stats(self, forum_agent_service):
        """Test retrieving service statistics."""
        stats = forum_agent_service.get_service_stats()
        
        assert "evaluations_processed" in stats
        assert "responses_generated" in stats
        assert "total_tokens_used" in stats
        assert "average_evaluation_time" in stats
        assert "service_name" in stats
        assert stats["service_name"] == "ForumAgentService"