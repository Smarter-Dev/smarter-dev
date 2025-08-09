"""Integration tests for complete forum agent flow.

This module tests the entire forum agent system end-to-end:
1. Database operations (forum agents and responses)
2. Bot event handling (Discord forum posts)
3. AI evaluation and response generation
4. Admin interface management
5. API endpoints

These tests use real database connections and mock Discord/AI services.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
from datetime import datetime, timezone

from smarter_dev.web.models import ForumAgent, ForumAgentResponse
from smarter_dev.bot.client import ForumPostData


class TestForumAgentIntegrationFlow:
    """Test complete forum agent integration flow."""
    
    @pytest.mark.asyncio
    async def test_complete_forum_agent_workflow(self, real_db_session, admin_auth_headers, real_api_client):
        """Test complete workflow: Create agent -> Process post -> Record response."""
        guild_id = "123456789012345678"
        
        # Step 1: Create forum agent via API
        agent_data = {
            "name": "Integration Test Agent",
            "system_prompt": "You are a helpful programming assistant. Only respond to Python-related questions.",
            "monitored_forums": ["forum_channel_123", "forum_channel_456"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5
        }
        
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        agent = create_response.json()
        agent_id = agent["id"]
        
        # Step 2: Verify agent was created in database
        db_agent = await real_db_session.get(ForumAgent, agent_id)
        assert db_agent is not None
        assert db_agent.name == "Integration Test Agent"
        assert db_agent.guild_id == guild_id
        assert db_agent.is_active is True
        
        # Step 3: Simulate forum post processing
        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient
        from smarter_dev.shared.config import get_settings
        
        settings = get_settings()
        api_client = APIClient(base_url=settings.api_base_url, api_key=settings.bot_api_key)
        forum_service = ForumAgentService(api_client, None)
        
        # Create test forum post
        post_data = ForumPostData(
            title="How to fix Python import error?",
            content="I'm getting ImportError when trying to import my module. Help!",
            author_display_name="TestUser",
            tags=["python", "error", "help"],
            attachments=["error_screenshot.png"],
            channel_id="forum_channel_123",  # This channel is monitored by our agent
            thread_id="thread_987654321",
            guild_id=guild_id
        )
        
        # Mock the AI evaluation to return a response
        with patch('smarter_dev.bot.agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
            mock_evaluate.return_value = (
                "This is a Python-related question about import errors, which matches my expertise",
                0.85,  # High confidence
                "To fix Python import errors, check your PYTHONPATH and module structure...",
                300  # Tokens used
            )
            
            # Process the forum post
            responses = await forum_service.process_forum_post(guild_id, post_data)
        
        # Step 4: Verify response was generated
        assert len(responses) == 1
        response = responses[0]
        assert response["agent_id"] == agent_id
        assert response["agent_name"] == "Integration Test Agent"
        assert response["should_respond"] is True
        assert response["confidence"] == 0.85
        assert "PYTHONPATH" in response["response_content"]
        assert response["tokens_used"] == 300
        
        # Step 5: Verify response was recorded in database
        await real_db_session.refresh(db_agent)
        agent_responses = await real_db_session.execute(
            real_db_session.query(ForumAgentResponse).where(ForumAgentResponse.forum_agent_id == agent_id)
        )
        db_responses = list(agent_responses.scalars())
        
        assert len(db_responses) >= 1
        db_response = db_responses[0]
        assert db_response.post_title == "How to fix Python import error?"
        assert db_response.confidence_score == 0.85
        assert db_response.responded is True
        assert db_response.tokens_used == 300
        
        # Step 6: Get analytics via API
        analytics_response = await real_api_client.get(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}/analytics",
            headers=admin_auth_headers
        )
        assert analytics_response.status_code == 200
        analytics = analytics_response.json()
        
        assert analytics["agent"]["name"] == "Integration Test Agent"
        assert analytics["statistics"]["total_evaluations"] >= 1
        assert analytics["statistics"]["total_responses"] >= 1
        assert analytics["statistics"]["total_tokens_used"] >= 300
        
        # Step 7: Test agent toggle via API
        toggle_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}/toggle",
            headers=admin_auth_headers
        )
        assert toggle_response.status_code == 200
        toggled_agent = toggle_response.json()
        assert toggled_agent["is_active"] is False
        
        # Step 8: Verify disabled agent doesn't process posts
        with patch('smarter_dev.bot.agent.ForumMonitorAgent.evaluate_post') as mock_evaluate_2:
            mock_evaluate_2.return_value = ("Test", 0.9, "Test response", 100)
            
            # Process another post - should be ignored due to inactive agent
            post_data_2 = ForumPostData(
                title="Another Python question",
                content="How do I use decorators?",
                author_display_name="TestUser2",
                tags=["python"],
                attachments=[],
                channel_id="forum_channel_123",
                thread_id="thread_111111111",
                guild_id=guild_id
            )
            
            responses_2 = await forum_service.process_forum_post(guild_id, post_data_2)
        
        # Should get no responses because agent is inactive
        active_responses = [r for r in responses_2 if r.get("should_respond", False)]
        assert len(active_responses) == 0
        
        # Step 9: Clean up - delete agent
        delete_response = await real_api_client.delete(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
            headers=admin_auth_headers
        )
        assert delete_response.status_code == 204
        
        # Verify deletion
        get_response = await real_api_client.get(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
            headers=admin_auth_headers
        )
        assert get_response.status_code == 404

    @pytest.mark.asyncio
    async def test_forum_agent_rate_limiting_integration(self, real_db_session, admin_auth_headers, real_api_client):
        """Test rate limiting works correctly in integration."""
        guild_id = "123456789012345678"
        
        # Create agent with low rate limit
        agent_data = {
            "name": "Rate Limited Agent",
            "system_prompt": "Test rate limiting",
            "monitored_forums": ["forum_channel_123"],
            "response_threshold": 0.5,
            "max_responses_per_hour": 1  # Only 1 response per hour
        }
        
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        agent = create_response.json()
        agent_id = agent["id"]
        
        # Set up service
        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient
        from smarter_dev.shared.config import get_settings
        
        settings = get_settings()
        api_client = APIClient(base_url=settings.api_base_url, api_key=settings.bot_api_key)
        forum_service = ForumAgentService(api_client, None)
        
        # Mock AI to always want to respond
        with patch('smarter_dev.bot.agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
            mock_evaluate.return_value = ("Should respond", 0.9, "Test response", 100)
            
            # First post - should get response
            post_data_1 = ForumPostData(
                title="First post",
                content="Test content 1",
                author_display_name="TestUser1",
                tags=["test"],
                attachments=[],
                channel_id="forum_channel_123",
                thread_id="thread_1",
                guild_id=guild_id
            )
            
            responses_1 = await forum_service.process_forum_post(guild_id, post_data_1)
            assert len(responses_1) == 1
            assert responses_1[0]["should_respond"] is True
            
            # Second post immediately after - should be rate limited
            post_data_2 = ForumPostData(
                title="Second post",
                content="Test content 2", 
                author_display_name="TestUser2",
                tags=["test"],
                attachments=[],
                channel_id="forum_channel_123",
                thread_id="thread_2",
                guild_id=guild_id
            )
            
            responses_2 = await forum_service.process_forum_post(guild_id, post_data_2)
            assert len(responses_2) == 1
            assert responses_2[0]["should_respond"] is False
            assert "rate limit" in responses_2[0]["decision_reason"].lower()
        
        # Clean up
        await real_api_client.delete(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
            headers=admin_auth_headers
        )

    @pytest.mark.asyncio
    async def test_forum_agent_channel_filtering_integration(self, real_db_session, admin_auth_headers, real_api_client):
        """Test forum channel filtering works correctly."""
        guild_id = "123456789012345678"
        
        # Create agent that only monitors specific channels
        agent_data = {
            "name": "Channel Filtered Agent",
            "system_prompt": "Test channel filtering",
            "monitored_forums": ["allowed_channel_123"],  # Only this channel
            "response_threshold": 0.5,
            "max_responses_per_hour": 10
        }
        
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        agent = create_response.json()
        agent_id = agent["id"]
        
        # Set up service
        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient
        from smarter_dev.shared.config import get_settings
        
        settings = get_settings()
        api_client = APIClient(base_url=settings.api_base_url, api_key=settings.bot_api_key)
        forum_service = ForumAgentService(api_client, None)
        
        with patch('smarter_dev.bot.agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
            mock_evaluate.return_value = ("Should respond", 0.9, "Test response", 100)
            
            # Post in allowed channel - should get response
            post_data_allowed = ForumPostData(
                title="Allowed channel post",
                content="This should be processed",
                author_display_name="TestUser1",
                tags=["test"],
                attachments=[],
                channel_id="allowed_channel_123",  # This channel is monitored
                thread_id="thread_1",
                guild_id=guild_id
            )
            
            responses_allowed = await forum_service.process_forum_post(guild_id, post_data_allowed)
            # Should process the post and call AI evaluation
            assert mock_evaluate.called
            mock_evaluate.reset_mock()
            
            # Post in different channel - should be ignored
            post_data_blocked = ForumPostData(
                title="Blocked channel post",
                content="This should be ignored",
                author_display_name="TestUser2",
                tags=["test"],
                attachments=[],
                channel_id="blocked_channel_456",  # This channel is NOT monitored
                thread_id="thread_2",
                guild_id=guild_id
            )
            
            responses_blocked = await forum_service.process_forum_post(guild_id, post_data_blocked)
            # Should not process the post, so AI evaluation should not be called
            assert not mock_evaluate.called
        
        # Clean up
        await real_api_client.delete(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
            headers=admin_auth_headers
        )

    @pytest.mark.asyncio
    async def test_multiple_forum_agents_integration(self, real_db_session, admin_auth_headers, real_api_client):
        """Test multiple forum agents processing the same post."""
        guild_id = "123456789012345678"
        
        # Create two different agents
        agent_data_1 = {
            "name": "Python Expert",
            "system_prompt": "You are a Python expert. Only respond to Python questions.",
            "monitored_forums": ["programming_forum"],
            "response_threshold": 0.8,
            "max_responses_per_hour": 5
        }
        
        agent_data_2 = {
            "name": "General Helper", 
            "system_prompt": "You are a general programming helper. Respond to any programming question.",
            "monitored_forums": ["programming_forum"],
            "response_threshold": 0.6,
            "max_responses_per_hour": 5
        }
        
        # Create first agent
        response_1 = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data_1,
            headers=admin_auth_headers
        )
        assert response_1.status_code == 201
        agent_1 = response_1.json()
        
        # Create second agent
        response_2 = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data_2,
            headers=admin_auth_headers
        )
        assert response_2.status_code == 201
        agent_2 = response_2.json()
        
        # Set up service
        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient
        from smarter_dev.shared.config import get_settings
        
        settings = get_settings()
        api_client = APIClient(base_url=settings.api_base_url, api_key=settings.bot_api_key)
        forum_service = ForumAgentService(api_client, None)
        
        # Mock different AI responses for each agent
        def mock_evaluate_side_effect(system_prompt, *args, **kwargs):
            if "Python expert" in system_prompt:
                return ("This is a Python question", 0.9, "Python-specific answer", 200)
            else:
                return ("This is a general programming question", 0.7, "General programming answer", 150)
        
        with patch('smarter_dev.bot.agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
            mock_evaluate.side_effect = mock_evaluate_side_effect
            
            # Create Python-related post
            post_data = ForumPostData(
                title="How to use Python lists?",
                content="I need help with Python list operations",
                author_display_name="TestUser",
                tags=["python", "lists"],
                attachments=[],
                channel_id="programming_forum",
                thread_id="thread_123",
                guild_id=guild_id
            )
            
            # Process post through both agents
            responses = await forum_service.process_forum_post(guild_id, post_data)
        
        # Should get responses from both agents
        assert len(responses) == 2
        
        # Verify both agents responded
        agent_names = {r["agent_name"] for r in responses}
        assert "Python Expert" in agent_names
        assert "General Helper" in agent_names
        
        # Verify both decided to respond (confidence above their thresholds)
        responding_agents = [r for r in responses if r["should_respond"]]
        assert len(responding_agents) == 2
        
        # Verify different responses
        python_expert_response = next(r for r in responses if r["agent_name"] == "Python Expert")
        general_helper_response = next(r for r in responses if r["agent_name"] == "General Helper")
        
        assert python_expert_response["confidence"] == 0.9
        assert general_helper_response["confidence"] == 0.7
        assert "Python-specific" in python_expert_response["response_content"]
        assert "General programming" in general_helper_response["response_content"]
        
        # Clean up
        await real_api_client.delete(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_1['id']}",
            headers=admin_auth_headers
        )
        await real_api_client.delete(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_2['id']}",
            headers=admin_auth_headers
        )

    @pytest.mark.asyncio
    async def test_forum_agent_error_recovery_integration(self, real_db_session, admin_auth_headers, real_api_client):
        """Test system handles errors gracefully without breaking."""
        guild_id = "123456789012345678"
        
        # Create test agent
        agent_data = {
            "name": "Error Test Agent",
            "system_prompt": "Test error handling",
            "monitored_forums": ["test_forum"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5
        }
        
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        agent = create_response.json()
        agent_id = agent["id"]
        
        # Set up service
        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient
        from smarter_dev.shared.config import get_settings
        
        settings = get_settings()
        api_client = APIClient(base_url=settings.api_base_url, api_key=settings.bot_api_key)
        forum_service = ForumAgentService(api_client, None)
        
        # Test AI evaluation error doesn't break the system
        with patch('smarter_dev.bot.agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
            mock_evaluate.side_effect = Exception("AI service temporarily unavailable")
            
            post_data = ForumPostData(
                title="Test error handling",
                content="This should handle AI errors gracefully",
                author_display_name="TestUser",
                tags=["test"],
                attachments=[],
                channel_id="test_forum",
                thread_id="thread_error",
                guild_id=guild_id
            )
            
            # Should not raise exception, should handle gracefully
            try:
                responses = await forum_service.process_forum_post(guild_id, post_data)
                # Should return empty list or error response, not crash
                assert isinstance(responses, list)
            except Exception as e:
                # If an exception is raised, it should be a ServiceError, not the original AI error
                assert "AI service" not in str(e)
        
        # Test system continues working after error
        with patch('smarter_dev.bot.agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
            mock_evaluate.return_value = ("Working again", 0.8, "System recovered", 100)
            
            post_data_2 = ForumPostData(
                title="Recovery test",
                content="System should work after error",
                author_display_name="TestUser",
                tags=["test"],
                attachments=[],
                channel_id="test_forum",
                thread_id="thread_recovery",
                guild_id=guild_id
            )
            
            responses = await forum_service.process_forum_post(guild_id, post_data_2)
            assert len(responses) == 1
            assert responses[0]["should_respond"] is True
            assert responses[0]["response_content"] == "System recovered"
        
        # Clean up
        await real_api_client.delete(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
            headers=admin_auth_headers
        )

    @pytest.mark.asyncio
    async def test_forum_agent_discord_event_integration(self, real_db_session):
        """Test Discord event processing integration."""
        guild_id = "123456789012345678"
        
        # Create agent in database directly (simulating existing agent)
        agent = ForumAgent(
            guild_id=guild_id,
            name="Discord Event Test Agent",
            system_prompt="Test Discord event integration",
            monitored_forums=["forum_channel_123"],
            response_threshold=0.7,
            max_responses_per_hour=5,
            is_active=True
        )
        real_db_session.add(agent)
        await real_db_session.commit()
        
        # Test Discord bot event handling
        from smarter_dev.bot.client import handle_forum_thread_create, extract_forum_post_data
        
        # Mock Discord objects
        mock_thread = Mock()
        mock_thread.id = 987654321
        mock_thread.name = "How to test Discord bots?"
        mock_thread.parent_id = 123456789  # forum_channel_123
        mock_thread.applied_tags = []
        
        mock_message = Mock()
        mock_message.content = "I need help testing my Discord bot"
        mock_message.author = Mock()
        mock_message.author.display_name = "BotDeveloper"
        mock_message.attachments = []
        
        mock_event = Mock()
        mock_event.guild_id = int(guild_id)
        mock_event.thread = mock_thread
        mock_event.is_forum_thread = True
        
        # Mock bot with forum agent service
        mock_bot = Mock()
        mock_forum_service = AsyncMock()
        mock_bot.d = {'forum_agent_service': mock_forum_service}
        
        # Mock the service to return a response
        mock_forum_service.process_forum_post.return_value = [{
            'agent_id': str(agent.id),
            'agent_name': 'Discord Event Test Agent',
            'should_respond': True,
            'response_content': 'For Discord bot testing, use pytest with mocks...',
            'confidence': 0.85
        }]
        
        # Mock the response posting
        mock_bot.rest = AsyncMock()
        
        with patch('smarter_dev.bot.client.extract_forum_post_data') as mock_extract:
            mock_post = Mock()
            mock_post.title = "How to test Discord bots?"
            mock_post.content = "I need help testing my Discord bot"
            mock_post.author_display_name = "BotDeveloper"
            mock_post.tags = []
            mock_post.attachments = []
            mock_post.channel_id = "123456789"
            mock_post.thread_id = "987654321"
            mock_post.guild_id = guild_id
            mock_extract.return_value = mock_post
            
            # Test the event handler
            await handle_forum_thread_create(mock_bot, mock_event)
        
        # Verify the flow worked
        mock_forum_service.process_forum_post.assert_called_once_with(guild_id, mock_post)
        mock_bot.rest.create_message.assert_called_once()
        
        # Verify message content
        call_args = mock_bot.rest.create_message.call_args
        assert call_args[0][0] == 987654321  # thread_id
        message_content = call_args[1]['content']
        assert 'Discord Event Test Agent' in message_content
        assert 'pytest with mocks' in message_content

    @pytest.mark.asyncio
    async def test_forum_agent_performance_integration(self, real_db_session, admin_auth_headers, real_api_client):
        """Test forum agent system performance under load."""
        guild_id = "123456789012345678"
        
        # Create multiple agents
        agent_ids = []
        for i in range(3):
            agent_data = {
                "name": f"Performance Agent {i}",
                "system_prompt": f"Performance test agent {i}",
                "monitored_forums": ["performance_forum"],
                "response_threshold": 0.5,
                "max_responses_per_hour": 10
            }
            
            response = await real_api_client.post(
                f"/api/admin/guilds/{guild_id}/forum-agents",
                json=agent_data,
                headers=admin_auth_headers
            )
            assert response.status_code == 201
            agent_ids.append(response.json()["id"])
        
        # Set up service
        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient
        from smarter_dev.shared.config import get_settings
        
        settings = get_settings()
        api_client = APIClient(base_url=settings.api_base_url, api_key=settings.bot_api_key)
        forum_service = ForumAgentService(api_client, None)
        
        # Process multiple posts in sequence
        import time
        start_time = time.time()
        
        with patch('smarter_dev.bot.agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
            mock_evaluate.return_value = ("Performance test", 0.8, "Fast response", 50)
            
            # Process 5 posts
            for i in range(5):
                post_data = ForumPostData(
                    title=f"Performance test post {i}",
                    content=f"Testing performance with post {i}",
                    author_display_name=f"TestUser{i}",
                    tags=["performance"],
                    attachments=[],
                    channel_id="performance_forum",
                    thread_id=f"thread_{i}",
                    guild_id=guild_id
                )
                
                responses = await forum_service.process_forum_post(guild_id, post_data)
                
                # Should get response from all 3 agents
                assert len(responses) == 3
                assert all(r["should_respond"] for r in responses)
        
        end_time = time.time()
        total_time = end_time - start_time
        
        # Performance check: should process 5 posts * 3 agents = 15 evaluations reasonably quickly
        # Allow generous time for CI environments
        assert total_time < 10.0, f"Processing took too long: {total_time}s"
        
        # Verify all evaluations were recorded
        total_responses = 0
        for agent_id in agent_ids:
            analytics_response = await real_api_client.get(
                f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}/analytics",
                headers=admin_auth_headers
            )
            assert analytics_response.status_code == 200
            analytics = analytics_response.json()
            total_responses += analytics["statistics"]["total_evaluations"]
        
        assert total_responses == 15  # 5 posts * 3 agents
        
        # Clean up
        for agent_id in agent_ids:
            await real_api_client.delete(
                f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
                headers=admin_auth_headers
            )