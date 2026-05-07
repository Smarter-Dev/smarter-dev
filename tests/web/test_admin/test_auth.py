"""Tests for admin authentication system."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from smarter_dev.web.admin.auth import AdminAuthError


class TestAdminAuthentication:
    """Test suite for admin authentication."""

    def test_login_page_get(self, admin_client):
        """Test GET request to login page redirects to Discord OAuth."""
        response = admin_client.get("/bot-admin/login", follow_redirects=False)

        assert response.status_code == 303

    def test_login_redirect_when_authenticated(self, authenticated_client):
        """Test login page redirects to dashboard when already authenticated."""
        response = authenticated_client.get("/bot-admin/login", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/bot-admin"

    def test_login_with_next_parameter(self, authenticated_client):
        """Test login page respects next parameter when authenticated."""
        response = authenticated_client.get("/bot-admin/login?next=/bot-admin/guilds", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/bot-admin/guilds"

    def test_successful_login(self, admin_client):
        """Test POST to login returns 405 (route is GET-only, Discord OAuth)."""
        response = admin_client.post("/bot-admin/login", data={
            "username": "admin",
            "password": "password"
        })

        assert response.status_code == 405

    def test_successful_login_with_next(self, admin_client):
        """Test POST to login with next param returns 405 (route is GET-only)."""
        response = admin_client.post("/bot-admin/login?next=/bot-admin/guilds", data={
            "username": "admin",
            "password": "password"
        })

        assert response.status_code == 405

    def test_failed_login_invalid_credentials(self, admin_client):
        """Test POST to login returns 405 (route is GET-only)."""
        response = admin_client.post("/bot-admin/login", data={
            "username": "admin",
            "password": "wrong_password"
        })

        assert response.status_code == 405

    def test_failed_login_empty_credentials(self, admin_client):
        """Test POST to login with empty credentials returns 405 (route is GET-only)."""
        response = admin_client.post("/bot-admin/login", data={
            "username": "",
            "password": ""
        })

        assert response.status_code == 405

    def test_production_mode_authentication(self, admin_client):
        """Test POST to login in production returns 405 (route is GET-only)."""
        response = admin_client.post("/bot-admin/login", data={
            "username": "admin",
            "password": "password"
        })

        assert response.status_code == 405

    def test_logout(self, authenticated_client):
        """Test admin logout."""
        response = authenticated_client.post("/bot-admin/logout", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/"

    def test_admin_required_decorator_unauthenticated(self, admin_client):
        """Test admin_required decorator redirects unauthenticated users."""
        response = admin_client.get("/bot-admin/", follow_redirects=False)

        assert response.status_code == 303
        assert "/bot-admin/login" in response.headers["location"]

    def test_admin_required_decorator_authenticated(self, authenticated_client, mock_discord_api, mock_database):
        """Test admin_required decorator allows authenticated users."""
        response = authenticated_client.get("/bot-admin/")

        # Should not redirect (will get 200 or other success code)
        assert response.status_code != 303

    def test_admin_required_preserves_next_url(self, admin_client):
        """Test admin_required decorator preserves requested URL."""
        response = admin_client.get("/bot-admin/guilds", follow_redirects=False)

        assert response.status_code == 303
        location = response.headers["location"]
        assert "/bot-admin/login" in location
        assert "next=" in location
        assert "/bot-admin/guilds" in location


class TestAdminAuthHelpers:
    """Test suite for admin authentication helper functions."""

    def test_get_current_admin_authenticated(self, authenticated_client):
        """Test get_current_admin with authenticated user."""
        from smarter_dev.web.admin.auth import get_current_admin
        from unittest.mock import Mock

        # Create a mock request with session
        request = Mock()
        request.session = {"is_admin": True, "username": "test_admin"}

        result = get_current_admin(request)
        assert result == "test_admin"

    def test_get_current_admin_unauthenticated(self):
        """Test get_current_admin with unauthenticated user."""
        from smarter_dev.web.admin.auth import get_current_admin
        from unittest.mock import Mock

        request = Mock()
        request.session = {}

        result = get_current_admin(request)
        assert result is None

    def test_is_admin_authenticated_true(self):
        """Test is_admin_authenticated returns True for authenticated admin."""
        from smarter_dev.web.admin.auth import is_admin_authenticated
        from unittest.mock import Mock

        request = Mock()
        request.session = {"is_admin": True}

        result = is_admin_authenticated(request)
        assert result is True

    def test_is_admin_authenticated_false(self):
        """Test is_admin_authenticated returns False for unauthenticated user."""
        from smarter_dev.web.admin.auth import is_admin_authenticated
        from unittest.mock import Mock

        request = Mock()
        request.session = {}

        result = is_admin_authenticated(request)
        assert result is False


class TestAdminAuthExceptions:
    """Test suite for admin authentication exceptions."""

    def test_admin_auth_error_creation(self):
        """Test AdminAuthError can be created and raised."""
        error = AdminAuthError("Test error message")
        assert str(error) == "Test error message"

        with pytest.raises(AdminAuthError, match="Test error message"):
            raise error
