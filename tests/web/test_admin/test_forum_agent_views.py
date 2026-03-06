"""Tests for admin forum agent management views."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from smarter_dev.web.admin.discord import GuildNotFoundError, DiscordAPIError


class TestForumAgentList:
    """Test suite for forum agent list view."""

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_list_success(self, mock_get_guild, mock_db_session, authenticated_client, mock_discord_guilds, mock_forum_agents):
        """Test successful forum agent list rendering."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session and forum agent query
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock forum agents query result
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = mock_forum_agents
        mock_session.execute.return_value = mock_result

        guild_id = "123456789012345678"
        response = authenticated_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents")

        assert response.status_code == 200
        assert b"Forum Agents" in response.content
        assert b"Python Helper" in response.content
        assert b"Code Reviewer" in response.content

    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_list_invalid_guild(self, mock_get_guild, authenticated_client):
        """Test forum agent list with invalid guild."""
        mock_get_guild.side_effect = GuildNotFoundError("Guild not found")

        guild_id = "invalid_guild_id"
        response = authenticated_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents")

        assert response.status_code == 404
        assert b"Guild not found" in response.content

    def test_forum_agent_list_requires_authentication(self, admin_client):
        """Test forum agent list requires authentication."""
        guild_id = "123456789012345678"
        response = admin_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents", follow_redirects=False)

        assert response.status_code == 303
        assert "/bot-admin/login" in response.headers["location"]


