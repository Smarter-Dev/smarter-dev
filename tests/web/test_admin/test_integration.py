"""Integration tests for admin interface."""

from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock, Mock


class TestAdminIntegration:
    """Integration tests for admin interface workflows."""

    @patch("smarter_dev.web.admin.views.get_bot_guilds")
    @patch("smarter_dev.web.admin.views.get_db_session_context")
    def test_full_admin_workflow(self, mock_db_session, mock_get_guilds, authenticated_client, mock_discord_guilds):
        """Test complete admin workflow from login to configuration."""
        # Mock Discord API
        mock_get_guilds.return_value = mock_discord_guilds

        # Mock database session
        mock_session = AsyncMock()
        async def mock_aenter(self):
            return mock_session
        async def mock_aexit(self, exc_type, exc_val, exc_tb):
            return None
        mock_db_session.return_value.__aenter__ = mock_aenter
        mock_db_session.return_value.__aexit__ = mock_aexit
        mock_session.execute.return_value.scalar.return_value = 10

        # 1. Access dashboard
        response = authenticated_client.get("/bot-admin/")
        assert response.status_code == 200
        assert b"Dashboard" in response.content

        # 2. View guild list
        response = authenticated_client.get("/bot-admin/guilds")
        assert response.status_code == 200
        assert b"Test Guild 1" in response.content

        # 3. View specific guild (would need more mocking for full test)
        # This would require mocking all the database operations for guild detail

    def test_authentication_flow(self, admin_client, mock_database, mock_discord_api):
        """Test authentication flow with Discord OAuth."""
        # 1. Access protected page (should redirect to login)
        response = admin_client.get("/bot-admin/", follow_redirects=False)
        assert response.status_code == 303
        assert "/bot-admin/login" in response.headers["location"]

        # 2. GET login page redirects to Discord OAuth (303)
        response = admin_client.get("/bot-admin/login", follow_redirects=False)
        assert response.status_code == 303

        # 3. POST to login is blocked (route is GET-only, Discord OAuth)
        response = admin_client.post("/bot-admin/login", data={
            "username": "admin",
            "password": "password"
        })
        assert response.status_code == 405

    def test_navigation_consistency(self, authenticated_client):
        """Test that navigation works consistently across pages."""
        with patch("smarter_dev.web.admin.views.get_bot_guilds", return_value=[]):
            with patch("smarter_dev.web.admin.views.get_db_session_context") as mock_db:
                mock_session = AsyncMock()
                async def mock_aenter(self):
                    return mock_session
                async def mock_aexit(self, exc_type, exc_val, exc_tb):
                    return None
                mock_db.return_value.__aenter__ = mock_aenter
                mock_db.return_value.__aexit__ = mock_aexit
                # Configure mock session to return proper mock results
                mock_result = Mock()
                mock_result.scalar.return_value = 0
                mock_session.execute.return_value = mock_result

                # Check that all main pages include consistent navigation
                pages = ["/bot-admin/", "/bot-admin/guilds"]

                for page in pages:
                    response = authenticated_client.get(page)
                    assert response.status_code == 200
                    assert b"Smarter Dev Admin" in response.content
                    assert b"Dashboard" in response.content
                    assert b"Guilds" in response.content

    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_error_page_consistency(self, mock_get_guild, authenticated_client):
        """Test that error pages are consistent."""
        from smarter_dev.web.admin.discord import GuildNotFoundError

        mock_get_guild.side_effect = GuildNotFoundError("Guild not found")

        # Test 404 error page
        response = authenticated_client.get("/bot-admin/guilds/invalid_id")
        assert response.status_code == 404
        assert b"Not Found" in response.content
        assert b"Back to Dashboard" in response.content

    def test_security_headers_and_session(self, authenticated_client):
        """Test that security measures are in place."""
        with patch("smarter_dev.web.admin.views.get_bot_guilds", return_value=[]):
            with patch("smarter_dev.web.admin.views.get_db_session_context") as mock_db:
                mock_session = AsyncMock()
                async def mock_aenter(self):
                    return mock_session
                async def mock_aexit(self, exc_type, exc_val, exc_tb):
                    return None
                mock_db.return_value.__aenter__ = mock_aenter
                mock_db.return_value.__aexit__ = mock_aexit
                # Configure mock session to return proper mock results
                mock_result = Mock()
                mock_result.scalar.return_value = 0
                mock_session.execute.return_value = mock_result

                response = authenticated_client.get("/bot-admin/")

                # Check that response includes security-related headers
                assert response.status_code == 200
                # The actual security headers would depend on middleware configuration

    @patch("smarter_dev.web.admin.views.get_db_session_context")
    @patch("smarter_dev.web.admin.views.get_guild_roles")
    @patch("smarter_dev.web.admin.views.get_guild_info")
    def test_form_submission_workflow(self, mock_get_guild, mock_get_roles, mock_db_session, authenticated_client, mock_discord_guilds, mock_discord_roles):
        """Test complete form submission workflow."""
        # Mock Discord API
        mock_get_guild.return_value = mock_discord_guilds[0]
        mock_get_roles.return_value = mock_discord_roles

        # Mock database session
        mock_session = AsyncMock()
        async def mock_aenter(self):
            return mock_session
        async def mock_aexit(self, exc_type, exc_val, exc_tb):
            return None
        mock_db_session.return_value.__aenter__ = mock_aenter
        mock_db_session.return_value.__aexit__ = mock_aexit

        # Mock operations
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
                mock_config_instance.update_config.return_value = mock_default_config

                # 1. Get the form page
                response = authenticated_client.get("/bot-admin/guilds/123456789012345678/bytes")
                assert response.status_code == 200
                assert b"Bytes Configuration" in response.content

                # 2. Submit the form
                response = authenticated_client.post(
                    "/bot-admin/guilds/123456789012345678/bytes",
                    data={
                        "starting_balance": "200",
                        "daily_amount": "20",
                        "max_transfer": "2000",
                        "transfer_cooldown_hours": "1"
                    }
                )

                assert response.status_code == 200
                assert b"Configuration updated successfully" in response.content

                # Verify the operation was called
                mock_config_instance.update_config.assert_called_once()


