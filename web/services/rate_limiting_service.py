"""
Rate Limiting Service - Following SOLID principles.

This service handles rate limiting for API endpoints and user actions,
with configurable limits, burst allowances, and comprehensive tracking.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Dict, Any, List, Optional, Protocol
import logging

logger = logging.getLogger(__name__)


class RateLimitStatus(Enum):
    """Status of rate limit check."""
    ALLOWED = "allowed"
    EXCEEDED = "exceeded"
    ERROR = "error"


@dataclass
class RateLimitConfig:
    """
    Configuration for rate limiting rules.
    
    Defines the limits, window, and behavior for rate limiting.
    """
    max_requests: int
    window_seconds: int
    burst_allowance: float = 1.2  # 20% burst allowance by default
    key_prefix: str = "rate_limit"
    
    def __post_init__(self):
        """Validate configuration values."""
        if self.max_requests <= 0:
            raise ValueError("max_requests must be positive")
        
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        
        if self.burst_allowance < 1.0:
            raise ValueError("burst_allowance must be >= 1.0")


@dataclass
class RateLimitResult:
    """
    Result of rate limit check.
    
    Contains all information about the rate limit status and remaining quota.
    """
    status: RateLimitStatus
    allowed: bool
    requests_made: int = 0
    requests_remaining: int = 0
    reset_timestamp: Optional[datetime] = None
    retry_after_seconds: int = 0
    limit: int = 0
    window_seconds: int = 0


class RateLimitExceededError(Exception):
    """Exception raised when rate limit is exceeded."""
    
    def __init__(self, message: str, retry_after_seconds: int = 0):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class CacheProtocol(Protocol):
    """Protocol defining the interface for cache operations."""
    
    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get value from cache."""
        pass
    
    async def set(self, key: str, value: Dict[str, Any], expire_seconds: Optional[int] = None) -> bool:
        """Set value in cache with optional expiration."""
        pass
    
    async def delete(self, key: str) -> bool:
        """Delete key from cache."""
        pass
    
    async def scan(self, pattern: str = "*") -> List[str]:
        """Scan for keys matching pattern."""
        pass