class TestForumAgentCreate:
    """Test suite for forum agent creation view."""

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    @patch("smarter_dev.web.admin.views.get_guild_roles")
    def test_forum_agent_create_get_success(self, mock_get_roles, mock_get_guild, mock_db_session,
                                           authenticated_client, mock_discord_guilds, mock_discord_roles):
        """Test successful forum agent creation form rendering."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        mock_get_roles.return_value = mock_discord_roles

        # Mock database session for forum channels query
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        guild_id = "123456789012345678"
        response = authenticated_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents/create")

        assert response.status_code == 200
        assert b"Create Forum Agent" in response.content
        assert b"Agent Name" in response.content
        assert b"System Prompt" in response.content
        assert b"Monitored Forums" in response.content
        assert b"Response Threshold" in response.content

    @patch("smarter_dev.web.admin.views.ForumAgentOperations")
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_create_post_success(self, mock_get_guild, mock_db_session,
                                           mock_forum_ops_cls, authenticated_client, mock_discord_guilds, sample_forum_agent_data):
        """Test successful forum agent creation via POST."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock ForumAgentOperations
        mock_forum_ops = AsyncMock()
        mock_forum_ops_cls.return_value = mock_forum_ops
        mock_forum_ops.create_agent.return_value = Mock()

        guild_id = "123456789012345678"
        # Add required enable_responses field for validation
        post_data = sample_forum_agent_data.copy()
        post_data["enable_responses"] = "on"

        response = authenticated_client.post(
            f"/bot-admin/guilds/{guild_id}/forum-agents/create",
            data=post_data,
            follow_redirects=False
        )

        assert response.status_code == 303
        assert f"/bot-admin/guilds/{guild_id}/forum-agents" in response.headers["location"]

        # Verify ForumAgentOperations.create_agent was called
        mock_forum_ops.create_agent.assert_called_once()

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_create_post_invalid_data(self, mock_get_guild, mock_db_session,
                                                 authenticated_client, mock_discord_guilds):
        """Test forum agent creation with invalid data."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        guild_id = "123456789012345678"
        invalid_data = {
            "name": "",  # Empty name
            "system_prompt": "",  # Empty system prompt
            "response_threshold": "invalid",  # Invalid threshold
        }

        response = authenticated_client.post(
            f"/bot-admin/guilds/{guild_id}/forum-agents/create",
            data=invalid_data
        )

        assert response.status_code == 400
        # The page returns validation errors in the template
        content_lower = response.content.lower()
        assert b"required" in content_lower or b"error" in content_lower or b"invalid" in content_lower

    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_create_invalid_guild(self, mock_get_guild, authenticated_client):
        """Test forum agent creation with invalid guild."""
        mock_get_guild.side_effect = GuildNotFoundError("Guild not found")

        guild_id = "invalid_guild_id"
        response = authenticated_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents/create")

        assert response.status_code == 404
        assert b"Guild not found" in response.content


class TestForumAgentEdit:
    """Test suite for forum agent editing view."""

    @patch("smarter_dev.web.admin.views.ForumAgentOperations")
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    @patch("smarter_dev.web.admin.views.get_guild_roles")
    def test_forum_agent_edit_get_success(self, mock_get_roles, mock_get_guild, mock_db_session,
                                        mock_forum_ops_cls, authenticated_client, mock_discord_guilds, mock_discord_roles, mock_forum_agents):
        """Test successful forum agent edit form rendering."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        mock_get_roles.return_value = mock_discord_roles

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock ForumAgentOperations.get_agent
        mock_forum_ops = AsyncMock()
        mock_forum_ops_cls.return_value = mock_forum_ops

        # Create a proper mock agent with iterable fields
        mock_agent = Mock()
        mock_agent.id = mock_forum_agents[0].id
        mock_agent.guild_id = "123456789012345678"
        mock_agent.name = "Python Helper"
        mock_agent.system_prompt = "You are a helpful Python programming assistant."
        mock_agent.monitored_forums = ["123456789012345678", "234567890123456789"]
        mock_agent.response_threshold = 0.7
        mock_agent.max_responses_per_hour = 5
        mock_agent.is_active = True
        mock_agent.enable_user_tagging = False
        mock_agent.enable_responses = True
        mock_agent.notification_topics = []
        mock_forum_ops.get_agent.return_value = mock_agent

        guild_id = "123456789012345678"
        agent_id = str(mock_agent.id)
        response = authenticated_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents/{agent_id}/edit")

        assert response.status_code == 200
        assert b"Edit Forum Agent" in response.content
        assert b"Python Helper" in response.content
        assert mock_agent.system_prompt.encode() in response.content

    @patch("smarter_dev.web.admin.views.ForumAgentOperations")
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_edit_post_success(self, mock_get_guild, mock_db_session,
                                         mock_forum_ops_cls, authenticated_client, mock_discord_guilds, mock_forum_agents, sample_forum_agent_data):
        """Test successful forum agent update via POST."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock ForumAgentOperations
        mock_forum_ops = AsyncMock()
        mock_forum_ops_cls.return_value = mock_forum_ops

        mock_agent = Mock()
        mock_agent.id = mock_forum_agents[0].id
        mock_agent.name = "Python Helper"
        mock_agent.enable_user_tagging = False
        mock_agent.enable_responses = True
        mock_agent.monitored_forums = ["123456789012345678"]
        mock_forum_ops.get_agent.return_value = mock_agent
        mock_forum_ops.update_agent.return_value = Mock()

        guild_id = "123456789012345678"
        agent_id = str(mock_agent.id)

        # Update data - include enable_responses for validation
        update_data = sample_forum_agent_data.copy()
        update_data["name"] = "Updated Python Helper"
        update_data["enable_responses"] = "on"

        response = authenticated_client.post(
            f"/bot-admin/guilds/{guild_id}/forum-agents/{agent_id}/edit",
            data=update_data,
            follow_redirects=False
        )

        assert response.status_code == 303
        assert f"/bot-admin/guilds/{guild_id}/forum-agents" in response.headers["location"]

        # Verify ForumAgentOperations.update_agent was called
        mock_forum_ops.update_agent.assert_called_once()

    @patch("smarter_dev.web.admin.views.ForumAgentOperations")
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_edit_not_found(self, mock_get_guild, mock_db_session,
                                      mock_forum_ops_cls, authenticated_client, mock_discord_guilds):
        """Test editing non-existent forum agent."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock ForumAgentOperations - agent not found
        mock_forum_ops = AsyncMock()
        mock_forum_ops_cls.return_value = mock_forum_ops
        mock_forum_ops.get_agent.return_value = None

        guild_id = "123456789012345678"
        agent_id = str(uuid4())
        response = authenticated_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents/{agent_id}/edit")

        assert response.status_code == 404
        assert b"Forum agent not found" in response.content


