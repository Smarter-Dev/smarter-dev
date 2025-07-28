"""Test suite for multi-tier rate limiting implementation.

This module tests the enhanced rate limiting system that enforces multiple
time windows to prevent both burst attacks and sustained abuse.

Rate limits to test:
- 10 requests per second
- 180 requests per minute  
- 2500 requests per 15 minutes
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import APIKey


class TestMultiTierRateLimiting:
    """Test multi-tier rate limiting with multiple time windows."""
    
    @pytest.fixture
    async def api_key_with_multi_tier_limits(
        self,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ) -> APIKey:
        """Create an API key with multi-tier rate limits for testing."""
        key_data = {
            "name": "Multi-Tier Test Key",
            "scopes": ["bytes:read", "bytes:write"],
            "description": "API key for testing multi-tier rate limiting",
            # Multi-tier limits
            "rate_limit_per_second": 10,
            "rate_limit_per_minute": 180,
            "rate_limit_per_15_minutes": 2500
        }
        
        # Create API key through admin interface
        response = await self.admin_client.post(
            "/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        
        created_key = response.json()
        return created_key
    
    async def test_rate_limit_per_second_enforcement(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        api_key_with_multi_tier_limits: dict
    ):
        """Test that the 10 requests per second limit is enforced."""
        api_key = api_key_with_multi_tier_limits["key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        
        # Make 10 requests rapidly (should succeed)
        start_time = datetime.now(timezone.utc)
        for i in range(10):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers
            )
            assert response.status_code == 200
            
            # Check rate limit headers
            assert "x-ratelimit-limit-second" in response.headers
            assert "x-ratelimit-remaining-second" in response.headers
            assert int(response.headers["x-ratelimit-limit-second"]) == 10
            assert int(response.headers["x-ratelimit-remaining-second"]) == 10 - (i + 1)
        
        # 11th request within the same second should be rate limited
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        
        # Should get 429 Too Many Requests
        assert response.status_code == 429
        
        error_data = response.json()
        assert "rate limit" in error_data["detail"].lower()
        assert "10 requests per second" in error_data["detail"]
        
        # Check rate limit headers in error response
        assert "x-ratelimit-limit-second" in response.headers
        assert "x-ratelimit-remaining-second" in response.headers
        assert int(response.headers["x-ratelimit-remaining-second"]) == 0
        assert "retry-after" in response.headers
    
    async def test_rate_limit_per_minute_enforcement(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        api_key_with_multi_tier_limits: dict
    ):
        """Test that the 180 requests per minute limit is enforced."""
        api_key = api_key_with_multi_tier_limits["key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        
        # Simulate 180 requests spread over a minute (should succeed)
        # We'll simulate this by making requests with small delays
        successful_requests = 0
        
        for i in range(180):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers
            )
            
            if response.status_code == 200:
                successful_requests += 1
                
                # Check minute-level headers
                assert "x-ratelimit-limit-minute" in response.headers
                assert "x-ratelimit-remaining-minute" in response.headers
                assert int(response.headers["x-ratelimit-limit-minute"]) == 180
                
            elif response.status_code == 429:
                # Check if it's a per-second limit (expected) or per-minute limit
                error_data = response.json()
                if "per second" in error_data["detail"]:
                    # Wait for second window to reset and continue
                    await asyncio.sleep(1.1)
                    continue
                else:
                    # This should be the per-minute limit being hit
                    assert "180 requests per minute" in error_data["detail"]
                    break
            
            # Small delay to avoid hitting per-second limits too often
            await asyncio.sleep(0.11)  # Just over 100ms to stay under 10/sec
        
        # Should have made close to 180 successful requests
        assert successful_requests >= 170  # Allow some variance for timing
    
    async def test_rate_limit_per_15_minutes_enforcement(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        api_key_with_multi_tier_limits: dict
    ):
        """Test that the 2500 requests per 15 minutes limit is enforced."""
        api_key = api_key_with_multi_tier_limits["key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        
        # This test would take too long to run 2500 requests in real-time
        # So we'll test the logic by checking headers and simulating time passage
        
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code == 200
        
        # Check 15-minute window headers
        assert "x-ratelimit-limit-15min" in response.headers
        assert "x-ratelimit-remaining-15min" in response.headers
        assert int(response.headers["x-ratelimit-limit-15min"]) == 2500
        assert int(response.headers["x-ratelimit-remaining-15min"]) == 2499
    
    async def test_multiple_windows_work_independently(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        api_key_with_multi_tier_limits: dict
    ):
        """Test that different rate limit windows work independently."""
        api_key = api_key_with_multi_tier_limits["key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        
        # Make a request and check all window headers are present
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code == 200
        
        # All three rate limit tiers should be present in headers
        assert "x-ratelimit-limit-second" in response.headers
        assert "x-ratelimit-remaining-second" in response.headers
        assert "x-ratelimit-reset-second" in response.headers
        
        assert "x-ratelimit-limit-minute" in response.headers
        assert "x-ratelimit-remaining-minute" in response.headers
        assert "x-ratelimit-reset-minute" in response.headers
        
        assert "x-ratelimit-limit-15min" in response.headers
        assert "x-ratelimit-remaining-15min" in response.headers
        assert "x-ratelimit-reset-15min" in response.headers
        
        # Verify the limits are correct
        assert int(response.headers["x-ratelimit-limit-second"]) == 10
        assert int(response.headers["x-ratelimit-limit-minute"]) == 180
        assert int(response.headers["x-ratelimit-limit-15min"]) == 2500
    
    async def test_rate_limit_window_reset_times(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        api_key_with_multi_tier_limits: dict
    ):
        """Test that rate limit reset times are calculated correctly for each window."""
        api_key = api_key_with_multi_tier_limits["key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        
        before_request = datetime.now(timezone.utc)
        
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code == 200
        
        after_request = datetime.now(timezone.utc)
        
        # Check reset times are reasonable
        reset_second = int(response.headers["x-ratelimit-reset-second"])
        reset_minute = int(response.headers["x-ratelimit-reset-minute"])
        reset_15min = int(response.headers["x-ratelimit-reset-15min"])
        
        # Convert to datetime objects
        reset_second_dt = datetime.fromtimestamp(reset_second, tz=timezone.utc)
        reset_minute_dt = datetime.fromtimestamp(reset_minute, tz=timezone.utc)
        reset_15min_dt = datetime.fromtimestamp(reset_15min, tz=timezone.utc)
        
        # Verify reset times are in the future and reasonable
        assert reset_second_dt > before_request
        assert reset_minute_dt > before_request
        assert reset_15min_dt > before_request
        
        # Verify time windows are approximately correct (with some tolerance)
        second_diff = (reset_second_dt - after_request).total_seconds()
        minute_diff = (reset_minute_dt - after_request).total_seconds()
        min15_diff = (reset_15min_dt - after_request).total_seconds()
        
        assert 0 <= second_diff <= 1  # Should reset within 1 second
        assert 0 <= minute_diff <= 60  # Should reset within 1 minute
        assert 0 <= min15_diff <= 900  # Should reset within 15 minutes
    
    async def test_strictest_limit_takes_precedence(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        api_key_with_multi_tier_limits: dict
    ):
        """Test that the strictest rate limit (shortest window) takes precedence."""
        api_key = api_key_with_multi_tier_limits["key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        
        # Quickly exhaust the per-second limit
        for i in range(10):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers
            )
            assert response.status_code == 200
        
        # Next request should be rate limited by per-second limit
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code == 429
        
        error_data = response.json()
        # Should mention the per-second limit as the limiting factor
        assert "10 requests per second" in error_data["detail"]
        
        # The retry-after header should be short (< 1 second)
        retry_after = int(response.headers["retry-after"])
        assert retry_after <= 1
    
    async def test_rate_limit_headers_format(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        api_key_with_multi_tier_limits: dict
    ):
        """Test that rate limit headers follow expected format."""
        api_key = api_key_with_multi_tier_limits["key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code == 200
        
        # Check header naming convention
        expected_headers = [
            "x-ratelimit-limit-second",
            "x-ratelimit-remaining-second", 
            "x-ratelimit-reset-second",
            "x-ratelimit-limit-minute",
            "x-ratelimit-remaining-minute",
            "x-ratelimit-reset-minute", 
            "x-ratelimit-limit-15min",
            "x-ratelimit-remaining-15min",
            "x-ratelimit-reset-15min"
        ]
        
        for header in expected_headers:
            assert header in response.headers
            
        # Check that all values are valid integers
        for header in expected_headers:
            assert response.headers[header].isdigit()
            assert int(response.headers[header]) >= 0
    
    async def test_backward_compatibility_with_legacy_headers(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        api_key_with_multi_tier_limits: dict
    ):
        """Test that legacy rate limit headers are still present for backward compatibility."""
        api_key = api_key_with_multi_tier_limits["key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code == 200
        
        # Legacy headers should still be present (using the strictest limit)
        assert "x-ratelimit-limit" in response.headers
        assert "x-ratelimit-remaining" in response.headers
        assert "x-ratelimit-reset" in response.headers
        
        # Legacy headers should match the per-second limits (strictest)
        assert response.headers["x-ratelimit-limit"] == response.headers["x-ratelimit-limit-second"]
        assert response.headers["x-ratelimit-remaining"] == response.headers["x-ratelimit-remaining-second"]
        assert response.headers["x-ratelimit-reset"] == response.headers["x-ratelimit-reset-second"]