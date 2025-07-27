"""Tests for admin authentication system."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from smarter_dev.web.admin.auth import AdminAuthError


class TestAdminAuthentication:
    """Test suite for admin authentication."""
    
    def test_login_page_get(self, admin_client):
        """Test GET request to login page."""
        response = admin_client.get("/admin/login")
        
        assert response.status_code == 200
        assert b"Admin Login" in response.content
        assert b"Username" in response.content
        assert b"Password" in response.content
    
    def test_login_redirect_when_authenticated(self, authenticated_client):
        """Test login page redirects when already authenticated."""
        response = authenticated_client.get("/admin/login", follow_redirects=False)
        
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"
    
    def test_login_with_next_parameter(self, authenticated_client):
        """Test login page respects next parameter."""
        response = authenticated_client.get("/admin/login?next=/admin/guilds", follow_redirects=False)
        
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/guilds"
    
    @patch("smarter_dev.web.admin.auth.get_settings")
    def test_successful_login(self, mock_settings, admin_client):
        """Test successful admin login."""
        mock_settings.return_value.is_development = True
        mock_settings.return_value.admin_username = "admin"
        mock_settings.return_value.admin_password = "password"
        
        response = admin_client.post("/admin/login", data={
            "username": "admin",
            "password": "password"
        }, follow_redirects=False)
        
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"
    
    @patch("smarter_dev.web.admin.auth.get_settings")
    def test_successful_login_with_next(self, mock_settings, admin_client):
        """Test successful login redirects to next URL."""
        mock_settings.return_value.is_development = True
        mock_settings.return_value.admin_username = "admin"
        mock_settings.return_value.admin_password = "password"
        
        response = admin_client.post("/admin/login?next=/admin/guilds", data={
            "username": "admin",
            "password": "password"
        }, follow_redirects=False)
        
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/guilds"
    
    @patch("smarter_dev.web.admin.auth.get_settings")
    def test_failed_login_invalid_credentials(self, mock_settings, admin_client):
        """Test failed login with invalid credentials."""
        mock_settings.return_value.is_development = True
        mock_settings.return_value.admin_username = "admin"
        mock_settings.return_value.admin_password = "password"
        
        response = admin_client.post("/admin/login", data={
            "username": "admin",
            "password": "wrong_password"
        })
        
        assert response.status_code == 400
        assert b"Invalid username or password" in response.content
    
    @patch("smarter_dev.web.admin.auth.get_settings")
    def test_failed_login_empty_credentials(self, mock_settings, admin_client):
        """Test failed login with empty credentials."""
        mock_settings.return_value.is_development = True
        
        response = admin_client.post("/admin/login", data={
            "username": "",
            "password": ""
        })
        
        assert response.status_code == 400
        assert b"Username and password are required" in response.content
    
    @patch("smarter_dev.web.admin.auth.get_settings")
    def test_production_mode_authentication(self, mock_settings, admin_client):
        """Test authentication in production mode."""
        mock_settings.return_value.is_development = False
        
        response = admin_client.post("/admin/login", data={
            "username": "admin",
            "password": "password"
        })
        
        assert response.status_code == 400
        assert b"Production authentication not yet implemented" in response.content
    
    def test_logout(self, authenticated_client):
        """Test admin logout."""
        response = authenticated_client.post("/admin/logout", follow_redirects=False)
        
        assert response.status_code == 303
        assert response.headers["location"] == "/"
    
    def test_admin_required_decorator_unauthenticated(self, admin_client):
        """Test admin_required decorator redirects unauthenticated users."""
        response = admin_client.get("/admin/", follow_redirects=False)
        
        assert response.status_code == 303
        assert "/admin/login" in response.headers["location"]
    
    def test_admin_required_decorator_authenticated(self, authenticated_client, mock_discord_api, mock_database):
        """Test admin_required decorator allows authenticated users."""
        response = authenticated_client.get("/admin/")
        
        # Should not redirect (will get 200 or other success code)
        assert response.status_code != 303
    
    def test_admin_required_preserves_next_url(self, admin_client):
        """Test admin_required decorator preserves requested URL."""
        response = admin_client.get("/admin/guilds", follow_redirects=False)
        
        assert response.status_code == 303
        location = response.headers["location"]
        assert "/admin/login" in location
        assert "next=" in location
        assert "/admin/guilds" in location


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