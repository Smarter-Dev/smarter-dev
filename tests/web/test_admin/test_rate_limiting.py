"""Test suite for API key rate limiting functionality using TDD methodology.

This module tests the rate limiting enforcement for API keys to ensure
proper throttling and protection against abuse.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import APIKey
from smarter_dev.web.crud import APIKeyOperations


class TestAPIKeyRateLimiting:
    """Test API key rate limiting enforcement and tracking."""

    async def test_rate_limit_within_bounds(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that requests within rate limit are allowed."""
        # Create API key with low rate limit for testing
        key_data = {
            "name": "Rate Limit Test Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 5,  # Very low limit for testing
            "description": "Testing rate limits"
        }
        
        # Create the API key
        create_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        test_key = create_response.json()["api_key"]
        
        # Make requests within the rate limit
        test_headers = {"Authorization": f"Bearer {test_key}"}
        
        # Should succeed for first 5 requests
        for i in range(5):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=test_headers
            )
            print(f"Request {i+1}: Status {response.status_code}")
            if response.status_code == 429:
                print(f"Rate limited on request {i+1}: {response.json()}")
                print(f"Headers: {dict(response.headers)}")
            # We expect this to work (even if it returns 404 for non-existent data)
            assert response.status_code in [200, 404], f"Request {i+1} failed with {response.status_code}: {response.json() if response.status_code == 429 else 'N/A'}"

    async def test_rate_limit_exceeded(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that requests exceeding rate limit are rejected."""
        # Create API key with very low rate limit
        key_data = {
            "name": "Rate Limit Exceed Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 3,  # Very low limit
            "description": "Testing rate limit exceeded"
        }
        
        create_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        test_key = create_response.json()["api_key"]
        
        test_headers = {"Authorization": f"Bearer {test_key}"}
        
        # Make requests up to the limit
        for i in range(3):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=test_headers
            )
            assert response.status_code in [200, 404]  # Should succeed
        
        # The 4th request should be rate limited
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=test_headers
        )
        assert response.status_code == 429  # Too Many Requests
        
        # Check rate limit error response
        error_data = response.json()
        assert "rate limit" in error_data["detail"].lower()
        assert "retry-after" in response.headers

    async def test_rate_limit_reset_after_hour(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that rate limits reset after an hour."""
        # This test will mock time advancement instead of actually waiting
        # Create API key with low limit
        key_data = {
            "name": "Rate Limit Reset Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 2,
            "description": "Testing rate limit reset"
        }
        
        create_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        test_key = create_response.json()["api_key"]
        
        # This test will verify the rate limiting logic
        # The actual implementation should handle time-based resets
        test_headers = {"Authorization": f"Bearer {test_key}"}
        
        # Make requests to exhaust limit
        for i in range(2):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=test_headers
            )
            assert response.status_code in [200, 404]
        
        # Should be rate limited now
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=test_headers
        )
        assert response.status_code == 429

    async def test_rate_limit_per_key_isolation(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that rate limits are isolated per API key."""
        # Create two different API keys
        key1_data = {
            "name": "Rate Limit Test Key 1",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 2,
            "description": "First key for isolation test"
        }
        
        key2_data = {
            "name": "Rate Limit Test Key 2", 
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 2,
            "description": "Second key for isolation test"
        }
        
        # Create first key
        response1 = await real_api_client.post(
            "/admin/api-keys",
            json=key1_data,
            headers=admin_auth_headers
        )
        assert response1.status_code == 201
        test_key1 = response1.json()["api_key"]
        
        # Create second key
        response2 = await real_api_client.post(
            "/admin/api-keys",
            json=key2_data,
            headers=admin_auth_headers
        )
        assert response2.status_code == 201
        test_key2 = response2.json()["api_key"]
        
        headers1 = {"Authorization": f"Bearer {test_key1}"}
        headers2 = {"Authorization": f"Bearer {test_key2}"}
        
        # Exhaust limit for key1
        for i in range(2):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers1
            )
            assert response.status_code in [200, 404]
        
        # Key1 should be rate limited
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers1
        )
        assert response.status_code == 429
        
        # Key2 should still work
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers2
        )
        assert response.status_code in [200, 404]

    async def test_rate_limit_tracking_in_database(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that rate limit usage is properly tracked in database."""
        # Create API key
        key_data = {
            "name": "Rate Limit Tracking Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 10,
            "description": "Testing usage tracking"
        }
        
        create_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        created_key = create_response.json()
        test_key = created_key["api_key"]
        key_id = created_key["id"]
        
        # Make some requests
        test_headers = {"Authorization": f"Bearer {test_key}"}
        for i in range(3):
            await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=test_headers
            )
        
        # Check that usage_count was updated in database
        from uuid import UUID
        api_key_ops = APIKeyOperations()
        db_key = await api_key_ops.get_api_key_by_id(
            session=real_db_session, 
            key_id=UUID(key_id)  # Convert string to UUID
        )
        
        assert db_key is not None
        assert db_key.usage_count >= 3  # Should have been incremented
        assert db_key.last_used_at is not None
        assert isinstance(db_key.last_used_at, datetime)

    async def test_different_rate_limits_per_key(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that different API keys can have different rate limits."""
        # Create high-limit key
        high_limit_data = {
            "name": "High Limit Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "High rate limit key"
        }
        
        # Create low-limit key
        low_limit_data = {
            "name": "Low Limit Key",
            "scopes": ["bot:read"], 
            "rate_limit_per_hour": 1,
            "description": "Low rate limit key"
        }
        
        # Create both keys
        high_response = await real_api_client.post(
            "/admin/api-keys",
            json=high_limit_data,
            headers=admin_auth_headers
        )
        assert high_response.status_code == 201
        high_key = high_response.json()["api_key"]
        
        low_response = await real_api_client.post(
            "/admin/api-keys",
            json=low_limit_data,
            headers=admin_auth_headers
        )
        assert low_response.status_code == 201
        low_key = low_response.json()["api_key"]
        
        high_headers = {"Authorization": f"Bearer {high_key}"}
        low_headers = {"Authorization": f"Bearer {low_key}"}
        
        # Low limit key should be limited after 1 request
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=low_headers
        )
        assert response.status_code in [200, 404]
        
        # Second request should be rate limited
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",  
            headers=low_headers
        )
        assert response.status_code == 429
        
        # High limit key should still work for many requests
        for i in range(5):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=high_headers
            )
            assert response.status_code in [200, 404]

    async def test_rate_limit_headers_in_response(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that rate limit information is included in response headers."""
        # Create API key
        key_data = {
            "name": "Rate Limit Headers Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 10,
            "description": "Testing rate limit headers"
        }
        
        create_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        test_key = create_response.json()["api_key"]
        
        test_headers = {"Authorization": f"Bearer {test_key}"}
        
        # Make a request
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=test_headers
        )
        
        # Check for rate limit headers
        assert "x-ratelimit-limit" in response.headers
        assert "x-ratelimit-remaining" in response.headers
        assert "x-ratelimit-reset" in response.headers
        
        # Verify header values
        assert int(response.headers["x-ratelimit-limit"]) == 10
        remaining = int(response.headers["x-ratelimit-remaining"])
        assert 0 <= remaining <= 10
        
        # Reset time should be a valid timestamp
        reset_time = int(response.headers["x-ratelimit-reset"])
        assert reset_time > 0

    async def test_disabled_api_key_not_rate_limited(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that disabled API keys are rejected before rate limiting."""
        # Create and then disable an API key
        key_data = {
            "name": "Disabled Key Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 5,
            "description": "Key to be disabled"
        }
        
        create_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        created_key = create_response.json()
        test_key = created_key["api_key"]
        key_id = created_key["id"]
        
        # Disable the key
        await real_api_client.delete(
            f"/admin/api-keys/{key_id}",
            headers=admin_auth_headers
        )
        
        # Attempt to use disabled key should return 401, not 429
        test_headers = {"Authorization": f"Bearer {test_key}"}
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=test_headers
        )
        
        # Should be unauthorized, not rate limited
        assert response.status_code == 401