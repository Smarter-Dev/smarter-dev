"""Test suite for escalated rate limiting reset times.

This module tests that when a rate limit is exceeded, the client must wait
until the next tier's reset time, not the current tier's reset time.

Example: If someone makes 11 requests in 1 second (exceeding the 10/sec limit),
they should wait until the end of the minute window, not just 1 second.
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import APIKey


class TestEscalatedRateLimitReset:
    """Test escalated rate limit reset times."""
    
    async def test_second_limit_exceeded_waits_for_minute_reset(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that exceeding per-second limit requires waiting for minute reset."""
        # Create API key directly in database with known limits
        from smarter_dev.web.crud import APIKeyOperations
        
        api_key_ops = APIKeyOperations()
        api_key = await api_key_ops.create_api_key(
            db=real_db_session,
            name="Escalation Test Key",
            scopes=["bytes:read"],
            rate_limit_per_second=3,  # Very low for easy testing
            rate_limit_per_minute=100,
            rate_limit_per_15_minutes=1000,
            created_by="test_user"
        )
        
        headers = {"Authorization": f"Bearer {api_key.key}"}
        
        # Make requests up to the per-second limit
        for i in range(3):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers
            )
            assert response.status_code == 200
        
        # The 4th request should be rate limited
        before_rate_limit = datetime.now(timezone.utc)
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        after_rate_limit = datetime.now(timezone.utc)
        
        # Should get 429 Too Many Requests
        assert response.status_code == 429
        
        # Check that retry-after is for the minute window, not second window
        retry_after = int(response.headers["retry-after"])
        
        # Should be much longer than 1 second (closer to a full minute)
        assert retry_after > 50  # Should be close to 60 seconds
        assert retry_after <= 60  # But not more than 60 seconds
        
        # Check that the reset time is for the minute window
        reset_time = int(response.headers["x-ratelimit-reset"])
        reset_dt = datetime.fromtimestamp(reset_time, tz=timezone.utc)
        
        # The reset time should be approximately 1 minute from now, not 1 second
        time_until_reset = (reset_dt - after_rate_limit).total_seconds()
        assert time_until_reset > 50  # Should be close to 60 seconds
        assert time_until_reset <= 60
    
    async def test_minute_limit_exceeded_waits_for_15min_reset(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that exceeding per-minute limit requires waiting for 15-minute reset."""
        # Create API key with limits that allow us to test minute exhaustion
        from smarter_dev.web.crud import APIKeyOperations
        
        api_key_ops = APIKeyOperations()
        api_key = await api_key_ops.create_api_key(
            db=real_db_session,
            name="Minute Escalation Test Key", 
            scopes=["bytes:read"],
            rate_limit_per_second=100,  # High enough to not interfere
            rate_limit_per_minute=2,    # Very low for easy testing
            rate_limit_per_15_minutes=1000,
            created_by="test_user"
        )
        
        headers = {"Authorization": f"Bearer {api_key.key}"}
        
        # Make requests up to the per-minute limit
        # We need to spread them out slightly to avoid hitting per-second limit
        for i in range(2):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers
            )
            assert response.status_code == 200
            await asyncio.sleep(0.05)  # Small delay to avoid per-second limit
        
        # The 3rd request should be rate limited due to per-minute limit
        before_rate_limit = datetime.now(timezone.utc)
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        after_rate_limit = datetime.now(timezone.utc)
        
        # Should get 429 Too Many Requests
        assert response.status_code == 429
        
        # Check that retry-after is for the 15-minute window, not minute window
        retry_after = int(response.headers["retry-after"])
        
        # Should be much longer than 60 seconds (closer to 15 minutes)
        assert retry_after > 800   # Should be close to 900 seconds (15 minutes)
        assert retry_after <= 900  # But not more than 15 minutes
        
        # Error message should mention the escalated window
        error_data = response.json()
        assert "15" in error_data["detail"] or "fifteen" in error_data["detail"].lower()
    
    async def test_15min_limit_exceeded_waits_for_15min_reset(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that exceeding 15-minute limit waits for 15-minute reset (no escalation)."""
        # For the highest tier, there's no escalation - just wait for the same tier
        from smarter_dev.web.crud import APIKeyOperations
        
        api_key_ops = APIKeyOperations()
        api_key = await api_key_ops.create_api_key(
            db=real_db_session,
            name="15min Limit Test Key",
            scopes=["bytes:read"], 
            rate_limit_per_second=100,
            rate_limit_per_minute=100,
            rate_limit_per_15_minutes=1,  # Very low for easy testing
            created_by="test_user"
        )
        
        headers = {"Authorization": f"Bearer {api_key.key}"}
        
        # Make 1 request (up to the 15-minute limit)
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code == 200
        
        # The 2nd request should be rate limited
        before_rate_limit = datetime.now(timezone.utc)
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        after_rate_limit = datetime.now(timezone.utc)
        
        # Should get 429 Too Many Requests
        assert response.status_code == 429
        
        # Check that retry-after is for the 15-minute window (no escalation available)
        retry_after = int(response.headers["retry-after"])
        
        # Should be close to 15 minutes
        assert retry_after > 800   # Should be close to 900 seconds
        assert retry_after <= 900  # But not more than 15 minutes
        
        # Error message should mention 15-minute limit
        error_data = response.json()
        assert "15" in error_data["detail"] or "fifteen" in error_data["detail"].lower()
    
    async def test_rate_limit_headers_show_next_tier_reset(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that rate limit headers show the next tier's reset time when exceeded."""
        from smarter_dev.web.crud import APIKeyOperations
        
        api_key_ops = APIKeyOperations()
        api_key = await api_key_ops.create_api_key(
            db=real_db_session,
            name="Header Test Key",
            scopes=["bytes:read"],
            rate_limit_per_second=2,
            rate_limit_per_minute=100,
            rate_limit_per_15_minutes=1000,
            created_by="test_user"
        )
        
        headers = {"Authorization": f"Bearer {api_key.key}"}
        
        # Exhaust per-second limit
        for i in range(2):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers
            )
            assert response.status_code == 200
        
        # Get rate limited response
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code == 429
        
        # The legacy x-ratelimit-reset header should show the MINUTE reset time, not second
        legacy_reset = int(response.headers["x-ratelimit-reset"])
        second_reset = int(response.headers["x-ratelimit-reset-second"])
        minute_reset = int(response.headers["x-ratelimit-reset-minute"])
        
        # Legacy reset should match the minute reset (next tier), not second reset
        assert legacy_reset == minute_reset
        assert legacy_reset != second_reset
        
        # The difference should be significant (close to a full minute vs 1 second)
        current_time = datetime.now(timezone.utc).timestamp()
        assert (minute_reset - current_time) > 50  # Minute reset is much later
        assert (second_reset - current_time) <= 1  # Second reset is very soon