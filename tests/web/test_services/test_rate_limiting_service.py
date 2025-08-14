"""Test cases for Rate Limiting Service."""

from __future__ import annotations

import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock, Mock, patch

from web.services.rate_limiting_service import (
    RateLimitingService,
    RateLimitResult,
    RateLimitStatus,
    RateLimitExceededError,
    RateLimitConfig
)


class TestRateLimitConfig:
    """Test cases for RateLimitConfig data structure."""
    
    def test_rate_limit_config_creation(self):
        """Test RateLimitConfig creation with default values."""
        config = RateLimitConfig(
            max_requests=100,
            window_seconds=3600
        )
        
        assert config.max_requests == 100
        assert config.window_seconds == 3600
        assert config.burst_allowance == 1.2  # Default 20% burst
        assert config.key_prefix == "rate_limit"
    
    def test_rate_limit_config_custom_values(self):
        """Test RateLimitConfig with custom values."""
        config = RateLimitConfig(
            max_requests=50,
            window_seconds=300,
            burst_allowance=1.5,
            key_prefix="api_limit"
        )
        
        assert config.max_requests == 50
        assert config.window_seconds == 300
        assert config.burst_allowance == 1.5
        assert config.key_prefix == "api_limit"
    
    def test_rate_limit_config_validation(self):
        """Test RateLimitConfig validation."""
        # Test invalid max_requests
        with pytest.raises(ValueError, match="max_requests must be positive"):
            RateLimitConfig(max_requests=0, window_seconds=3600)
        
        # Test invalid window_seconds
        with pytest.raises(ValueError, match="window_seconds must be positive"):
            RateLimitConfig(max_requests=100, window_seconds=0)
        
        # Test invalid burst_allowance
        with pytest.raises(ValueError, match="burst_allowance must be >= 1.0"):
            RateLimitConfig(max_requests=100, window_seconds=3600, burst_allowance=0.5)


class TestRateLimitResult:
    """Test cases for RateLimitResult data structure."""
    
    def test_rate_limit_result_allowed(self):
        """Test RateLimitResult for allowed request."""
        result = RateLimitResult(
            status=RateLimitStatus.ALLOWED,
            allowed=True,
            requests_made=10,
            requests_remaining=90,
            reset_timestamp=datetime.now(timezone.utc),
            retry_after_seconds=0
        )
        
        assert result.status == RateLimitStatus.ALLOWED
        assert result.allowed is True
        assert result.requests_made == 10
        assert result.requests_remaining == 90
        assert result.retry_after_seconds == 0
    
    def test_rate_limit_result_exceeded(self):
        """Test RateLimitResult for exceeded limit."""
        reset_time = datetime.now(timezone.utc) + timedelta(minutes=30)
        
        result = RateLimitResult(
            status=RateLimitStatus.EXCEEDED,
            allowed=False,
            requests_made=105,
            requests_remaining=0,
            reset_timestamp=reset_time,
            retry_after_seconds=1800
        )
        
        assert result.status == RateLimitStatus.EXCEEDED
        assert result.allowed is False
        assert result.requests_made == 105
        assert result.requests_remaining == 0
        assert result.retry_after_seconds == 1800