class TestForumAgentDelete:
    """Test suite for forum agent deletion."""

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_delete_success(self, mock_get_guild, mock_db_session,
                                       authenticated_client, mock_discord_guilds, mock_forum_agents):
        """Test successful forum agent deletion."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock forum agent query result
        mock_result = Mock()
        mock_agent = mock_forum_agents[0]
        mock_result.scalar_one_or_none.return_value = mock_agent
        mock_session.execute.return_value = mock_result

        guild_id = "123456789012345678"
        agent_id = str(mock_agent.id)

        response = authenticated_client.post(
            f"/bot-admin/guilds/{guild_id}/forum-agents/{agent_id}/delete",
            follow_redirects=False
        )

        assert response.status_code == 303
        assert f"/bot-admin/guilds/{guild_id}/forum-agents" in response.headers["location"]

        # Verify database operations were called
        mock_session.delete.assert_called_once_with(mock_agent)
        mock_session.commit.assert_called_once()

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_delete_not_found(self, mock_get_guild, mock_db_session,
                                        authenticated_client, mock_discord_guilds):
        """Test deleting non-existent forum agent."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock forum agent query result - not found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        guild_id = "123456789012345678"
        agent_id = str(uuid4())

        response = authenticated_client.post(f"/bot-admin/guilds/{guild_id}/forum-agents/{agent_id}/delete")

        assert response.status_code == 404
        assert b"Forum agent not found" in response.content


class TestForumAgentAnalytics:
    """Test suite for forum agent analytics view."""

    @patch("smarter_dev.web.admin.views.ForumAgentOperations")
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_analytics_success(self, mock_get_guild, mock_db_session,
                                         mock_forum_ops_cls, authenticated_client, mock_discord_guilds, mock_forum_agents):
        """Test successful forum agent analytics rendering."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock ForumAgentOperations.get_agent_analytics
        mock_forum_ops = AsyncMock()
        mock_forum_ops_cls.return_value = mock_forum_ops

        mock_agent = mock_forum_agents[0]
        from datetime import datetime, timezone

        # The view accesses agent data via dict subscript (analytics['agent']['name'])
        # AND via attribute access in the template (agent.created_at.strftime).
        # Use a dict subclass that supports both patterns.
        class DictWithAttrs(dict):
            """Dict that also supports attribute access."""
            def __getattr__(self, name):
                try:
                    return self[name]
                except KeyError:
                    raise AttributeError(name)

        mock_agent_data = DictWithAttrs(
            id=str(mock_agent.id),
            name="Python Helper",
            is_active=True,
            created_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
            system_prompt="You are a helpful Python programming assistant.",
            response_threshold=0.7,
            max_responses_per_hour=5,
            enable_responses=True,
            enable_user_tagging=False,
        )

        mock_forum_ops.get_agent_analytics.return_value = {
            "agent": mock_agent_data,
            "statistics": {
                "total_evaluations": 30,
                "total_responses": 25,
                "response_rate": 83.3,
                "total_tokens_used": 12500,
                "average_confidence": 0.85,
                "average_response_time_ms": 1250.0,
            },
            "recent_responses": [],
        }

        guild_id = "123456789012345678"
        agent_id = str(mock_agent.id)
        response = authenticated_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents/{agent_id}/analytics")

        assert response.status_code == 200
        assert b"Python Helper" in response.content

    @patch("smarter_dev.web.admin.views.ForumAgentOperations")
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_analytics_not_found(self, mock_get_guild, mock_db_session,
                                           mock_forum_ops_cls, authenticated_client, mock_discord_guilds):
        """Test analytics for non-existent forum agent."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock ForumAgentOperations - analytics returns None (not found)
        mock_forum_ops = AsyncMock()
        mock_forum_ops_cls.return_value = mock_forum_ops
        mock_forum_ops.get_agent_analytics.return_value = None

        guild_id = "123456789012345678"
        agent_id = str(uuid4())
        response = authenticated_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents/{agent_id}/analytics")

        assert response.status_code == 404
        assert b"not found" in response.content.lower()


