"""Tests for authentication endpoints."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from httpx import AsyncClient


class TestAuthValidation:
    """Test authentication API key validation."""
    
    async def test_validate_token_success(
        self,
        real_api_client: AsyncClient,
        bot_headers: dict[str, str]
    ):
        """Test successful API key validation."""
        response = await real_api_client.post("/auth/validate", headers=bot_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["expires_at"] is None
    
    async def test_validate_token_missing_header(self, real_api_client: AsyncClient):
        """Test API key validation without authorization header."""
        response = await real_api_client.post("/auth/validate")
        
        assert response.status_code == 403
    
    async def test_validate_token_invalid_format(self, real_api_client: AsyncClient):
        """Test API key validation with invalid header format."""
        headers = {"Authorization": "InvalidFormat token"}
        response = await real_api_client.post("/auth/validate", headers=headers)
        
        assert response.status_code == 403
    
    async def test_validate_token_invalid_token(
        self,
        real_api_client: AsyncClient,
        invalid_headers: dict[str, str]
    ):
        """Test API key validation with invalid token."""
        response = await real_api_client.post("/auth/validate", headers=invalid_headers)
        
        assert response.status_code == 401
        data = response.json()
        assert "Invalid API key format" in data["detail"]
    
    async def test_validate_token_empty_token(self, real_api_client: AsyncClient):
        """Test API key validation with empty token."""
        headers = {"Authorization": "Bearer "}
        response = await real_api_client.post("/auth/validate", headers=headers)
        
        assert response.status_code == 403  # Empty token returns 403 (from HTTPBearer)


class TestAuthHealthCheck:
    """Test authentication health check endpoint."""
    
    async def test_health_check_success(self, real_api_client: AsyncClient):
        """Test successful health check."""
        response = await api_client.get("/auth/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "1.0.0"
        assert isinstance(data["timestamp"], str)
        assert data["database"] is True
        assert data["redis"] is True
    
    async def test_health_check_no_token_configured(self, real_api_client: AsyncClient):
        """Test health check when no bot token is configured."""
        with patch('smarter_dev.web.api.routers.auth.get_settings') as mock_settings:
            mock_settings.return_value.discord_bot_token = ""
            
            response = await api_client.get("/auth/health")
            
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "degraded"


class TestAuthStatus:
    """Test authentication status endpoint."""
    
    async def test_auth_status_success(
        self,
        real_api_client: AsyncClient,
        bot_headers: dict[str, str]
    ):
        """Test successful authentication status."""
        response = await real_api_client.get("/auth/status", headers=bot_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is True
        assert data["key_name"] == "Test API Key"
        assert data["environment"] == "testing"
        assert data["api_version"] == "1.0.0"
        assert "timestamp" in data
    
    async def test_auth_status_unauthorized(self, real_api_client: AsyncClient):
        """Test authentication status without API key."""
        response = await real_api_client.get("/auth/status")
        
        assert response.status_code == 403
    
    async def test_auth_status_invalid_token(
        self,
        real_api_client: AsyncClient,
        invalid_headers: dict[str, str]
    ):
        """Test authentication status with invalid API key."""
        response = await real_api_client.get("/auth/status", headers=invalid_headers)
        
        assert response.status_code == 401


class TestAuthErrors:
    """Test authentication error handling."""
    
    async def test_malformed_bearer_token(self, real_api_client: AsyncClient):
        """Test handling of malformed bearer token."""
        headers = {"Authorization": "Bearer"}  # Missing token part
        response = await api_client.post("/auth/validate", headers=headers)
        
        assert response.status_code == 403
    
    async def test_non_bearer_auth(self, real_api_client: AsyncClient):
        """Test handling of non-bearer authentication."""
        headers = {"Authorization": "Basic dGVzdDp0ZXN0"}
        response = await api_client.post("/auth/validate", headers=headers)
        
        assert response.status_code == 403
    
    async def test_case_sensitive_bearer(self, real_api_client: AsyncClient):
        """Test that bearer token is case sensitive."""
        headers = {"Authorization": "bearer test_bot_token_12345"}
        response = await api_client.post("/auth/validate", headers=headers)
        
        assert response.status_code == 403
    
    async def test_extra_spaces_in_header(self, real_api_client: AsyncClient):
        """Test handling of extra spaces in authorization header."""
        headers = {"Authorization": "Bearer  test_bot_token_12345  "}
        response = await api_client.post("/auth/validate", headers=headers)
        
        # The token should be stripped, so this should fail due to extra spaces
        assert response.status_code == 401


class TestAuthIntegration:
    """Test authentication integration scenarios."""
    
    async def test_auth_flow_complete(
        self,
        real_api_client: AsyncClient,
        bot_headers: dict[str, str]
    ):
        """Test complete authentication flow."""
        # First validate the token
        validate_response = await api_client.post(
            "/auth/validate", 
            headers=bot_headers
        )
        assert validate_response.status_code == 200
        assert validate_response.json()["valid"] is True
        
        # Then check status
        status_response = await api_client.get(
            "/auth/status", 
            headers=bot_headers
        )
        assert status_response.status_code == 200
        assert status_response.json()["authenticated"] is True
        
        # Finally check health
        health_response = await api_client.get("/auth/health")
        assert health_response.status_code == 200
        assert health_response.json()["status"] == "healthy"
    
    async def test_concurrent_auth_requests(
        self,
        real_api_client: AsyncClient,
        bot_headers: dict[str, str]
    ):
        """Test concurrent authentication requests."""
        import asyncio
        
        # Make multiple concurrent requests
        tasks = [
            api_client.post("/auth/validate", headers=bot_headers)
            for _ in range(5)
        ]
        
        responses = await asyncio.gather(*tasks)
        
        # All should succeed
        for response in responses:
            assert response.status_code == 200
            assert response.json()["valid"] is True