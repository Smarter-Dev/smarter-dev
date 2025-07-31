"""Tests for admin interface views."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from smarter_dev.web.admin.discord import GuildNotFoundError, DiscordAPIError


class TestAdminDashboard:
    """Test suite for admin dashboard view."""
    
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_bot_guilds")
    def test_dashboard_success(self, mock_get_guilds, mock_db_session, authenticated_client):
        """Test successful dashboard rendering."""
        # Mock Discord API
        mock_get_guilds.return_value = []
        
        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None
        
        # Mock database queries
        mock_session.execute.return_value.scalar.return_value = 10
        
        response = authenticated_client.get("/admin/")
        
        assert response.status_code == 200
        assert b"Dashboard" in response.content
        assert b"Total Users" in response.content
    
    @patch("smarter_dev.web.admin.views.get_bot_guilds")
    def test_dashboard_discord_api_error(self, mock_get_guilds, authenticated_client):
        """Test dashboard with Discord API error."""
        mock_get_guilds.side_effect = DiscordAPIError("API error")
        
        response = authenticated_client.get("/admin/")
        
        assert response.status_code == 200
        assert b"Discord API error" in response.content
    
    def test_dashboard_requires_authentication(self, admin_client):
        """Test dashboard requires authentication."""
        response = admin_client.get("/admin/", follow_redirects=False)
        
        assert response.status_code == 303
        assert "/admin/login" in response.headers["location"]


class TestGuildList:
    """Test suite for guild list view."""
    
    @patch("smarter_dev.web.admin.views.get_bot_guilds")
    def test_guild_list_success(self, mock_get_guilds, authenticated_client, mock_discord_guilds):
        """Test successful guild list rendering."""
        mock_get_guilds.return_value = mock_discord_guilds
        
        response = authenticated_client.get("/admin/guilds")
        
        assert response.status_code == 200
        assert b"Guild Management" in response.content
        assert b"Test Guild 1" in response.content
    
    @patch("smarter_dev.web.admin.views.get_bot_guilds", new_callable=AsyncMock)
    def test_guild_list_discord_error(self, mock_get_guilds, authenticated_client):
        """Test guild list with Discord API error."""
        # Configure AsyncMock for async function  
        mock_get_guilds.side_effect = DiscordAPIError("API error")
        
        response = authenticated_client.get("/admin/guilds")
        
        assert response.status_code == 200
        assert b"Discord API error" in response.content
    
    def test_guild_list_requires_authentication(self, admin_client):
        """Test guild list requires authentication."""
        response = admin_client.get("/admin/guilds", follow_redirects=False)
        
        assert response.status_code == 303
        assert "/admin/login" in response.headers["location"]


class TestGuildDetail:
    """Test suite for guild detail view."""
    
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_guild_detail_success(self, mock_get_guild, mock_db_session, authenticated_client, mock_discord_guilds):
        """Test successful guild detail rendering."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        
        # Mock database session and operations
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None
        
        # Mock operations
        with patch("smarter_dev.web.admin.views.BytesOperations") as mock_bytes_ops:
            with patch("smarter_dev.web.admin.views.BytesConfigOperations") as mock_config_ops:
                with patch("smarter_dev.web.admin.views.SquadOperations") as mock_squad_ops:
                    mock_bytes_instance = AsyncMock()
                    mock_config_instance = AsyncMock()
                    mock_squad_instance = AsyncMock()
                    mock_bytes_ops.return_value = mock_bytes_instance
                    mock_config_ops.return_value = mock_config_instance
                    mock_squad_ops.return_value = mock_squad_instance
                    
                    mock_bytes_instance.get_leaderboard.return_value = []
                    mock_config_instance.get_config.return_value = None
                    mock_squad_instance.get_guild_squads.return_value = []
                    
                    # Mock database execute results
                    # The view makes multiple execute calls, so set up side_effect
                    mock_transaction_result = Mock()
                    mock_transaction_result.scalars.return_value.all.return_value = []
                    
                    mock_stats_result = Mock()
                    mock_stats_result.first.return_value = Mock(
                        total_users=10,
                        total_balance=1000,
                        total_transactions=50
                    )
                    
                    # Return transaction result first, then stats result
                    mock_session.execute.side_effect = [mock_transaction_result, mock_stats_result]
                    
                    response = authenticated_client.get("/admin/guilds/123456789012345678")
                    
                    assert response.status_code == 200
                    assert b"Test Guild 1" in response.content
    
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_guild_detail_not_found(self, mock_get_guild, authenticated_client):
        """Test guild detail with guild not found."""
        mock_get_guild.side_effect = GuildNotFoundError("Guild not found")
        
        response = authenticated_client.get("/admin/guilds/invalid_guild_id")
        
        assert response.status_code == 404
        assert b"Guild invalid_guild_id not found" in response.content
    
    def test_guild_detail_requires_authentication(self, admin_client):
        """Test guild detail requires authentication."""
        response = admin_client.get("/admin/guilds/123456789012345678", follow_redirects=False)
        
        assert response.status_code == 303
        assert "/admin/login" in response.headers["location"]