class TestForumAgentToggle:
    """Test suite for forum agent enable/disable functionality."""

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_toggle_success(self, mock_get_guild, mock_db_session,
                                       authenticated_client, mock_discord_guilds, mock_forum_agents):
        """Test successful forum agent toggle."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock forum agent query result
        mock_result = Mock()
        mock_agent = mock_forum_agents[0]
        mock_agent.is_active = True  # Initially active
        mock_result.scalar_one_or_none.return_value = mock_agent
        mock_session.execute.return_value = mock_result

        guild_id = "123456789012345678"
        agent_id = str(mock_agent.id)

        response = authenticated_client.post(
            f"/bot-admin/guilds/{guild_id}/forum-agents/{agent_id}/toggle",
            follow_redirects=False
        )

        assert response.status_code == 303
        assert f"/bot-admin/guilds/{guild_id}/forum-agents" in response.headers["location"]

        # Verify agent was toggled to inactive
        assert mock_agent.is_active is False
        mock_session.commit.assert_called_once()

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_toggle_not_found(self, mock_get_guild, mock_db_session,
                                        authenticated_client, mock_discord_guilds):
        """Test toggling non-existent forum agent."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock forum agent query result - not found
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        guild_id = "123456789012345678"
        agent_id = str(uuid4())

        response = authenticated_client.post(f"/bot-admin/guilds/{guild_id}/forum-agents/{agent_id}/toggle")

        assert response.status_code == 404
        assert b"Forum agent not found" in response.content


class TestForumAgentBulkOperations:
    """Test suite for bulk forum agent operations."""

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agents_bulk_disable(self, mock_get_guild, mock_db_session,
                                      authenticated_client, mock_discord_guilds, mock_forum_agents):
        """Test bulk disable of forum agents."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock forum agents query result
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = mock_forum_agents
        mock_session.execute.return_value = mock_result

        guild_id = "123456789012345678"
        agent_ids = [str(agent.id) for agent in mock_forum_agents]

        response = authenticated_client.post(
            f"/bot-admin/guilds/{guild_id}/forum-agents/bulk",
            data={
                "action": "disable",
                "agent_ids": agent_ids
            },
            follow_redirects=False
        )

        assert response.status_code == 303
        assert f"/bot-admin/guilds/{guild_id}/forum-agents" in response.headers["location"]

        # Verify all agents were disabled
        for agent in mock_forum_agents:
            assert agent.is_active is False
        mock_session.commit.assert_called_once()

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agents_bulk_delete(self, mock_get_guild, mock_db_session,
                                     authenticated_client, mock_discord_guilds, mock_forum_agents):
        """Test bulk deletion of forum agents."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock forum agents query result
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = mock_forum_agents
        mock_session.execute.return_value = mock_result

        guild_id = "123456789012345678"
        agent_ids = [str(agent.id) for agent in mock_forum_agents]

        response = authenticated_client.post(
            f"/bot-admin/guilds/{guild_id}/forum-agents/bulk",
            data={
                "action": "delete",
                "agent_ids": agent_ids
            },
            follow_redirects=False
        )

        assert response.status_code == 303
        assert f"/bot-admin/guilds/{guild_id}/forum-agents" in response.headers["location"]

        # Verify all agents were deleted
        assert mock_session.delete.call_count == len(mock_forum_agents)
        mock_session.commit.assert_called_once()


