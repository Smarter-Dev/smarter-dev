"""Test suite for API key rate limiting functionality using TDD methodology.

This module tests the rate limiting enforcement for API keys to ensure
proper throttling and protection against abuse.

The rate limiter uses multi-tier windows (per-second, per-minute, per-15min)
tracked via security_logs entries. Tests create keys with low per-second
limits to trigger rate limiting without waiting for longer windows.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import APIKey
from smarter_dev.web.crud import APIKeyOperations
from smarter_dev.web.security import generate_secure_api_key


async def _create_key_with_limits(
    db_session: AsyncSession,
    name: str,
    rate_limit_per_second: int = 10,
    rate_limit_per_minute: int = 180,
    rate_limit_per_15_minutes: int = 2500,
) -> Tuple[APIKey, str]:
    """Helper: create an API key directly in the DB with custom multi-tier limits.

    Returns (api_key_model, plaintext_key).
    """
    full_key, key_hash, key_prefix = generate_secure_api_key()
    api_key = APIKey(
        name=name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes=["bot:read", "bot:write"],
        rate_limit_per_hour=10000,
        rate_limit_per_second=rate_limit_per_second,
        rate_limit_per_minute=rate_limit_per_minute,
        rate_limit_per_15_minutes=rate_limit_per_15_minutes,
        created_by="test",
        is_active=True,
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)
    return api_key, full_key


class TestAPIKeyRateLimiting:
    """Test API key rate limiting enforcement and tracking."""

    async def test_rate_limit_within_bounds(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str],
    ):
        """Test that requests within rate limit are allowed."""
        key_data = {
            "name": "Rate Limit Test Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 1000,
            "description": "Testing rate limits",
        }

        create_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers,
        )
        assert create_response.status_code == 201
        test_key = create_response.json()["api_key"]
        test_headers = {"Authorization": f"Bearer {test_key}"}

        # Default per-second limit is 10, so 5 requests should be fine
        for i in range(5):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=test_headers,
            )
            assert response.status_code in [200, 404], (
                f"Request {i+1} failed with {response.status_code}"
            )

    async def test_rate_limit_exceeded(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
    ):
        """Test that requests exceeding the per-second rate limit are rejected."""
        # Create a key with a very low per-second limit so we can trigger it easily
        api_key, full_key = await _create_key_with_limits(
            real_db_session,
            name="Rate Limit Exceed Test",
            rate_limit_per_second=3,
        )
        test_headers = {"Authorization": f"Bearer {full_key}"}

        # Make requests up to the per-second limit
        for i in range(3):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=test_headers,
            )
            assert response.status_code in [200, 404]

        # The 4th request should be rate limited
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=test_headers,
        )
        assert response.status_code == 429

        error_data = response.json()
        assert "rate limit" in error_data["detail"].lower()
        assert "retry-after" in response.headers

    async def test_rate_limit_reset_after_window(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
    ):
        """Test that rate limits reset after the time window expires."""
        api_key, full_key = await _create_key_with_limits(
            real_db_session,
            name="Rate Limit Reset Test",
            rate_limit_per_second=2,
        )
        test_headers = {"Authorization": f"Bearer {full_key}"}

        # Exhaust the per-second limit
        for i in range(2):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=test_headers,
            )
            assert response.status_code in [200, 404]

        # Should be rate limited now
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=test_headers,
        )
        assert response.status_code == 429

        # Wait for the per-second window to expire
        await asyncio.sleep(1.2)

        # Should work again after the window resets
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=test_headers,
        )
        assert response.status_code in [200, 404]

    async def test_rate_limit_per_key_isolation(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
    ):
        """Test that rate limits are isolated per API key."""
        api_key1, key1 = await _create_key_with_limits(
            real_db_session,
            name="Isolation Key 1",
            rate_limit_per_second=2,
        )
        api_key2, key2 = await _create_key_with_limits(
            real_db_session,
            name="Isolation Key 2",
            rate_limit_per_second=2,
        )
        headers1 = {"Authorization": f"Bearer {key1}"}
        headers2 = {"Authorization": f"Bearer {key2}"}

        # Exhaust limit for key1
        for _ in range(2):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers1,
            )
            assert response.status_code in [200, 404]

        # Key1 should be rate limited
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers1,
        )
        assert response.status_code == 429

        # Key2 should still work
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers2,
        )
        assert response.status_code in [200, 404]

    async def test_rate_limit_tracking_in_database(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
    ):
        """Test that rate limit usage is tracked via security logs."""
        api_key, full_key = await _create_key_with_limits(
            real_db_session,
            name="Rate Limit Tracking Test",
            rate_limit_per_second=10,
        )
        test_headers = {"Authorization": f"Bearer {full_key}"}

        # Make some requests
        for _ in range(3):
            await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=test_headers,
            )

        # The multi-tier rate limiter tracks via security_logs, not usage_count.
        # Verify that security log entries were created.
        from smarter_dev.web.security_logger import SecurityLogger

        security_logger = SecurityLogger()
        logs = await security_logger.get_logs_for_api_key(
            real_db_session, api_key.id
        )
        # Should have at least some log entries (api_key_used + api_request)
        assert len(logs) >= 3

    async def test_different_rate_limits_per_key(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
    ):
        """Test that different API keys can have different rate limits."""
        # High-limit key (won't be limited)
        _, high_key = await _create_key_with_limits(
            real_db_session,
            name="High Limit Key",
            rate_limit_per_second=100,
        )
        # Low-limit key (will be limited quickly)
        _, low_key = await _create_key_with_limits(
            real_db_session,
            name="Low Limit Key",
            rate_limit_per_second=1,
        )

        high_headers = {"Authorization": f"Bearer {high_key}"}
        low_headers = {"Authorization": f"Bearer {low_key}"}

        # Low limit key: first request succeeds
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=low_headers,
        )
        assert response.status_code in [200, 404]

        # Low limit key: second request should be rate limited
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=low_headers,
        )
        assert response.status_code == 429

        # High limit key should still work
        for _ in range(5):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=high_headers,
            )
            assert response.status_code in [200, 404]

    async def test_rate_limit_headers_in_response(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str],
    ):
        """Test that rate limit information is included in response headers."""
        key_data = {
            "name": "Rate Limit Headers Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 1000,
            "description": "Testing rate limit headers",
        }

        create_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers,
        )
        assert create_response.status_code == 201
        test_key = create_response.json()["api_key"]
        test_headers = {"Authorization": f"Bearer {test_key}"}

        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=test_headers,
        )

        # Check for legacy rate limit headers
        assert "x-ratelimit-limit" in response.headers
        assert "x-ratelimit-remaining" in response.headers
        assert "x-ratelimit-reset" in response.headers

        # Values should be valid integers
        assert int(response.headers["x-ratelimit-limit"]) > 0
        remaining = int(response.headers["x-ratelimit-remaining"])
        assert remaining >= 0
        reset_time = int(response.headers["x-ratelimit-reset"])
        assert reset_time > 0

    async def test_disabled_api_key_not_rate_limited(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str],
    ):
        """Test that disabled API keys are rejected before rate limiting."""
        key_data = {
            "name": "Disabled Key Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 5,
            "description": "Key to be disabled",
        }

        create_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers,
        )
        assert create_response.status_code == 201
        created_key = create_response.json()
        test_key = created_key["api_key"]
        key_id = created_key["id"]

        # Disable the key
        await real_api_client.delete(
            f"/admin/api-keys/{key_id}",
            headers=admin_auth_headers,
        )

        # Attempt to use disabled key should return 401, not 429
        test_headers = {"Authorization": f"Bearer {test_key}"}
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=test_headers,
        )
        assert response.status_code == 401