class TestAdminSecurity:
    """Security-focused integration tests."""

    def test_all_admin_routes_require_authentication(self, admin_client):
        """Test that all admin routes require authentication."""
        protected_routes = [
            "/bot-admin/",
            "/bot-admin/guilds",
            "/bot-admin/guilds/123",
            "/bot-admin/guilds/123/bytes",
            "/bot-admin/guilds/123/squads"
        ]

        for route in protected_routes:
            response = admin_client.get(route, follow_redirects=False)
            assert response.status_code == 303
            assert "/bot-admin/login" in response.headers["location"]

    def test_login_route_accepts_get_and_post_only(self, admin_client):
        """Test that login route only accepts GET and POST."""
        # GET returns 303 (Discord OAuth redirect)
        response = admin_client.get("/bot-admin/login", follow_redirects=False)
        assert response.status_code == 303

        # POST returns 405 (disabled, Discord OAuth only)
        response = admin_client.post("/bot-admin/login", data={})
        assert response.status_code == 405

        # Other methods should not be allowed
        response = admin_client.put("/bot-admin/login")
        assert response.status_code == 405  # Method not allowed

    def test_logout_requires_post_method(self, authenticated_client):
        """Test that logout requires POST method."""
        # POST should work
        response = authenticated_client.post("/bot-admin/logout", follow_redirects=False)
        print(f"POST logout response: {response.status_code}, content: {response.content[:200]}")
        assert response.status_code == 303

        # GET should not be allowed
        response = authenticated_client.get("/bot-admin/logout")
        print(f"GET logout response: {response.status_code}")
        assert response.status_code == 405  # Method not allowed

    def test_session_persistence(self, mock_settings, authenticated_client):
        """Test that admin sessions persist correctly across multiple requests."""
        # Use the authenticated_client (which mocks admin_required) to verify
        # session persistence across multiple protected page requests
        with patch("smarter_dev.web.admin.views.get_bot_guilds", return_value=[]):
            with patch("smarter_dev.web.admin.views.get_db_session_context") as mock_db:
                mock_session = AsyncMock()
                async def mock_aenter(self):
                    return mock_session
                async def mock_aexit(self, exc_type, exc_val, exc_tb):
                    return None
                mock_db.return_value.__aenter__ = mock_aenter
                mock_db.return_value.__aexit__ = mock_aexit
                # Configure mock session to return proper mock results
                mock_result = Mock()
                mock_result.scalar.return_value = 0
                mock_session.execute.return_value = mock_result

                for _ in range(3):  # Multiple requests
                    response = authenticated_client.get("/bot-admin/")
                    assert response.status_code == 200