class TestForumAgentErrorHandling:
    """Test suite for forum agent error handling scenarios."""

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_database_error(self, mock_get_guild, mock_db_session,
                                       authenticated_client, mock_discord_guilds):
        """Test forum agent operations with database errors."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]

        # Mock database session with error
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None

        # Mock database error
        mock_session.execute.side_effect = Exception("Database connection error")

        guild_id = "123456789012345678"
        response = authenticated_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents")

        assert response.status_code == 500
        assert b"database error" in response.content.lower()

    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_forum_agent_discord_api_error(self, mock_get_guild, authenticated_client):
        """Test forum agent operations with Discord API errors."""
        # Mock Discord API error
        mock_get_guild.side_effect = DiscordAPIError("Discord API rate limited")

        guild_id = "123456789012345678"
        response = authenticated_client.get(f"/bot-admin/guilds/{guild_id}/forum-agents")

        assert response.status_code == 503
        assert b"discord api" in response.content.lower()


class TestForumAgentValidation:
    """Test suite for forum agent data validation."""

    def test_system_prompt_validation(self, authenticated_client):
        """Test system prompt validation requirements."""
        # Test cases for system prompt validation
        test_cases = [
            ("", False, "empty prompt"),
            ("a", False, "too short"),
            ("a" * 10001, False, "too long"),
            ("Valid system prompt for testing forum agents.", True, "valid prompt"),
            ("Prompt with\nnewlines\nand special chars!", True, "multiline prompt"),
        ]

        for prompt, should_pass, description in test_cases:
            from smarter_dev.web.admin.views import validate_forum_agent_data

            data = {
                "name": "Test Agent",
                "system_prompt": prompt,
                "monitored_forums": ["123456789"],
                "response_threshold": 0.7,
                "max_responses_per_hour": 5,
                "enable_responses": "on",
            }

            is_valid, errors = validate_forum_agent_data(data)

            if should_pass:
                assert is_valid, f"Expected valid for {description}, got errors: {errors}"
            else:
                assert not is_valid, f"Expected invalid for {description}"
                # Check for system_prompt or prompt-related error
                assert any("system" in error.lower() or "prompt" in error.lower() for error in errors), f"Expected system_prompt error for {description}, got: {errors}"

    def test_threshold_validation(self, authenticated_client):
        """Test response threshold validation."""
        test_cases = [
            (-0.1, False, "negative threshold"),
            (0.0, True, "zero threshold"),
            (0.5, True, "mid threshold"),
            (1.0, True, "max threshold"),
            (1.1, False, "over max threshold"),
            ("invalid", False, "non-numeric threshold"),
        ]

        for threshold, should_pass, description in test_cases:
            from smarter_dev.web.admin.views import validate_forum_agent_data

            data = {
                "name": "Test Agent",
                "system_prompt": "Valid system prompt for testing.",
                "monitored_forums": ["123456789"],
                "response_threshold": threshold,
                "max_responses_per_hour": 5,
                "enable_responses": "on",
            }

            is_valid, errors = validate_forum_agent_data(data)

            if should_pass:
                assert is_valid, f"Expected valid for {description}, got errors: {errors}"
            else:
                assert not is_valid, f"Expected invalid for {description}"
                assert any("threshold" in error.lower() for error in errors), f"Expected threshold error for {description}"

    def test_rate_limit_validation(self, authenticated_client):
        """Test rate limit validation."""
        test_cases = [
            (-1, False, "negative rate limit"),
            (0, True, "zero rate limit (disabled)"),
            (1, True, "minimum rate limit"),
            (100, True, "reasonable rate limit"),
            (1000, False, "excessive rate limit"),
            ("invalid", False, "non-numeric rate limit"),
        ]

        for rate_limit, should_pass, description in test_cases:
            from smarter_dev.web.admin.views import validate_forum_agent_data

            data = {
                "name": "Test Agent",
                "system_prompt": "Valid system prompt for testing.",
                "monitored_forums": ["123456789"],
                "response_threshold": 0.7,
                "max_responses_per_hour": rate_limit,
                "enable_responses": "on",
            }

            is_valid, errors = validate_forum_agent_data(data)

            if should_pass:
                assert is_valid, f"Expected valid for {description}, got errors: {errors}"
            else:
                assert not is_valid, f"Expected invalid for {description}"
                assert any("rate" in error.lower() for error in errors), f"Expected rate limit error for {description}"