class RateLimitingService:
    """
    Service for rate limiting API requests and user actions.
    
    Following SRP: Only handles rate limiting logic and tracking.
    Following DIP: Depends on abstractions (cache protocol).
    Following OCP: Extensible for different rate limiting strategies.
    """
    
    def __init__(self, cache: CacheProtocol):
        """
        Initialize service with cache dependency.
        
        Args:
            cache: Cache implementation for storing rate limit data
        """
        self.cache = cache
    
    async def check_rate_limit(
        self,
        identifier: str,
        config: RateLimitConfig
    ) -> RateLimitResult:
        """
        Check rate limit for an identifier without incrementing.
        
        Args:
            identifier: Unique identifier (user ID, IP address, etc.)
            config: Rate limit configuration
            
        Returns:
            RateLimitResult with current status
        """
        try:
            cache_key = self._format_cache_key(identifier, config)
            cache_data = await self.cache.get(cache_key)
            
            current_time = datetime.now(timezone.utc)
            
            if not cache_data:
                # First request in window
                reset_timestamp = current_time + timedelta(seconds=config.window_seconds)
                
                return RateLimitResult(
                    status=RateLimitStatus.ALLOWED,
                    allowed=True,
                    requests_made=1,
                    requests_remaining=config.max_requests - 1,
                    reset_timestamp=reset_timestamp,
                    retry_after_seconds=0,
                    limit=config.max_requests,
                    window_seconds=config.window_seconds
                )
            
            # Parse existing data
            window_start = datetime.fromisoformat(cache_data["window_start"])
            requests_made = cache_data["requests"]
            
            # Check if window has expired
            if current_time >= window_start + timedelta(seconds=config.window_seconds):
                # Start new window
                reset_timestamp = current_time + timedelta(seconds=config.window_seconds)
                
                return RateLimitResult(
                    status=RateLimitStatus.ALLOWED,
                    allowed=True,
                    requests_made=1,
                    requests_remaining=config.max_requests - 1,
                    reset_timestamp=reset_timestamp,
                    retry_after_seconds=0,
                    limit=config.max_requests,
                    window_seconds=config.window_seconds
                )
            
            # Check limits (including burst allowance)
            max_allowed = int(config.max_requests * config.burst_allowance)
            reset_timestamp = self._calculate_reset_timestamp(window_start, config)
            
            # Check if this request would exceed the limit
            if (requests_made + 1) > max_allowed:
                # Rate limit exceeded
                retry_after = self._calculate_retry_after(reset_timestamp)
                
                logger.warning(
                    f"Rate limit exceeded for {identifier}: "
                    f"{requests_made}/{max_allowed} requests"
                )
                
                return RateLimitResult(
                    status=RateLimitStatus.EXCEEDED,
                    allowed=False,
                    requests_made=requests_made,
                    requests_remaining=0,
                    reset_timestamp=reset_timestamp,
                    retry_after_seconds=retry_after,
                    limit=config.max_requests,
                    window_seconds=config.window_seconds
                )
            
            # Within limits
            remaining = max(0, config.max_requests - (requests_made + 1))
            
            return RateLimitResult(
                status=RateLimitStatus.ALLOWED,
                allowed=True,
                requests_made=requests_made + 1,
                requests_remaining=remaining,
                reset_timestamp=reset_timestamp,
                retry_after_seconds=0,
                limit=config.max_requests,
                window_seconds=config.window_seconds
            )
            
        except Exception as e:
            logger.exception(f"Error checking rate limit for {identifier}")
            
            return RateLimitResult(
                status=RateLimitStatus.ERROR,
                allowed=False,
                requests_made=0,
                requests_remaining=0,
                retry_after_seconds=60,  # Conservative retry time
                limit=config.max_requests,
                window_seconds=config.window_seconds
            )
    
    async def check_rate_limit_and_increment(
        self,
        identifier: str,
        config: RateLimitConfig
    ) -> RateLimitResult:
        """
        Check rate limit and increment counter if allowed.
        
        Args:
            identifier: Unique identifier (user ID, IP address, etc.)
            config: Rate limit configuration
            
        Returns:
            RateLimitResult with updated status
        """
        # First check without incrementing
        result = await self.check_rate_limit(identifier, config)
        
        # Only increment if allowed
        if result.allowed:
            await self.increment_rate_limit_counter(identifier, config)
        
        return result
    
    async def increment_rate_limit_counter(
        self,
        identifier: str,
        config: RateLimitConfig
    ) -> None:
        """
        Increment the rate limit counter for an identifier.
        
        Args:
            identifier: Unique identifier
            config: Rate limit configuration
        """
        try:
            cache_key = self._format_cache_key(identifier, config)
            cache_data = await self.cache.get(cache_key)
            
            current_time = datetime.now(timezone.utc)
            
            if not cache_data:
                # Create new entry
                new_data = {
                    "requests": 1,
                    "window_start": current_time.isoformat(),
                    "first_request": current_time.isoformat()
                }
            else:
                # Check if we need to start a new window
                window_start = datetime.fromisoformat(cache_data["window_start"])
                
                if current_time >= window_start + timedelta(seconds=config.window_seconds):
                    # Start new window
                    new_data = {
                        "requests": 1,
                        "window_start": current_time.isoformat(),
                        "first_request": current_time.isoformat()
                    }
                else:
                    # Increment existing window
                    new_data = cache_data.copy()
                    new_data["requests"] += 1
            
            # Store with expiration slightly longer than window to handle clock skew
            expire_seconds = config.window_seconds + 60
            
            await self.cache.set(cache_key, new_data, expire_seconds)
            
            logger.debug(
                f"Incremented rate limit for {identifier}: "
                f"{new_data['requests']} requests"
            )
            
        except Exception as e:
            logger.exception(f"Error incrementing rate limit for {identifier}")
    
    async def reset_rate_limit(
        self,
        identifier: str,
        config: RateLimitConfig
    ) -> bool:
        """
        Reset rate limit for an identifier.
        
        Args:
            identifier: Unique identifier
            config: Rate limit configuration
            
        Returns:
            True if reset successful
        """
        try:
            cache_key = self._format_cache_key(identifier, config)
            result = await self.cache.delete(cache_key)
            
            logger.info(f"Reset rate limit for {identifier}")
            return result
            
        except Exception as e:
            logger.exception(f"Error resetting rate limit for {identifier}")
            return False
    
    async def get_rate_limit_status(
        self,
        identifier: str,
        config: RateLimitConfig
    ) -> Dict[str, Any]:
        """
        Get current rate limit status for an identifier.
        
        Args:
            identifier: Unique identifier
            config: Rate limit configuration
            
        Returns:
            Dictionary with rate limit status information
        """
        try:
            cache_key = self._format_cache_key(identifier, config)
            cache_data = await self.cache.get(cache_key)
            
            current_time = datetime.now(timezone.utc)
            
            if not cache_data:
                return {
                    "requests_made": 0,
                    "requests_remaining": config.max_requests,
                    "limit": config.max_requests,
                    "window_seconds": config.window_seconds,
                    "reset_timestamp": None,
                    "is_exceeded": False
                }
            
            window_start = datetime.fromisoformat(cache_data["window_start"])
            requests_made = cache_data["requests"]
            
            # Check if window has expired
            if current_time >= window_start + timedelta(seconds=config.window_seconds):
                return {
                    "requests_made": 0,
                    "requests_remaining": config.max_requests,
                    "limit": config.max_requests,
                    "window_seconds": config.window_seconds,
                    "reset_timestamp": None,
                    "is_exceeded": False
                }
            
            reset_timestamp = self._calculate_reset_timestamp(window_start, config)
            remaining = max(0, config.max_requests - requests_made)
            is_exceeded = requests_made >= config.max_requests
            
            return {
                "requests_made": requests_made,
                "requests_remaining": remaining,
                "limit": config.max_requests,
                "window_seconds": config.window_seconds,
                "reset_timestamp": reset_timestamp,
                "is_exceeded": is_exceeded
            }
            
        except Exception as e:
            logger.exception(f"Error getting rate limit status for {identifier}")
            return {
                "requests_made": 0,
                "requests_remaining": 0,
                "limit": config.max_requests,
                "window_seconds": config.window_seconds,
                "reset_timestamp": None,
                "is_exceeded": True,
                "error": str(e)
            }
    
    async def bulk_check_rate_limits(
        self,
        identifiers: List[str],
        config: RateLimitConfig
    ) -> Dict[str, RateLimitResult]:
        """
        Check rate limits for multiple identifiers efficiently.
        
        Args:
            identifiers: List of unique identifiers
            config: Rate limit configuration
            
        Returns:
            Dictionary mapping identifiers to their rate limit results
        """
        results = {}
        
        for identifier in identifiers:
            result = await self.check_rate_limit(identifier, config)
            results[identifier] = result
        
        logger.debug(f"Bulk checked rate limits for {len(identifiers)} identifiers")
        
        return results
    
    async def cleanup_expired_entries(self, max_age_seconds: int = 86400) -> int:
        """
        Clean up expired rate limit entries from cache.
        
        Args:
            max_age_seconds: Maximum age of entries to keep (default 24 hours)
            
        Returns:
            Number of entries deleted
        """
        try:
            # Scan for all rate limit keys
            all_keys = await self.cache.scan("rate_limit:*")
            deleted_count = 0
            
            current_time = datetime.now(timezone.utc)
            cutoff_time = current_time - timedelta(seconds=max_age_seconds)
            
            for key in all_keys:
                try:
                    cache_data = await self.cache.get(key)
                    
                    if cache_data and "window_start" in cache_data:
                        window_start = datetime.fromisoformat(cache_data["window_start"])
                        
                        if window_start < cutoff_time:
                            await self.cache.delete(key)
                            deleted_count += 1
                            
                except Exception as e:
                    logger.warning(f"Error checking cache entry {key}: {e}")
                    continue
            
            logger.info(f"Cleaned up {deleted_count} expired rate limit entries")
            return deleted_count
            
        except Exception as e:
            logger.exception("Error during rate limit cleanup")
            return 0
    
    async def get_service_statistics(self) -> Dict[str, Any]:
        """
        Get statistics about the rate limiting service.
        
        Returns:
            Dictionary with service statistics
        """
        try:
            # Scan for all rate limit keys
            all_keys = await self.cache.scan("*")
            
            total_active_limits = 0
            total_requests_tracked = 0
            limits_by_prefix = {}
            
            for key in all_keys:
                try:
                    # Parse key to get prefix
                    if ":" in key:
                        prefix = key.split(":")[0]
                        
                        cache_data = await self.cache.get(key)
                        if cache_data and "requests" in cache_data:
                            total_active_limits += 1
                            total_requests_tracked += cache_data["requests"]
                            
                            limits_by_prefix[prefix] = limits_by_prefix.get(prefix, 0) + 1
                            
                except Exception as e:
                    logger.warning(f"Error processing cache entry {key}: {e}")
                    continue
            
            return {
                "total_active_limits": total_active_limits,
                "total_requests_tracked": total_requests_tracked,
                "limits_by_prefix": limits_by_prefix,
                "service_status": "healthy"
            }
            
        except Exception as e:
            logger.exception("Error getting service statistics")
            return {
                "total_active_limits": 0,
                "total_requests_tracked": 0,
                "limits_by_prefix": {},
                "service_status": "error",
                "error": str(e)
            }
    
    def _format_cache_key(self, identifier: str, config: RateLimitConfig) -> str:
        """
        Format cache key for rate limit data.
        
        Args:
            identifier: Unique identifier
            config: Rate limit configuration
            
        Returns:
            Formatted cache key
        """
        return f"{config.key_prefix}:{identifier}"
    
    def _calculate_reset_timestamp(
        self,
        window_start: datetime,
        config: RateLimitConfig
    ) -> datetime:
        """
        Calculate when the rate limit window resets.
        
        Args:
            window_start: When the current window started
            config: Rate limit configuration
            
        Returns:
            Timestamp when window resets
        """
        return window_start + timedelta(seconds=config.window_seconds)
    
    def _calculate_retry_after(self, reset_timestamp: datetime) -> int:
        """
        Calculate retry after seconds until reset.
        
        Args:
            reset_timestamp: When rate limit resets
            
        Returns:
            Seconds until reset
        """
        current_time = datetime.now(timezone.utc)
        time_diff = reset_timestamp - current_time
        
        return max(0, int(time_diff.total_seconds()))
    
    async def enforce_rate_limit(
        self,
        identifier: str,
        config: RateLimitConfig,
        raise_on_exceeded: bool = True
    ) -> RateLimitResult:
        """
        Enforce rate limit with optional exception raising.
        
        Convenience method that checks and increments, with option to raise
        exception when limit is exceeded.
        
        Args:
            identifier: Unique identifier
            config: Rate limit configuration
            raise_on_exceeded: Whether to raise exception on limit exceeded
            
        Returns:
            RateLimitResult
            
        Raises:
            RateLimitExceededError: If rate limit exceeded and raise_on_exceeded=True
        """
        result = await self.check_rate_limit_and_increment(identifier, config)
        
        if not result.allowed and raise_on_exceeded:
            raise RateLimitExceededError(
                f"Rate limit exceeded for {identifier}. "
                f"Try again in {result.retry_after_seconds} seconds.",
                retry_after_seconds=result.retry_after_seconds
            )
        
        return result
    
    async def apply_rate_limit_headers(
        self,
        identifier: str,
        config: RateLimitConfig
    ) -> Dict[str, str]:
        """
        Get rate limit headers for HTTP responses.
        
        Args:
            identifier: Unique identifier
            config: Rate limit configuration
            
        Returns:
            Dictionary of HTTP headers
        """
        status = await self.get_rate_limit_status(identifier, config)
        
        headers = {
            "X-RateLimit-Limit": str(config.max_requests),
            "X-RateLimit-Remaining": str(status["requests_remaining"]),
            "X-RateLimit-Reset": str(int(status["reset_timestamp"].timestamp())) if status["reset_timestamp"] else "0"
        }
        
        if status["is_exceeded"]:
            # Calculate retry after
            if status["reset_timestamp"]:
                retry_after = self._calculate_retry_after(status["reset_timestamp"])
                headers["Retry-After"] = str(retry_after)
        
        return headers