class TestBytesConfig:
    """Test suite for bytes configuration view."""
    
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_bytes_config_get_success(self, mock_get_guild, mock_db_session, authenticated_client, mock_discord_guilds):
        """Test successful bytes config GET request."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        
        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None
        
        # Mock bytes operations
        with patch("smarter_dev.web.admin.views.BytesConfigOperations") as mock_config_ops:
            with patch("smarter_dev.web.admin.views.BytesConfig") as mock_config_model:
                mock_config_instance = AsyncMock()
                mock_config_ops.return_value = mock_config_instance
                
                # Mock a valid config object for get_defaults
                mock_default_config = Mock()
                mock_default_config.starting_balance = 100
                mock_default_config.daily_amount = 10
                mock_default_config.max_transfer = 1000
                mock_default_config.transfer_cooldown_hours = 0
                mock_default_config.streak_bonuses = {8: 2, 16: 4, 32: 8}
                mock_default_config.role_rewards = {}
                mock_config_model.get_defaults.return_value = mock_default_config
                
                # Mock get_config to raise exception so it falls back to get_defaults
                mock_config_instance.get_config.side_effect = Exception("Config not found")
                mock_config_instance.create_config.side_effect = Exception("Create failed")
                
                response = authenticated_client.get("/admin/guilds/123456789012345678/bytes")
                
                assert response.status_code == 200
                assert b"Bytes Configuration" in response.content
    
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_bytes_config_post_success(self, mock_get_guild, mock_db_session, authenticated_client, mock_discord_guilds, sample_form_data):
        """Test successful bytes config POST request."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        
        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None
        
        # Mock bytes operations
        with patch("smarter_dev.web.admin.views.BytesConfigOperations") as mock_config_ops:
            with patch("smarter_dev.web.admin.views.get_redis_client") as mock_redis:
                mock_config_instance = AsyncMock()
                mock_config_ops.return_value = mock_config_instance
                
                # Mock successful config update
                mock_updated_config = Mock()
                mock_updated_config.starting_balance = 200
                mock_updated_config.daily_amount = 20
                mock_updated_config.max_transfer = 2000
                mock_updated_config.transfer_cooldown_hours = 1
                mock_updated_config.streak_bonuses = {8: 2, 16: 4, 32: 8}
                mock_updated_config.role_rewards = {}
                mock_config_instance.update_config.return_value = mock_updated_config
                
                # Mock Redis client
                mock_redis_client = AsyncMock()
                mock_redis.return_value = mock_redis_client
                mock_redis_client.publish.return_value = None
                
                response = authenticated_client.post(
                    "/admin/guilds/123456789012345678/bytes",
                    data=sample_form_data
                )
                
                assert response.status_code == 200
                assert b"Configuration updated successfully" in response.content
    
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_bytes_config_guild_not_found(self, mock_get_guild, authenticated_client):
        """Test bytes config with guild not found."""
        mock_get_guild.side_effect = GuildNotFoundError("Guild not found")
        
        response = authenticated_client.get("/admin/guilds/invalid_guild_id/bytes")
        
        assert response.status_code == 404
        assert b"Guild invalid_guild_id not found" in response.content
    
    def test_bytes_config_requires_authentication(self, admin_client):
        """Test bytes config requires authentication."""
        response = admin_client.get("/admin/guilds/123456789012345678/bytes", follow_redirects=False)
        
        assert response.status_code == 303
        assert "/admin/login" in response.headers["location"]


