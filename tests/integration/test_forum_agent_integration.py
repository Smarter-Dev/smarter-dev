"""Integration tests for complete forum agent flow.

This module tests the entire forum agent system end-to-end:
1. Database operations (forum agents and responses)
2. Bot event handling (Discord forum posts)
3. AI evaluation and response generation

These tests use real database connections and mock Discord/AI services.
Agents are created/managed via direct database operations since the API
only exposes GET/response-recording endpoints for forum agents.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
from datetime import datetime, timezone

from smarter_dev.web.models import ForumAgent, ForumAgentResponse
from smarter_dev.web.crud import ForumAgentOperations
from smarter_dev.bot.client import ForumPostData


def _mock_response(json_data, status_code=200):
    """Create a mock httpx Response object."""
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
    return resp


class TestForumAgentIntegrationFlow:
    """Test complete forum agent integration flow."""

    @pytest.mark.asyncio
    async def test_complete_forum_agent_workflow(self, real_db_session):
        """Test complete workflow: Create agent -> Process post -> Verify via API."""
        guild_id = "123456789012345678"

        # Step 1: Create forum agent directly in database
        forum_ops = ForumAgentOperations(real_db_session)
        db_agent = await forum_ops.create_agent(
            guild_id=guild_id,
            name="Integration Test Agent",
            system_prompt="You are a helpful programming assistant. Only respond to Python-related questions.",
            monitored_forums=["forum_channel_123", "forum_channel_456"],
            response_threshold=0.7,
            max_responses_per_hour=5,
            created_by="test_user"
        )
        await real_db_session.commit()
        agent_id = str(db_agent.id)

        # Step 2: Verify agent was created in database
        assert db_agent is not None
        assert db_agent.name == "Integration Test Agent"
        assert db_agent.guild_id == guild_id
        assert db_agent.is_active is True

        # Step 3: Simulate forum post processing
        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient

        agents_list = [{
            "id": agent_id,
            "guild_id": guild_id,
            "name": "Integration Test Agent",
            "system_prompt": db_agent.system_prompt,
            "monitored_forums": db_agent.monitored_forums,
            "response_threshold": db_agent.response_threshold,
            "max_responses_per_hour": db_agent.max_responses_per_hour,
            "is_active": True
        }]

        with patch.object(APIClient, '__init__', return_value=None), \
             patch.object(APIClient, 'get') as mock_get, \
             patch.object(APIClient, 'post') as mock_post:

            def mock_get_side_effect(url, **kwargs):
                if "count" in url:
                    return _mock_response({"count": 0})
                return _mock_response(agents_list)

            mock_get.side_effect = mock_get_side_effect
            mock_post.return_value = _mock_response({"id": str(uuid4())})

            forum_service = ForumAgentService(APIClient("http://test", "test"), None)

            post_data = ForumPostData(
                title="How to fix Python import error?",
                content="I'm getting ImportError when trying to import my module. Help!",
                author_display_name="TestUser",
                tags=["python", "error", "help"],
                attachments=["error_screenshot.png"],
                channel_id="forum_channel_123",
                thread_id="thread_987654321",
                guild_id=guild_id
            )

            with patch('smarter_dev.bot.agents.forum_agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
                mock_evaluate.return_value = (
                    "This is a Python-related question about import errors",
                    0.85,
                    "To fix Python import errors, check your PYTHONPATH and module structure...",
                    300
                )

                responses = await forum_service.process_forum_post(guild_id, post_data)

        # Step 4: Verify response was generated
        assert len(responses) == 1
        response = responses[0]
        assert response["agent_name"] == "Integration Test Agent"
        assert response["should_respond"] is True
        assert response["confidence"] == 0.85

        # Step 5: Deactivate agent and verify it doesn't respond
        db_agent.is_active = False
        await real_db_session.commit()

        inactive_agents_list = [{
            "id": agent_id,
            "guild_id": guild_id,
            "name": "Integration Test Agent",
            "system_prompt": db_agent.system_prompt,
            "monitored_forums": db_agent.monitored_forums,
            "response_threshold": db_agent.response_threshold,
            "max_responses_per_hour": db_agent.max_responses_per_hour,
            "is_active": False
        }]

        with patch.object(APIClient, '__init__', return_value=None), \
             patch.object(APIClient, 'get') as mock_get:

            mock_get.return_value = _mock_response(inactive_agents_list)

            forum_service_2 = ForumAgentService(APIClient("http://test", "test"), None)

            with patch('smarter_dev.bot.agents.forum_agent.ForumMonitorAgent.evaluate_post') as mock_evaluate_2:
                mock_evaluate_2.return_value = ("Test", 0.9, "Test response", 100)

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

                responses_2 = await forum_service_2.process_forum_post(guild_id, post_data_2)

        active_responses = [r for r in responses_2 if r.get("should_respond", False)]
        assert len(active_responses) == 0

    @pytest.mark.asyncio
    async def test_forum_agent_rate_limiting_integration(self, real_db_session):
        """Test rate limiting works correctly in integration."""
        guild_id = "123456789012345678"

        forum_ops = ForumAgentOperations(real_db_session)
        db_agent = await forum_ops.create_agent(
            guild_id=guild_id,
            name="Rate Limited Agent",
            system_prompt="Test rate limiting",
            monitored_forums=["forum_channel_123"],
            response_threshold=0.5,
            max_responses_per_hour=1,
            created_by="test_user"
        )
        await real_db_session.commit()

        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient

        agents_list = [{
            "id": str(db_agent.id),
            "guild_id": guild_id,
            "name": db_agent.name,
            "system_prompt": db_agent.system_prompt,
            "monitored_forums": db_agent.monitored_forums,
            "response_threshold": db_agent.response_threshold,
            "max_responses_per_hour": 1,
            "is_active": True
        }]

        with patch.object(APIClient, '__init__', return_value=None), \
             patch.object(APIClient, 'get') as mock_get, \
             patch.object(APIClient, 'post') as mock_post:

            response_count = 0

            def mock_get_side_effect(url, **kwargs):
                if "count" in url:
                    return _mock_response({"count": response_count})
                return _mock_response(agents_list)

            def mock_post_side_effect(*args, **kwargs):
                nonlocal response_count
                response_count += 1
                return _mock_response({"id": str(uuid4())})

            mock_get.side_effect = mock_get_side_effect
            mock_post.side_effect = mock_post_side_effect

            forum_service = ForumAgentService(APIClient("http://test", "test"), None)

            with patch('smarter_dev.bot.agents.forum_agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
                mock_evaluate.return_value = ("Should respond", 0.9, "Test response", 100)

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

    @pytest.mark.asyncio
    async def test_forum_agent_channel_filtering_integration(self, real_db_session):
        """Test forum channel filtering works correctly."""
        guild_id = "123456789012345678"

        forum_ops = ForumAgentOperations(real_db_session)
        db_agent = await forum_ops.create_agent(
            guild_id=guild_id,
            name="Channel Filtered Agent",
            system_prompt="Test channel filtering",
            monitored_forums=["allowed_channel_123"],
            response_threshold=0.5,
            max_responses_per_hour=10,
            created_by="test_user"
        )
        await real_db_session.commit()

        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient

        agents_list = [{
            "id": str(db_agent.id),
            "guild_id": guild_id,
            "name": db_agent.name,
            "system_prompt": db_agent.system_prompt,
            "monitored_forums": ["allowed_channel_123"],
            "response_threshold": db_agent.response_threshold,
            "max_responses_per_hour": db_agent.max_responses_per_hour,
            "is_active": True
        }]

        with patch.object(APIClient, '__init__', return_value=None), \
             patch.object(APIClient, 'get') as mock_get, \
             patch.object(APIClient, 'post') as mock_post:

            def mock_get_side_effect(url, **kwargs):
                if "count" in url:
                    return _mock_response({"count": 0})
                return _mock_response(agents_list)

            mock_get.side_effect = mock_get_side_effect
            mock_post.return_value = _mock_response({"id": str(uuid4())})

            forum_service = ForumAgentService(APIClient("http://test", "test"), None)

            with patch('smarter_dev.bot.agents.forum_agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
                mock_evaluate.return_value = ("Should respond", 0.9, "Test response", 100)

                post_data_allowed = ForumPostData(
                    title="Allowed channel post",
                    content="This should be processed",
                    author_display_name="TestUser1",
                    tags=["test"],
                    attachments=[],
                    channel_id="allowed_channel_123",
                    thread_id="thread_1",
                    guild_id=guild_id
                )

                responses_allowed = await forum_service.process_forum_post(guild_id, post_data_allowed)
                assert mock_evaluate.called
                mock_evaluate.reset_mock()

                post_data_blocked = ForumPostData(
                    title="Blocked channel post",
                    content="This should be ignored",
                    author_display_name="TestUser2",
                    tags=["test"],
                    attachments=[],
                    channel_id="blocked_channel_456",
                    thread_id="thread_2",
                    guild_id=guild_id
                )

                responses_blocked = await forum_service.process_forum_post(guild_id, post_data_blocked)
                assert not mock_evaluate.called

    @pytest.mark.asyncio
    async def test_multiple_forum_agents_integration(self, real_db_session):
        """Test multiple forum agents processing the same post."""
        guild_id = "123456789012345678"

        forum_ops = ForumAgentOperations(real_db_session)
        agent_1 = await forum_ops.create_agent(
            guild_id=guild_id,
            name="Python Expert",
            system_prompt="You are a Python expert. Only respond to Python questions.",
            monitored_forums=["programming_forum"],
            response_threshold=0.8,
            max_responses_per_hour=5,
            created_by="test_user"
        )
        agent_2 = await forum_ops.create_agent(
            guild_id=guild_id,
            name="General Helper",
            system_prompt="You are a general programming helper. Respond to any programming question.",
            monitored_forums=["programming_forum"],
            response_threshold=0.6,
            max_responses_per_hour=5,
            created_by="test_user"
        )
        await real_db_session.commit()

        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient

        agents_list = [
            {
                "id": str(agent_1.id),
                "guild_id": guild_id,
                "name": "Python Expert",
                "system_prompt": agent_1.system_prompt,
                "monitored_forums": ["programming_forum"],
                "response_threshold": 0.8,
                "max_responses_per_hour": 5,
                "is_active": True
            },
            {
                "id": str(agent_2.id),
                "guild_id": guild_id,
                "name": "General Helper",
                "system_prompt": agent_2.system_prompt,
                "monitored_forums": ["programming_forum"],
                "response_threshold": 0.6,
                "max_responses_per_hour": 5,
                "is_active": True
            }
        ]

        with patch.object(APIClient, '__init__', return_value=None), \
             patch.object(APIClient, 'get') as mock_get, \
             patch.object(APIClient, 'post') as mock_post:

            def mock_get_side_effect(url, **kwargs):
                if "count" in url:
                    return _mock_response({"count": 0})
                return _mock_response(agents_list)

            mock_get.side_effect = mock_get_side_effect
            mock_post.return_value = _mock_response({"id": str(uuid4())})

            forum_service = ForumAgentService(APIClient("http://test", "test"), None)

            def mock_evaluate_side_effect(system_prompt, *args, **kwargs):
                if "Python expert" in system_prompt:
                    return ("This is a Python question", 0.9, "Python-specific answer", 200)
                else:
                    return ("This is a general programming question", 0.7, "General programming answer", 150)

            with patch('smarter_dev.bot.agents.forum_agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
                mock_evaluate.side_effect = mock_evaluate_side_effect

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

                responses = await forum_service.process_forum_post(guild_id, post_data)

        assert len(responses) == 2

        agent_names = {r["agent_name"] for r in responses}
        assert "Python Expert" in agent_names
        assert "General Helper" in agent_names

        responding_agents = [r for r in responses if r["should_respond"]]
        assert len(responding_agents) == 2

        python_expert_response = next(r for r in responses if r["agent_name"] == "Python Expert")
        general_helper_response = next(r for r in responses if r["agent_name"] == "General Helper")

        assert python_expert_response["confidence"] == 0.9
        assert general_helper_response["confidence"] == 0.7
        assert "Python-specific" in python_expert_response["response_content"]
        assert "General programming" in general_helper_response["response_content"]

    @pytest.mark.asyncio
    async def test_forum_agent_error_recovery_integration(self, real_db_session):
        """Test system handles errors gracefully without breaking."""
        guild_id = "123456789012345678"

        forum_ops = ForumAgentOperations(real_db_session)
        db_agent = await forum_ops.create_agent(
            guild_id=guild_id,
            name="Error Test Agent",
            system_prompt="Test error handling",
            monitored_forums=["test_forum"],
            response_threshold=0.7,
            max_responses_per_hour=5,
            created_by="test_user"
        )
        await real_db_session.commit()

        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient

        agents_list = [{
            "id": str(db_agent.id),
            "guild_id": guild_id,
            "name": db_agent.name,
            "system_prompt": db_agent.system_prompt,
            "monitored_forums": db_agent.monitored_forums,
            "response_threshold": db_agent.response_threshold,
            "max_responses_per_hour": db_agent.max_responses_per_hour,
            "is_active": True
        }]

        with patch.object(APIClient, '__init__', return_value=None), \
             patch.object(APIClient, 'get') as mock_get, \
             patch.object(APIClient, 'post') as mock_post:

            def mock_get_side_effect(url, **kwargs):
                if "count" in url:
                    return _mock_response({"count": 0})
                return _mock_response(agents_list)

            mock_get.side_effect = mock_get_side_effect
            mock_post.return_value = _mock_response({"id": str(uuid4())})

            forum_service = ForumAgentService(APIClient("http://test", "test"), None)

            # Test AI evaluation error doesn't break the system
            with patch('smarter_dev.bot.agents.forum_agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
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

                try:
                    responses = await forum_service.process_forum_post(guild_id, post_data)
                    assert isinstance(responses, list)
                except Exception as e:
                    assert "AI service" not in str(e)

            # Test system continues working after error
            with patch('smarter_dev.bot.agents.forum_agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
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

    @pytest.mark.asyncio
    async def test_forum_agent_discord_event_integration(self, real_db_session):
        """Test Discord event processing integration."""
        guild_id = "123456789012345678"

        agent = ForumAgent(
            guild_id=guild_id,
            name="Discord Event Test Agent",
            system_prompt="Test Discord event integration",
            monitored_forums=["forum_channel_123"],
            response_threshold=0.7,
            max_responses_per_hour=5,
            is_active=True,
            created_by="test_user"
        )
        real_db_session.add(agent)
        await real_db_session.commit()

        from smarter_dev.bot.client import handle_forum_thread_create, extract_forum_post_data

        mock_thread = Mock()
        mock_thread.id = 987654321
        mock_thread.name = "How to test Discord bots?"
        mock_thread.parent_id = 123456789
        mock_thread.applied_tags = []

        mock_event = Mock()
        mock_event.guild_id = int(guild_id)
        mock_event.thread = mock_thread
        mock_event.is_forum_thread = True

        mock_bot = Mock()
        mock_forum_service = AsyncMock()
        mock_bot.d = {'forum_agent_service': mock_forum_service}

        # handle_forum_thread_create calls process_forum_post_with_tagging (not process_forum_post)
        # It returns (responses, topic_user_map)
        mock_forum_service.process_forum_post_with_tagging.return_value = (
            [{
                'agent_id': str(agent.id),
                'agent_name': 'Discord Event Test Agent',
                'should_respond': True,
                'response_content': 'For Discord bot testing, use pytest with mocks...',
                'confidence': 0.85
            }],
            {}  # empty topic_user_map
        )

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

            await handle_forum_thread_create(mock_bot, mock_event)

        mock_forum_service.process_forum_post_with_tagging.assert_called_once()
        mock_bot.rest.create_message.assert_called_once()

        call_args = mock_bot.rest.create_message.call_args
        assert call_args[0][0] == 987654321
        # post_agent_responses uses raw response_content (no agent name prefix)
        message_content = call_args[1]['content']
        assert 'pytest with mocks' in message_content

    @pytest.mark.asyncio
    async def test_forum_agent_performance_integration(self, real_db_session):
        """Test forum agent system performance under load."""
        guild_id = "123456789012345678"

        forum_ops = ForumAgentOperations(real_db_session)
        agents = []
        for i in range(3):
            agent = await forum_ops.create_agent(
                guild_id=guild_id,
                name=f"Performance Agent {i}",
                system_prompt=f"Performance test agent {i}",
                monitored_forums=["performance_forum"],
                response_threshold=0.5,
                max_responses_per_hour=100,
                created_by="test_user"
            )
            agents.append(agent)
        await real_db_session.commit()

        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.api_client import APIClient

        agents_list = [
            {
                "id": str(a.id),
                "guild_id": guild_id,
                "name": a.name,
                "system_prompt": a.system_prompt,
                "monitored_forums": ["performance_forum"],
                "response_threshold": 0.5,
                "max_responses_per_hour": 100,
                "is_active": True
            }
            for a in agents
        ]

        with patch.object(APIClient, '__init__', return_value=None), \
             patch.object(APIClient, 'get') as mock_get, \
             patch.object(APIClient, 'post') as mock_post:

            response_count = 0

            def mock_get_side_effect(url, **kwargs):
                if "count" in url:
                    return _mock_response({"count": response_count})
                return _mock_response(agents_list)

            def mock_post_side_effect(*args, **kwargs):
                nonlocal response_count
                response_count += 1
                return _mock_response({"id": str(uuid4())})

            mock_get.side_effect = mock_get_side_effect
            mock_post.side_effect = mock_post_side_effect

            forum_service = ForumAgentService(APIClient("http://test", "test"), None)

            import time
            start_time = time.time()

            with patch('smarter_dev.bot.agents.forum_agent.ForumMonitorAgent.evaluate_post') as mock_evaluate:
                mock_evaluate.return_value = ("Performance test", 0.8, "Fast response", 50)

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
                    assert len(responses) == 3
                    assert all(r["should_respond"] for r in responses)

            end_time = time.time()
            total_time = end_time - start_time

            assert total_time < 10.0, f"Processing took too long: {total_time}s"
