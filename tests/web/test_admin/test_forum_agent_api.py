"""Tests for admin forum agent management API endpoints."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from smarter_dev.web.models import ForumAgent, ForumAgentResponse


class TestForumAgentAPI:
    """Test suite for forum agent API endpoints."""
    
    @pytest.mark.asyncio
    async def test_create_forum_agent_api_success(self, real_api_client, admin_auth_headers, real_db_session):
        """Test successful forum agent creation via API."""
        guild_id = "123456789012345678"
        agent_data = {
            "name": "Test Python Helper",
            "system_prompt": "You are a helpful Python programming assistant.",
            "monitored_forums": ["123456789", "987654321"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5
        }
        
        response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Test Python Helper"
        assert data["guild_id"] == guild_id
        assert data["is_active"] is True
        assert "id" in data
    
    @pytest.mark.asyncio
    async def test_create_forum_agent_api_validation_error(self, real_api_client, admin_auth_headers):
        """Test forum agent creation with validation errors."""
        guild_id = "123456789012345678"
        invalid_data = {
            "name": "",  # Empty name
            "system_prompt": "",  # Empty system prompt
            "response_threshold": 2.0,  # Invalid threshold
            "max_responses_per_hour": -1  # Invalid rate limit
        }
        
        response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=invalid_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
        assert any("name" in str(error) for error in data["detail"])
        assert any("system_prompt" in str(error) for error in data["detail"])
    
    @pytest.mark.asyncio
    async def test_list_forum_agents_api_success(self, real_api_client, admin_auth_headers, real_db_session):
        """Test successful forum agent listing via API."""
        guild_id = "123456789012345678"
        
        # Create test agents
        agents_data = [
            {
                "name": "Python Helper",
                "system_prompt": "Python programming assistant",
                "monitored_forums": ["123456789"],
                "response_threshold": 0.7,
                "max_responses_per_hour": 5
            },
            {
                "name": "Code Reviewer",
                "system_prompt": "Code review assistant",
                "monitored_forums": ["987654321"],
                "response_threshold": 0.8,
                "max_responses_per_hour": 3
            }
        ]
        
        # Create agents via API
        created_agents = []
        for agent_data in agents_data:
            response = await real_api_client.post(
                f"/api/admin/guilds/{guild_id}/forum-agents",
                json=agent_data,
                headers=admin_auth_headers
            )
            assert response.status_code == 201
            created_agents.append(response.json())
        
        # List agents
        response = await real_api_client.get(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert any(agent["name"] == "Python Helper" for agent in data)
        assert any(agent["name"] == "Code Reviewer" for agent in data)
    
    @pytest.mark.asyncio
    async def test_get_forum_agent_api_success(self, real_api_client, admin_auth_headers, real_db_session):
        """Test successful forum agent retrieval via API."""
        guild_id = "123456789012345678"
        agent_data = {
            "name": "Test Agent",
            "system_prompt": "Test system prompt",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5
        }
        
        # Create agent
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        created_agent = create_response.json()
        agent_id = created_agent["id"]
        
        # Get agent
        response = await real_api_client.get(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == agent_id
        assert data["name"] == "Test Agent"
        assert data["system_prompt"] == "Test system prompt"
    
    @pytest.mark.asyncio
    async def test_get_forum_agent_api_not_found(self, real_api_client, admin_auth_headers):
        """Test forum agent retrieval for non-existent agent."""
        guild_id = "123456789012345678"
        fake_agent_id = str(uuid4())
        
        response = await real_api_client.get(
            f"/api/admin/guilds/{guild_id}/forum-agents/{fake_agent_id}",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 404
        data = response.json()
        assert "not found" in data["detail"].lower()
    
    @pytest.mark.asyncio
    async def test_update_forum_agent_api_success(self, real_api_client, admin_auth_headers, real_db_session):
        """Test successful forum agent update via API."""
        guild_id = "123456789012345678"
        
        # Create agent
        create_data = {
            "name": "Original Name",
            "system_prompt": "Original system prompt",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5
        }
        
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=create_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        agent_id = create_response.json()["id"]
        
        # Update agent
        update_data = {
            "name": "Updated Name",
            "system_prompt": "Updated system prompt",
            "monitored_forums": ["123456789", "987654321"],
            "response_threshold": 0.8,
            "max_responses_per_hour": 10
        }
        
        response = await real_api_client.put(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
            json=update_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
        assert data["system_prompt"] == "Updated system prompt"
        assert len(data["monitored_forums"]) == 2
        assert data["response_threshold"] == 0.8
        assert data["max_responses_per_hour"] == 10
    
    @pytest.mark.asyncio
    async def test_delete_forum_agent_api_success(self, real_api_client, admin_auth_headers, real_db_session):
        """Test successful forum agent deletion via API."""
        guild_id = "123456789012345678"
        agent_data = {
            "name": "To Delete",
            "system_prompt": "Will be deleted",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5
        }
        
        # Create agent
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        agent_id = create_response.json()["id"]
        
        # Delete agent
        response = await real_api_client.delete(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 204
        
        # Verify deletion
        get_response = await real_api_client.get(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
            headers=admin_auth_headers
        )
        assert get_response.status_code == 404
    
    @pytest.mark.asyncio
    async def test_toggle_forum_agent_api_success(self, real_api_client, admin_auth_headers, real_db_session):
        """Test successful forum agent toggle via API."""
        guild_id = "123456789012345678"
        agent_data = {
            "name": "Toggle Test",
            "system_prompt": "Test toggle functionality",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5
        }
        
        # Create agent (starts active)
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        agent_id = create_response.json()["id"]
        assert create_response.json()["is_active"] is True
        
        # Toggle to inactive
        response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}/toggle",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False
        
        # Toggle back to active
        response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}/toggle",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is True


class TestForumAgentAnalyticsAPI:
    """Test suite for forum agent analytics API endpoints."""
    
    @pytest.mark.asyncio
    async def test_get_forum_agent_analytics_api_success(self, real_api_client, admin_auth_headers, real_db_session):
        """Test successful forum agent analytics retrieval via API."""
        guild_id = "123456789012345678"
        
        # Create forum agent
        agent_data = {
            "name": "Analytics Test Agent",
            "system_prompt": "Test analytics functionality",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5
        }
        
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        agent_id = create_response.json()["id"]
        
        # Create some test responses for analytics
        from smarter_dev.web.models import ForumAgentResponse
        from datetime import datetime, timezone
        
        test_responses = [
            ForumAgentResponse(
                forum_agent_id=agent_id,
                channel_id="123456789",
                thread_id="thread_1",
                post_title="Test Question 1",
                post_content="How do I use Python?",
                author_display_name="TestUser1",
                post_tags=["python"],
                attachments=[],
                decision_reason="This is a Python question",
                confidence_score=0.85,
                response_content="Python is a programming language...",
                tokens_used=250,
                response_time_ms=800,
                responded=True,
                created_at=datetime.now(timezone.utc)
            ),
            ForumAgentResponse(
                forum_agent_id=agent_id,
                channel_id="123456789",
                thread_id="thread_2", 
                post_title="Test Question 2",
                post_content="What is JavaScript?",
                author_display_name="TestUser2",
                post_tags=["javascript"],
                attachments=[],
                decision_reason="This is not my area of expertise",
                confidence_score=0.3,
                response_content="",
                tokens_used=150,
                response_time_ms=600,
                responded=False,
                created_at=datetime.now(timezone.utc)
            )
        ]
        
        # Add responses to database
        for response in test_responses:
            real_db_session.add(response)
        await real_db_session.commit()
        
        # Get analytics
        response = await real_api_client.get(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}/analytics",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify analytics structure
        assert "agent" in data
        assert "statistics" in data
        
        # Verify agent data
        assert data["agent"]["id"] == agent_id
        assert data["agent"]["name"] == "Analytics Test Agent"
        
        # Verify statistics
        stats = data["statistics"]
        assert stats["total_evaluations"] == 2
        assert stats["total_responses"] == 1
        assert stats["response_rate"] == 0.5  # 1 out of 2
        assert stats["total_tokens_used"] == 400  # 250 + 150
        assert stats["average_confidence"] == 0.575  # (0.85 + 0.3) / 2
        assert stats["average_response_time_ms"] == 700  # (800 + 600) / 2
    
    @pytest.mark.asyncio 
    async def test_get_forum_agent_analytics_empty_data(self, real_api_client, admin_auth_headers, real_db_session):
        """Test forum agent analytics with no response data."""
        guild_id = "123456789012345678"
        
        # Create forum agent
        agent_data = {
            "name": "Empty Analytics Agent",
            "system_prompt": "Test empty analytics",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5
        }
        
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        agent_id = create_response.json()["id"]
        
        # Get analytics (no responses created)
        response = await real_api_client.get(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}/analytics",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify empty statistics
        stats = data["statistics"]
        assert stats["total_evaluations"] == 0
        assert stats["total_responses"] == 0
        assert stats["response_rate"] == 0.0
        assert stats["total_tokens_used"] == 0
        assert stats["average_confidence"] is None
        assert stats["average_response_time_ms"] is None
    
    @pytest.mark.asyncio
    async def test_get_guild_forum_agent_analytics_overview(self, real_api_client, admin_auth_headers, real_db_session):
        """Test guild-wide forum agent analytics overview."""
        guild_id = "123456789012345678"
        
        # Create multiple forum agents
        agent_names = ["Agent 1", "Agent 2", "Agent 3"]
        agent_ids = []
        
        for name in agent_names:
            agent_data = {
                "name": name,
                "system_prompt": f"System prompt for {name}",
                "monitored_forums": ["123456789"],
                "response_threshold": 0.7,
                "max_responses_per_hour": 5
            }
            
            create_response = await real_api_client.post(
                f"/api/admin/guilds/{guild_id}/forum-agents",
                json=agent_data,
                headers=admin_auth_headers
            )
            assert create_response.status_code == 201
            agent_ids.append(create_response.json()["id"])
        
        # Get guild analytics overview
        response = await real_api_client.get(
            f"/api/admin/guilds/{guild_id}/forum-agents/analytics",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify overview structure
        assert "guild_id" in data
        assert "total_agents" in data
        assert "active_agents" in data
        assert "overall_statistics" in data
        assert "agent_summaries" in data
        
        # Verify counts
        assert data["guild_id"] == guild_id
        assert data["total_agents"] == 3
        assert data["active_agents"] == 3  # All created as active
        assert len(data["agent_summaries"]) == 3
        
        # Verify agent summaries
        summary_names = [agent["name"] for agent in data["agent_summaries"]]
        for name in agent_names:
            assert name in summary_names


class TestForumAgentSecurityAPI:
    """Test suite for forum agent API security and authorization."""
    
    @pytest.mark.asyncio
    async def test_forum_agent_api_requires_authentication(self, real_api_client):
        """Test that forum agent API endpoints require authentication."""
        guild_id = "123456789012345678"
        agent_id = str(uuid4())
        
        # Test endpoints without auth headers
        endpoints = [
            ("GET", f"/api/admin/guilds/{guild_id}/forum-agents"),
            ("POST", f"/api/admin/guilds/{guild_id}/forum-agents"),
            ("GET", f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}"),
            ("PUT", f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}"),
            ("DELETE", f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}"),
            ("POST", f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}/toggle"),
            ("GET", f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}/analytics"),
        ]
        
        for method, url in endpoints:
            if method == "GET":
                response = await real_api_client.get(url)
            elif method == "POST":
                response = await real_api_client.post(url, json={})
            elif method == "PUT":
                response = await real_api_client.put(url, json={})
            elif method == "DELETE":
                response = await real_api_client.delete(url)
            
            assert response.status_code == 401, f"Expected 401 for {method} {url}, got {response.status_code}"
            data = response.json()
            assert "authentication" in data["detail"].lower() or "unauthorized" in data["detail"].lower()
    
    @pytest.mark.asyncio
    async def test_forum_agent_api_guild_isolation(self, real_api_client, admin_auth_headers, real_db_session):
        """Test that forum agents are properly isolated by guild."""
        guild1_id = "123456789012345678"
        guild2_id = "987654321098765432"
        
        # Create agent in guild 1
        agent_data = {
            "name": "Guild 1 Agent",
            "system_prompt": "Agent for guild 1",
            "monitored_forums": ["123456789"],
            "response_threshold": 0.7,
            "max_responses_per_hour": 5
        }
        
        create_response = await real_api_client.post(
            f"/api/admin/guilds/{guild1_id}/forum-agents",
            json=agent_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        agent_id = create_response.json()["id"]
        
        # Try to access agent from guild 2 (should fail)
        response = await real_api_client.get(
            f"/api/admin/guilds/{guild2_id}/forum-agents/{agent_id}",
            headers=admin_auth_headers
        )
        assert response.status_code == 404
        
        # List agents in guild 2 (should be empty)
        response = await real_api_client.get(
            f"/api/admin/guilds/{guild2_id}/forum-agents",
            headers=admin_auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0
        
        # List agents in guild 1 (should have our agent)
        response = await real_api_client.get(
            f"/api/admin/guilds/{guild1_id}/forum-agents",
            headers=admin_auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == agent_id


class TestForumAgentBulkOperationsAPI:
    """Test suite for forum agent bulk operations API."""
    
    @pytest.mark.asyncio
    async def test_bulk_enable_disable_agents_api(self, real_api_client, admin_auth_headers, real_db_session):
        """Test bulk enable/disable of forum agents via API."""
        guild_id = "123456789012345678"
        
        # Create multiple agents
        agent_ids = []
        for i in range(3):
            agent_data = {
                "name": f"Bulk Test Agent {i}",
                "system_prompt": f"Bulk test agent {i}",
                "monitored_forums": ["123456789"],
                "response_threshold": 0.7,
                "max_responses_per_hour": 5
            }
            
            create_response = await real_api_client.post(
                f"/api/admin/guilds/{guild_id}/forum-agents",
                json=agent_data,
                headers=admin_auth_headers
            )
            assert create_response.status_code == 201
            agent_ids.append(create_response.json()["id"])
        
        # Bulk disable
        bulk_data = {
            "action": "disable",
            "agent_ids": agent_ids
        }
        
        response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents/bulk",
            json=bulk_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["modified_count"] == 3
        
        # Verify all agents are disabled
        for agent_id in agent_ids:
            get_response = await real_api_client.get(
                f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
                headers=admin_auth_headers
            )
            assert get_response.status_code == 200
            assert get_response.json()["is_active"] is False
        
        # Bulk enable
        bulk_data["action"] = "enable"
        response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents/bulk",
            json=bulk_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["modified_count"] == 3
        
        # Verify all agents are enabled
        for agent_id in agent_ids:
            get_response = await real_api_client.get(
                f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
                headers=admin_auth_headers
            )
            assert get_response.status_code == 200
            assert get_response.json()["is_active"] is True
    
    @pytest.mark.asyncio
    async def test_bulk_delete_agents_api(self, real_api_client, admin_auth_headers, real_db_session):
        """Test bulk deletion of forum agents via API."""
        guild_id = "123456789012345678"
        
        # Create multiple agents
        agent_ids = []
        for i in range(3):
            agent_data = {
                "name": f"Delete Test Agent {i}",
                "system_prompt": f"Delete test agent {i}",
                "monitored_forums": ["123456789"],
                "response_threshold": 0.7,
                "max_responses_per_hour": 5
            }
            
            create_response = await real_api_client.post(
                f"/api/admin/guilds/{guild_id}/forum-agents",
                json=agent_data,
                headers=admin_auth_headers
            )
            assert create_response.status_code == 201
            agent_ids.append(create_response.json()["id"])
        
        # Bulk delete
        bulk_data = {
            "action": "delete",
            "agent_ids": agent_ids[:2]  # Delete first 2 agents only
        }
        
        response = await real_api_client.post(
            f"/api/admin/guilds/{guild_id}/forum-agents/bulk",
            json=bulk_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["modified_count"] == 2
        
        # Verify first 2 agents are deleted
        for agent_id in agent_ids[:2]:
            get_response = await real_api_client.get(
                f"/api/admin/guilds/{guild_id}/forum-agents/{agent_id}",
                headers=admin_auth_headers
            )
            assert get_response.status_code == 404
        
        # Verify third agent still exists
        get_response = await real_api_client.get(
            f"/api/admin/guilds/{guild_id}/forum-agents/{agent_ids[2]}",
            headers=admin_auth_headers
        )
        assert get_response.status_code == 200