class TestSquadsConfig:
    """Test suite for squads configuration view."""
    
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_roles")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_squads_config_get_success(self, mock_get_guild, mock_get_roles, mock_db_session, authenticated_client, mock_discord_guilds, mock_discord_roles):
        """Test successful squads config GET request."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        mock_get_roles.return_value = mock_discord_roles
        
        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None
        
        # Mock squad operations
        with patch("smarter_dev.web.admin.views.SquadOperations") as mock_ops:
            mock_instance = AsyncMock()
            mock_ops.return_value = mock_instance
            mock_instance.list_squads.return_value = []
            
            response = authenticated_client.get("/admin/guilds/123456789012345678/squads")
            
            assert response.status_code == 200
            assert b"Squad Management" in response.content
    
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_roles")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_squads_config_create_success(self, mock_get_guild, mock_get_roles, mock_db_session, authenticated_client, mock_discord_guilds, mock_discord_roles, sample_squad_data):
        """Test successful squad creation."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        mock_get_roles.return_value = mock_discord_roles
        
        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None
        
        # Mock squad operations
        with patch("smarter_dev.web.admin.views.SquadOperations") as mock_ops:
            mock_instance = AsyncMock()
            mock_ops.return_value = mock_instance
            mock_instance.create_squad.return_value = Mock()
            mock_instance.list_squads.return_value = []
            
            response = authenticated_client.post(
                "/admin/guilds/123456789012345678/squads",
                data=sample_squad_data
            )
            
            assert response.status_code == 200
            assert b"Squad created successfully" in response.content
    
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_roles")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_squads_config_update_success(self, mock_get_guild, mock_get_roles, mock_db_session, authenticated_client, mock_discord_guilds, mock_discord_roles):
        """Test successful squad update."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        mock_get_roles.return_value = mock_discord_roles
        
        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None
        
        # Mock squad operations
        with patch("smarter_dev.web.admin.views.SquadOperations") as mock_ops:
            mock_instance = AsyncMock()
            mock_ops.return_value = mock_instance
            mock_instance.update_squad.return_value = Mock()
            mock_instance.list_squads.return_value = []
            
            squad_id = str(uuid4())
            response = authenticated_client.post(
                "/admin/guilds/123456789012345678/squads",
                data={
                    "action": "update",
                    "squad_id": squad_id,
                    "name": "Updated Squad",
                    "switch_cost": "75",
                    "is_active": "on"
                }
            )
            
            assert response.status_code == 200
            assert b"Squad updated successfully" in response.content
    
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_roles")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_squads_config_delete_success(self, mock_get_guild, mock_get_roles, mock_db_session, authenticated_client, mock_discord_guilds, mock_discord_roles):
        """Test successful squad deletion."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        mock_get_roles.return_value = mock_discord_roles
        
        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None
        
        # Mock squad operations
        with patch("smarter_dev.web.admin.views.SquadOperations") as mock_ops:
            mock_instance = AsyncMock()
            mock_ops.return_value = mock_instance
            mock_instance.delete_squad.return_value = Mock()
            mock_instance.list_squads.return_value = []
            
            squad_id = str(uuid4())
            response = authenticated_client.post(
                "/admin/guilds/123456789012345678/squads",
                data={
                    "action": "delete",
                    "squad_id": squad_id
                }
            )
            
            assert response.status_code == 200
            assert b"Squad deleted successfully" in response.content
    
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_squads_config_guild_not_found(self, mock_get_guild, authenticated_client):
        """Test squads config with guild not found."""
        mock_get_guild.side_effect = GuildNotFoundError("Guild not found")
        
        response = authenticated_client.get("/admin/guilds/invalid_guild_id/squads")
        
        assert response.status_code == 404
        assert b"Guild invalid_guild_id not found" in response.content
    
    def test_squads_config_requires_authentication(self, admin_client):
        """Test squads config requires authentication."""
        response = admin_client.get("/admin/guilds/123456789012345678/squads", follow_redirects=False)
        
        assert response.status_code == 303
        assert "/admin/login" in response.headers["location"]


class TestViewErrorHandling:
    """Test suite for view error handling."""
    
    @patch("smarter_dev.web.admin.views.get_bot_guilds")
    def test_dashboard_unexpected_error(self, mock_get_guilds, authenticated_client):
        """Test dashboard with unexpected error."""
        mock_get_guilds.side_effect = Exception("Unexpected error")
        
        response = authenticated_client.get("/admin/")
        
        assert response.status_code == 200
        assert b"An unexpected error occurred" in response.content
    
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_guild_detail_unexpected_error(self, mock_get_guild, authenticated_client):
        """Test guild detail with unexpected error."""
        mock_get_guild.side_effect = Exception("Unexpected error")
        
        response = authenticated_client.get("/admin/guilds/123456789012345678")
        
        assert response.status_code == 500
        assert b"An unexpected error occurred" in response.content
    
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_bytes_config_invalid_form_data(self, mock_get_guild, mock_db_session, authenticated_client, mock_discord_guilds):
        """Test bytes config with invalid form data."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        
        # Mock database session
        mock_session = AsyncMock()
        mock_db_session.return_value.__aenter__.return_value = mock_session
        mock_db_session.return_value.__aexit__.return_value = None
        
        # Mock bytes operations
        with patch("smarter_dev.web.admin.views.BytesConfigOperations") as mock_config_ops:
            with patch("smarter_dev.web.admin.views.BytesConfig") as mock_config_model:
                mock_config_instance = AsyncMock()
                mock_config_ops.return_value = mock_config_instance
                
                # Mock a valid config object for get_defaults fallback
                mock_default_config = Mock()
                mock_default_config.starting_balance = 100
                mock_default_config.daily_amount = 10
                mock_default_config.max_transfer = 1000
                mock_default_config.transfer_cooldown_hours = 0
                mock_default_config.streak_bonuses = {8: 2, 16: 4, 32: 8}
                mock_default_config.role_rewards = {}
                mock_config_model.get_defaults.return_value = mock_default_config
                
                # Mock get_config to raise exception so it falls back to get_defaults
                mock_config_instance.get_config.side_effect = Exception("Config not found")
                
                response = authenticated_client.post(
                    "/admin/guilds/123456789012345678/bytes",
                    data={
                        "starting_balance": "invalid",  # Invalid data
                        "daily_amount": "10"
                    }
                )
                
                assert response.status_code == 400
                assert b"Invalid configuration values" in response.content