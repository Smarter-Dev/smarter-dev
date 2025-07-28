"""Rate limiting implementation for API keys.

This module provides rate limiting functionality using PostgreSQL
for simple and reliable API key usage tracking.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from smarter_dev.web.crud import APIKeyOperations
from smarter_dev.web.models import APIKey


class RateLimiter:
    """Rate limiter for API keys using PostgreSQL for simple and reliable tracking."""
    
    def __init__(self):
        self.api_key_ops = APIKeyOperations()
    
    async def check_rate_limit(
        self, 
        api_key: APIKey, 
        db: AsyncSession,
        request: Request,
        response: Response
    ) -> None:
        """Check if API key is within rate limits using PostgreSQL.
        
        Args:
            api_key: The API key to check
            db: Database session
            request: FastAPI request object
            response: FastAPI response object to add headers
            
        Raises:
            HTTPException: If rate limit is exceeded (429)
        """
        current_time = datetime.now(timezone.utc)
        window_start = current_time - timedelta(hours=1)  # 1 hour sliding window
        
        try:
            # Count requests in the current window using a simple database query
            # We'll use the API key's last_used_at and usage_count for basic rate limiting
            
            # Simple approach: reset usage count if more than an hour has passed
            # Handle timezone-aware and timezone-naive datetime comparison
            last_used = api_key.last_used_at
            if last_used is not None and last_used.tzinfo is None:
                # Make timezone-naive datetime timezone-aware for comparison
                last_used = last_used.replace(tzinfo=timezone.utc)
            
            if last_used is None or (current_time - last_used) > timedelta(hours=1):
                # Reset the counter - it's been more than an hour
                api_key.usage_count = 0
                api_key.last_used_at = current_time
                
            # Check if we're within the rate limit
            if api_key.usage_count >= api_key.rate_limit_per_hour:
                # Calculate when the limit will reset
                if last_used:
                    reset_time = last_used + timedelta(hours=1)
                    retry_after = int((reset_time - current_time).total_seconds())
                else:
                    retry_after = 3600
                    reset_time = current_time + timedelta(hours=1)
                
                # Add rate limit headers
                response.headers["x-ratelimit-limit"] = str(api_key.rate_limit_per_hour)
                response.headers["x-ratelimit-remaining"] = "0"
                response.headers["x-ratelimit-reset"] = str(int(reset_time.timestamp()))
                response.headers["retry-after"] = str(max(1, retry_after))
                
                # Log rate limit violation
                from smarter_dev.web.security_logger import get_security_logger
                security_logger = get_security_logger()
                await security_logger.log_rate_limit_exceeded(
                    session=db,
                    api_key=api_key,
                    request=request,
                    current_usage=api_key.usage_count,
                    limit=api_key.rate_limit_per_hour
                )
                
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit of {api_key.rate_limit_per_hour} requests per hour exceeded. Try again later.",
                    headers={
                        "retry-after": str(max(1, retry_after)),
                        "x-ratelimit-limit": str(api_key.rate_limit_per_hour),
                        "x-ratelimit-remaining": "0",
                        "x-ratelimit-reset": str(int(reset_time.timestamp()))
                    }
                )
            
            # Increment usage counter and update last used time
            api_key.usage_count += 1
            api_key.last_used_at = current_time
            
            # Commit the changes to ensure they're persisted
            try:
                await db.commit()
            except Exception:
                # If commit fails, rollback and continue
                await db.rollback()
            
            # Add rate limit headers to successful response
            remaining = api_key.rate_limit_per_hour - api_key.usage_count
            reset_time = current_time + timedelta(hours=1)
            
            response.headers["x-ratelimit-limit"] = str(api_key.rate_limit_per_hour)
            response.headers["x-ratelimit-remaining"] = str(max(0, remaining))
            response.headers["x-ratelimit-reset"] = str(int(reset_time.timestamp()))
            
        except HTTPException:
            # Re-raise rate limit exceptions
            raise
        except Exception as e:
            # If database operation fails, log and allow the request
            # Better to allow requests than block them due to infrastructure issues
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Rate limiting error for API key {api_key.id}: {e}")
            
            # Add basic headers even if rate limiting failed
            response.headers["x-ratelimit-limit"] = str(api_key.rate_limit_per_hour)
            response.headers["x-ratelimit-remaining"] = str(api_key.rate_limit_per_hour)
            response.headers["x-ratelimit-reset"] = str(int((current_time + timedelta(hours=1)).timestamp()))


# Global rate limiter instance
rate_limiter = RateLimiter()


async def enforce_rate_limit(
    api_key: APIKey,
    db: AsyncSession,
    request: Request,
    response: Response
) -> None:
    """Enforce rate limiting for an API key.
    
    This function should be called from API endpoints or middleware
    to check and enforce rate limits.
    
    Args:
        api_key: The authenticated API key
        db: Database session
        request: FastAPI request object
        response: FastAPI response object
        
    Raises:
        HTTPException: If rate limit is exceeded (429)
    """
    await rate_limiter.check_rate_limit(api_key, db, request, response)