class TestRateLimitingService:
    """Test cases for Rate Limiting Service functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_cache = AsyncMock()
        self.service = RateLimitingService(cache=self.mock_cache)
        
        # Sample rate limit configs
        self.api_config = RateLimitConfig(
            max_requests=100,
            window_seconds=3600,
            key_prefix="api"
        )
        
        self.submission_config = RateLimitConfig(
            max_requests=10,
            window_seconds=300,
            key_prefix="submission"
        )
        
        # Sample identifiers
        self.user_id = "user123"
        self.ip_address = "192.168.1.100"
    
    async def test_check_rate_limit_first_request(self):
        """Test rate limiting for first request."""
        # Mock no existing cache entry
        self.mock_cache.get.return_value = None
        
        # Act
        result = await self.service.check_rate_limit(
            identifier=self.user_id,
            config=self.api_config
        )
        
        # Assert
        assert result.status == RateLimitStatus.ALLOWED
        assert result.allowed is True
        assert result.requests_made == 1
        assert result.requests_remaining == 99
        assert result.retry_after_seconds == 0
        
        # Verify cache operations
        expected_key = f"{self.api_config.key_prefix}:{self.user_id}"
        self.mock_cache.get.assert_called_once_with(expected_key)
    
    async def test_check_rate_limit_within_limit(self):
        """Test rate limiting when within limit."""
        # Mock existing cache entry
        current_time = datetime.now(timezone.utc)
        window_start = current_time - timedelta(minutes=30)
        
        cache_data = {
            "requests": 25,
            "window_start": window_start.isoformat(),
            "first_request": window_start.isoformat()
        }
        self.mock_cache.get.return_value = cache_data
        
        # Act
        result = await self.service.check_rate_limit(
            identifier=self.user_id,
            config=self.api_config
        )
        
        # Assert
        assert result.status == RateLimitStatus.ALLOWED
        assert result.allowed is True
        assert result.requests_made == 26  # Incremented
        assert result.requests_remaining == 74
        assert result.retry_after_seconds == 0
    
    async def test_check_rate_limit_exceeded(self):
        """Test rate limiting when limit is exceeded."""
        # Mock cache entry at limit
        current_time = datetime.now(timezone.utc)
        window_start = current_time - timedelta(minutes=30)
        
        cache_data = {
            "requests": 120,  # At burst limit (100 * 1.2)
            "window_start": window_start.isoformat(),
            "first_request": window_start.isoformat()
        }
        self.mock_cache.get.return_value = cache_data
        
        # Act
        result = await self.service.check_rate_limit(
            identifier=self.user_id,
            config=self.api_config
        )
        
        # Assert
        assert result.status == RateLimitStatus.EXCEEDED
        assert result.allowed is False
        assert result.requests_made == 120
        assert result.requests_remaining == 0
        assert result.retry_after_seconds > 0  # Should have retry time
    
    async def test_check_rate_limit_window_reset(self):
        """Test rate limiting when window has reset."""
        # Mock cache entry from previous window
        current_time = datetime.now(timezone.utc)
        old_window_start = current_time - timedelta(hours=2)  # Old window
        
        cache_data = {
            "requests": 100,
            "window_start": old_window_start.isoformat(),
            "first_request": old_window_start.isoformat()
        }
        self.mock_cache.get.return_value = cache_data
        
        # Act
        result = await self.service.check_rate_limit(
            identifier=self.user_id,
            config=self.api_config
        )
        
        # Assert - Should reset to new window
        assert result.status == RateLimitStatus.ALLOWED
        assert result.allowed is True
        assert result.requests_made == 1  # Reset
        assert result.requests_remaining == 99
    
    async def test_check_rate_limit_burst_allowance(self):
        """Test rate limiting with burst allowance."""
        # Create config with burst allowance
        burst_config = RateLimitConfig(
            max_requests=100,
            window_seconds=3600,
            burst_allowance=1.2  # 20% burst = 120 total
        )
        
        # Mock cache entry within burst range
        current_time = datetime.now(timezone.utc)
        window_start = current_time - timedelta(minutes=30)
        
        cache_data = {
            "requests": 110,  # Over normal limit but within burst
            "window_start": window_start.isoformat(),
            "first_request": window_start.isoformat()
        }
        self.mock_cache.get.return_value = cache_data
        
        # Act
        result = await self.service.check_rate_limit(
            identifier=self.user_id,
            config=burst_config
        )
        
        # Assert - Should still be allowed due to burst
        assert result.status == RateLimitStatus.ALLOWED
        assert result.allowed is True
        assert result.requests_made == 111
    
    async def test_check_rate_limit_burst_exceeded(self):
        """Test rate limiting when burst limit is exceeded."""
        # Create config with burst allowance
        burst_config = RateLimitConfig(
            max_requests=100,
            window_seconds=3600,
            burst_allowance=1.2  # 20% burst = 120 total
        )
        
        # Mock cache entry exceeding burst
        current_time = datetime.now(timezone.utc)
        window_start = current_time - timedelta(minutes=30)
        
        cache_data = {
            "requests": 120,  # At burst limit
            "window_start": window_start.isoformat(),
            "first_request": window_start.isoformat()
        }
        self.mock_cache.get.return_value = cache_data
        
        # Act
        result = await self.service.check_rate_limit(
            identifier=self.user_id,
            config=burst_config
        )
        
        # Assert - Should be denied
        assert result.status == RateLimitStatus.EXCEEDED
        assert result.allowed is False
    
    async def test_check_rate_limit_and_increment(self):
        """Test checking and incrementing rate limit."""
        # Mock no existing cache entry
        self.mock_cache.get.return_value = None
        
        # Act
        result = await self.service.check_rate_limit_and_increment(
            identifier=self.user_id,
            config=self.api_config
        )
        
        # Assert
        assert result.status == RateLimitStatus.ALLOWED
        assert result.allowed is True
        assert result.requests_made == 1
        
        # Verify cache set was called to increment
        self.mock_cache.set.assert_called_once()
    
    async def test_check_rate_limit_and_increment_exceeded(self):
        """Test checking and incrementing when limit exceeded."""
        # Mock cache entry at limit
        current_time = datetime.now(timezone.utc)
        window_start = current_time - timedelta(minutes=30)
        
        cache_data = {
            "requests": 120,  # At burst limit (100 * 1.2)
            "window_start": window_start.isoformat(),
            "first_request": window_start.isoformat()
        }
        self.mock_cache.get.return_value = cache_data
        
        # Act
        result = await self.service.check_rate_limit_and_increment(
            identifier=self.user_id,
            config=self.api_config
        )
        
        # Assert
        assert result.status == RateLimitStatus.EXCEEDED
        assert result.allowed is False
        
        # Verify cache was NOT incremented when exceeded
        self.mock_cache.set.assert_not_called()
    
    async def test_increment_rate_limit_counter(self):
        """Test incrementing rate limit counter."""
        # Mock existing cache
        current_time = datetime.now(timezone.utc)
        window_start = current_time - timedelta(minutes=30)
        
        cache_data = {
            "requests": 50,
            "window_start": window_start.isoformat(),
            "first_request": window_start.isoformat()
        }
        self.mock_cache.get.return_value = cache_data
        
        # Act
        await self.service.increment_rate_limit_counter(
            identifier=self.user_id,
            config=self.api_config
        )
        
        # Assert cache set was called with incremented value
        self.mock_cache.set.assert_called_once()
        call_args = self.mock_cache.set.call_args
        
        # Verify the data structure was updated
        updated_data = call_args[0][1]  # Second argument is the data
        assert updated_data["requests"] == 51
    
    async def test_reset_rate_limit(self):
        """Test resetting rate limit for an identifier."""
        # Act
        await self.service.reset_rate_limit(
            identifier=self.user_id,
            config=self.api_config
        )
        
        # Assert
        expected_key = f"{self.api_config.key_prefix}:{self.user_id}"
        self.mock_cache.delete.assert_called_once_with(expected_key)
    
    async def test_get_rate_limit_status(self):
        """Test getting current rate limit status."""
        # Mock existing cache
        current_time = datetime.now(timezone.utc)
        window_start = current_time - timedelta(minutes=30)
        
        cache_data = {
            "requests": 75,
            "window_start": window_start.isoformat(),
            "first_request": window_start.isoformat()
        }
        self.mock_cache.get.return_value = cache_data
        
        # Act
        status = await self.service.get_rate_limit_status(
            identifier=self.user_id,
            config=self.api_config
        )
        
        # Assert
        assert status["requests_made"] == 75
        assert status["requests_remaining"] == 25
        assert status["limit"] == 100
        assert status["window_seconds"] == 3600
        assert "reset_timestamp" in status
    
    async def test_get_rate_limit_status_no_usage(self):
        """Test getting rate limit status with no previous usage."""
        # Mock no cache entry
        self.mock_cache.get.return_value = None
        
        # Act
        status = await self.service.get_rate_limit_status(
            identifier=self.user_id,
            config=self.api_config
        )
        
        # Assert
        assert status["requests_made"] == 0
        assert status["requests_remaining"] == 100
        assert status["limit"] == 100
    
    async def test_bulk_check_rate_limits(self):
        """Test bulk checking multiple rate limits."""
        identifiers = ["user1", "user2", "user3"]
        
        # Mock cache responses
        cache_responses = [
            None,  # user1: no cache
            {"requests": 50, "window_start": datetime.now(timezone.utc).isoformat(), "first_request": datetime.now(timezone.utc).isoformat()},  # user2: within limit
            {"requests": 120, "window_start": datetime.now(timezone.utc).isoformat(), "first_request": datetime.now(timezone.utc).isoformat()}  # user3: at burst limit
        ]
        
        self.mock_cache.get.side_effect = cache_responses
        
        # Act
        results = await self.service.bulk_check_rate_limits(
            identifiers=identifiers,
            config=self.api_config
        )
        
        # Assert
        assert len(results) == 3
        assert results["user1"].status == RateLimitStatus.ALLOWED
        assert results["user2"].status == RateLimitStatus.ALLOWED
        assert results["user3"].status == RateLimitStatus.EXCEEDED
    
    async def test_cleanup_expired_entries(self):
        """Test cleaning up expired rate limit entries."""
        # Mock cache keys and expired entries
        cache_keys = [
            "api:user1",
            "api:user2", 
            "submission:user3"
        ]
        
        # Mock cache scan
        self.mock_cache.scan.return_value = cache_keys
        
        # Mock cache get responses - some expired, some valid
        current_time = datetime.now(timezone.utc)
        expired_time = current_time - timedelta(hours=3)
        valid_time = current_time - timedelta(minutes=30)
        
        cache_responses = [
            {"window_start": expired_time.isoformat()},  # Expired
            {"window_start": valid_time.isoformat()},    # Valid
            {"window_start": expired_time.isoformat()}   # Expired
        ]
        
        self.mock_cache.get.side_effect = cache_responses
        
        # Act
        deleted_count = await self.service.cleanup_expired_entries(
            max_age_seconds=7200  # 2 hours
        )
        
        # Assert
        assert deleted_count == 2  # Two expired entries
        
        # Verify expired entries were deleted
        expected_delete_calls = ["api:user1", "submission:user3"]
        actual_delete_calls = [call[0][0] for call in self.mock_cache.delete.call_args_list]
        assert set(actual_delete_calls) == set(expected_delete_calls)
    
    async def test_get_service_statistics(self):
        """Test getting rate limiting service statistics."""
        # Mock cache scan and responses
        cache_keys = ["api:user1", "api:user2", "submission:user3"]
        self.mock_cache.scan.return_value = cache_keys
        
        current_time = datetime.now(timezone.utc)
        cache_responses = [
            {"requests": 50, "window_start": current_time.isoformat()},
            {"requests": 75, "window_start": current_time.isoformat()},
            {"requests": 5, "window_start": current_time.isoformat()}
        ]
        self.mock_cache.get.side_effect = cache_responses
        
        # Act
        stats = await self.service.get_service_statistics()
        
        # Assert
        assert stats["total_active_limits"] == 3
        assert stats["total_requests_tracked"] == 130  # 50 + 75 + 5
        assert "limits_by_prefix" in stats
        assert stats["limits_by_prefix"]["api"] == 2
        assert stats["limits_by_prefix"]["submission"] == 1
    
    async def test_format_cache_key(self):
        """Test cache key formatting."""
        key = self.service._format_cache_key("user123", self.api_config)
        assert key == "api:user123"
        
        # Test with different prefix
        custom_config = RateLimitConfig(
            max_requests=50,
            window_seconds=300,
            key_prefix="custom"
        )
        key = self.service._format_cache_key("test_id", custom_config)
        assert key == "custom:test_id"
    
    async def test_calculate_reset_timestamp(self):
        """Test reset timestamp calculation."""
        window_start = datetime.now(timezone.utc) - timedelta(minutes=30)
        
        reset_time = self.service._calculate_reset_timestamp(
            window_start, 
            self.api_config
        )
        
        expected_reset = window_start + timedelta(seconds=self.api_config.window_seconds)
        assert abs((reset_time - expected_reset).total_seconds()) < 1
    
    async def test_calculate_retry_after(self):
        """Test retry after calculation."""
        current_time = datetime.now(timezone.utc)
        reset_time = current_time + timedelta(minutes=30)
        
        retry_seconds = self.service._calculate_retry_after(reset_time)
        
        assert 1790 <= retry_seconds <= 1810  # Around 30 minutes (1800s) with small tolerance


class TestRateLimitingServiceIntegration:
    """Integration test cases combining multiple rate limiting scenarios."""
    
    def setup_method(self):
        """Set up integration test fixtures."""
        self.mock_cache = AsyncMock()
        self.service = RateLimitingService(cache=self.mock_cache)
        
        self.api_config = RateLimitConfig(
            max_requests=10,
            window_seconds=60
        )
    
    async def test_rapid_requests_scenario(self):
        """Test handling rapid consecutive requests."""
        user_id = "rapid_user"
        
        # Simulate rapid requests
        results = []
        cache_state = None
        
        for i in range(15):  # More than burst limit (12)
            # Mock cache based on previous state
            self.mock_cache.get.return_value = cache_state
            
            result = await self.service.check_rate_limit_and_increment(
                identifier=user_id,
                config=self.api_config
            )
            results.append(result)
            
            # Update mock cache state for next iteration
            if result.allowed:
                current_time = datetime.now(timezone.utc)
                cache_state = {
                    "requests": i + 1,
                    "window_start": current_time.isoformat(),
                    "first_request": current_time.isoformat()
                }
        
        # Assert first 12 requests allowed (10 * 1.2 burst allowance), rest denied
        for i in range(12):
            assert results[i].allowed is True
            assert results[i].requests_made == i + 1
        
        for i in range(12, 15):
            assert results[i].allowed is False


class TestRateLimitExceptions:
    """Test cases for rate limiting exceptions."""
    
    def test_rate_limit_exceeded_error(self):
        """Test RateLimitExceededError exception."""
        retry_after = 300
        error = RateLimitExceededError(
            "Rate limit exceeded",
            retry_after_seconds=retry_after
        )
        
        assert str(error) == "Rate limit exceeded"
        assert error.retry_after_seconds == retry_after


class TestRateLimitStatus:
    """Test cases for RateLimitStatus enum."""
    
    def test_rate_limit_status_values(self):
        """Test RateLimitStatus enum values."""
        assert RateLimitStatus.ALLOWED.value == "allowed"
        assert RateLimitStatus.EXCEEDED.value == "exceeded"
        assert RateLimitStatus.ERROR.value == "error"
    
    def test_rate_limit_status_comparison(self):
        """Test RateLimitStatus comparison."""
        assert RateLimitStatus.ALLOWED != RateLimitStatus.EXCEEDED
        assert RateLimitStatus.EXCEEDED != RateLimitStatus.ERROR
        assert RateLimitStatus.ALLOWED.value == "allowed"