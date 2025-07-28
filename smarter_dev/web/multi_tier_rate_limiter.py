"""Multi-tier rate limiting implementation for API keys.

This module provides advanced rate limiting with multiple time windows
to prevent both burst attacks and sustained abuse.

Rate limiting tiers:
- 10 requests per second (burst protection)
- 180 requests per minute (short-term abuse prevention)
- 2500 requests per 15 minutes (sustained abuse prevention)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple, NamedTuple
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from smarter_dev.web.crud import APIKeyOperations
from smarter_dev.web.models import APIKey


@dataclass
class RateLimitWindow:
    """Configuration for a single rate limiting window."""
    name: str
    duration_seconds: int
    limit: int
    header_suffix: str


class RateLimitResult(NamedTuple):
    """Result of rate limit check."""
    allowed: bool
    window: Optional[RateLimitWindow]
    remaining: int
    reset_time: datetime
    retry_after: int


class MultiTierRateLimiter:
    """Multi-tier rate limiter using PostgreSQL for reliable tracking.
    
    Implements multiple rate limiting windows with different time frames
    to provide comprehensive protection against abuse.
    """
    
    def __init__(self):
        self.api_key_ops = APIKeyOperations()
        
        # Define rate limiting windows (from strictest to most lenient)
        self.windows = [
            RateLimitWindow("second", 1, 0, "second"),  # Will be filled from API key
            RateLimitWindow("minute", 60, 0, "minute"),  # Will be filled from API key  
            RateLimitWindow("15min", 900, 0, "15min"),   # Will be filled from API key
        ]
    
    def _get_windows_for_api_key(self, api_key: APIKey) -> list[RateLimitWindow]:
        """Get rate limiting windows configured for a specific API key."""
        return [
            RateLimitWindow("second", 1, api_key.rate_limit_per_second, "second"),
            RateLimitWindow("minute", 60, api_key.rate_limit_per_minute, "minute"),
            RateLimitWindow("15min", 900, api_key.rate_limit_per_15_minutes, "15min"),
        ]
    
    def _get_next_tier_window(self, windows: list[RateLimitWindow], current_window: RateLimitWindow) -> RateLimitWindow:
        """Get the next tier window for escalation when current window is exceeded.
        
        Args:
            windows: List of all rate limit windows (in order from strictest to most lenient)
            current_window: The window that was exceeded
            
        Returns:
            The next tier window to escalate to, or the same window if it's the highest tier
        """
        try:
            current_index = windows.index(current_window)
            # Return next window if available, otherwise stay at current (highest tier)
            if current_index + 1 < len(windows):
                return windows[current_index + 1]
            else:
                return current_window  # Already at highest tier
        except ValueError:
            # If current window not found, return the highest tier window
            return windows[-1]
    
    async def _get_usage_count_for_window(
        self, 
        api_key: APIKey, 
        db: AsyncSession,
        window: RateLimitWindow,
        current_time: datetime
    ) -> int:
        """Get the number of requests made within a specific time window.
        
        Uses security_logs table to get accurate counts for each window.
        """
        window_start = current_time - timedelta(seconds=window.duration_seconds)
        
        # Query security logs for API requests within the window
        query = text("""
            SELECT COUNT(*) 
            FROM security_logs 
            WHERE api_key_id = :api_key_id 
              AND action = 'api_request'
              AND created_at >= :window_start
        """)
        
        result = await db.execute(
            query, 
            {
                "api_key_id": api_key.id,
                "window_start": window_start
            }
        )
        
        count = result.scalar()
        return count or 0
    
    async def _log_api_request(
        self, 
        api_key: APIKey, 
        db: AsyncSession,
        request: Request
    ) -> None:
        """Log an API request for rate limiting tracking."""
        from smarter_dev.web.security_logger import get_security_logger
        
        security_logger = get_security_logger()
        await security_logger.log_api_request(
            session=db,
            api_key=api_key,
            request=request,
            success=True
        )
    
    async def check_rate_limits(
        self, 
        api_key: APIKey, 
        db: AsyncSession,
        request: Request,
        response: Response
    ) -> None:
        """Check all rate limiting windows and enforce the strictest limit.
        
        Args:
            api_key: The API key to check
            db: Database session
            request: FastAPI request object
            response: FastAPI response object to add headers
            
        Raises:
            HTTPException: If any rate limit is exceeded (429)
        """
        current_time = datetime.now(timezone.utc)
        windows = self._get_windows_for_api_key(api_key)
        
        # Check each rate limiting window
        rate_limit_results = []
        
        for window in windows:
            usage_count = await self._get_usage_count_for_window(
                api_key, db, window, current_time
            )
            
            remaining = max(0, window.limit - usage_count)
            reset_time = current_time + timedelta(seconds=window.duration_seconds)
            retry_after = window.duration_seconds
            
            # Check if this window is exceeded
            if usage_count >= window.limit:
                # Escalate to next tier's reset time for better security
                next_tier_window = self._get_next_tier_window(windows, window)
                escalated_reset_time = current_time + timedelta(seconds=next_tier_window.duration_seconds)
                escalated_retry_after = next_tier_window.duration_seconds
                
                rate_limit_results.append(RateLimitResult(
                    allowed=False,
                    window=window,
                    remaining=0,
                    reset_time=escalated_reset_time,  # Use escalated reset time
                    retry_after=escalated_retry_after  # Use escalated retry time
                ))
                
                # Rate limit exceeded - add headers and raise exception
                await self._add_rate_limit_headers(response, windows, rate_limit_results, current_time)
                await self._log_rate_limit_violation(db, api_key, request, window, usage_count)
                
                # Use escalated tier name in error message
                escalated_tier_name = next_tier_window.name if next_tier_window != window else window.name
                
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit of {window.limit} requests per {window.name} exceeded. Must wait until {escalated_tier_name} window resets.",
                    headers={
                        "retry-after": str(escalated_retry_after),
                        **self._get_rate_limit_headers_with_escalation(windows, rate_limit_results, current_time, next_tier_window)
                    }
                )
            else:
                rate_limit_results.append(RateLimitResult(
                    allowed=True,
                    window=window,
                    remaining=remaining,
                    reset_time=reset_time,
                    retry_after=0
                ))
        
        # All rate limits passed - log the request and add headers
        await self._log_api_request(api_key, db, request)
        await self._add_rate_limit_headers(response, windows, rate_limit_results, current_time)
    
    async def _add_rate_limit_headers(
        self,
        response: Response,
        windows: list[RateLimitWindow],
        results: list[RateLimitResult],
        current_time: datetime
    ) -> None:
        """Add rate limiting headers to the response."""
        # Add headers for each window
        for window, result in zip(windows, results):
            suffix = window.header_suffix
            response.headers[f"x-ratelimit-limit-{suffix}"] = str(window.limit)
            response.headers[f"x-ratelimit-remaining-{suffix}"] = str(result.remaining)
            response.headers[f"x-ratelimit-reset-{suffix}"] = str(int(result.reset_time.timestamp()))
        
        # Add legacy headers (using the strictest limit - per second)
        if results:
            strictest_result = results[0]  # First window is always the strictest
            response.headers["x-ratelimit-limit"] = str(windows[0].limit)
            response.headers["x-ratelimit-remaining"] = str(strictest_result.remaining)
            response.headers["x-ratelimit-reset"] = str(int(strictest_result.reset_time.timestamp()))
    
    def _get_rate_limit_headers(
        self,
        windows: list[RateLimitWindow],
        results: list[RateLimitResult],
        current_time: datetime
    ) -> dict[str, str]:
        """Get rate limiting headers as a dictionary."""
        headers = {}
        
        # Add headers for each window
        for window, result in zip(windows, results):
            suffix = window.header_suffix
            headers[f"x-ratelimit-limit-{suffix}"] = str(window.limit)
            headers[f"x-ratelimit-remaining-{suffix}"] = str(result.remaining)
            headers[f"x-ratelimit-reset-{suffix}"] = str(int(result.reset_time.timestamp()))
        
        # Add legacy headers (using the strictest limit)
        if results:
            strictest_result = results[0]
            headers["x-ratelimit-limit"] = str(windows[0].limit)
            headers["x-ratelimit-remaining"] = str(strictest_result.remaining)
            headers["x-ratelimit-reset"] = str(int(strictest_result.reset_time.timestamp()))
        
        return headers
    
    def _get_rate_limit_headers_with_escalation(
        self,
        windows: list[RateLimitWindow],
        results: list[RateLimitResult],
        current_time: datetime,
        escalated_window: RateLimitWindow
    ) -> dict[str, str]:
        """Get rate limiting headers with escalated reset time for legacy headers."""
        headers = {}
        
        # Add headers for each window (show actual window state)
        for window, result in zip(windows, results):
            suffix = window.header_suffix
            headers[f"x-ratelimit-limit-{suffix}"] = str(window.limit)
            headers[f"x-ratelimit-remaining-{suffix}"] = str(result.remaining)
            headers[f"x-ratelimit-reset-{suffix}"] = str(int(
                (current_time + timedelta(seconds=window.duration_seconds)).timestamp()
            ))
        
        # Legacy headers use the escalated reset time, not the current tier's reset time
        if results:
            escalated_reset_time = current_time + timedelta(seconds=escalated_window.duration_seconds)
            headers["x-ratelimit-limit"] = str(windows[0].limit)  # Still show the limit that was exceeded
            headers["x-ratelimit-remaining"] = "0"  # Rate limited
            headers["x-ratelimit-reset"] = str(int(escalated_reset_time.timestamp()))  # Escalated reset time
        
        return headers
    
    async def _log_rate_limit_violation(
        self,
        db: AsyncSession,
        api_key: APIKey,
        request: Request,
        window: RateLimitWindow,
        current_usage: int
    ) -> None:
        """Log a rate limit violation for security monitoring."""
        from smarter_dev.web.security_logger import get_security_logger
        
        security_logger = get_security_logger()
        await security_logger.log_rate_limit_exceeded(
            session=db,
            api_key=api_key,
            request=request,
            current_usage=current_usage,
            limit=window.limit,
            window=window.name
        )


# Global multi-tier rate limiter instance
multi_tier_rate_limiter = MultiTierRateLimiter()


async def enforce_multi_tier_rate_limits(
    api_key: APIKey,
    db: AsyncSession,
    request: Request,
    response: Response
) -> None:
    """Enforce multi-tier rate limiting for an API key.
    
    This function should be called from API endpoints or middleware
    to check and enforce all rate limiting windows.
    
    Args:
        api_key: The authenticated API key
        db: Database session
        request: FastAPI request object
        response: FastAPI response object
        
    Raises:
        HTTPException: If any rate limit is exceeded (429)
    """
    await multi_tier_rate_limiter.check_rate_limits(api_key, db, request, response)