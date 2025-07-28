"""Middleware for API rate limiting and other cross-cutting concerns.

This module provides middleware implementations for rate limiting,
request tracking, and other functionality that needs to run on every request.
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from smarter_dev.web.rate_limiter import rate_limiter
from smarter_dev.web.security import hash_api_key
from smarter_dev.web.crud import APIKeyOperations

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware for API key rate limiting.
    
    This middleware runs after authentication and enforces rate limits
    on authenticated API requests.
    """
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with rate limiting.
        
        Args:
            request: FastAPI request object
            call_next: Next middleware/endpoint in chain
            
        Returns:
            Response: HTTP response with rate limit headers
        """
        # Call the next middleware/endpoint first to get authentication
        response = await call_next(request)
        
        # Only apply rate limiting to authenticated API requests
        if not self._should_rate_limit(request):
            return response
        
        # Try to get API key from request state (set by auth dependency)
        api_key = getattr(request.state, "api_key", None)
        
        if api_key:
            try:
                # Get database session from request state
                db = getattr(request.state, "db_session", None)
                if db:
                    # Apply rate limiting retroactively
                    # This is not ideal but works with FastAPI's dependency system
                    await rate_limiter.check_rate_limit(api_key, db, request, response)
                    
            except Exception as e:
                # If rate limiting fails, log but don't break the request
                logger.warning(f"Rate limiting error: {e}")
        
        return response
    
    def _should_rate_limit(self, request: Request) -> bool:
        """Check if request should be rate limited.
        
        Args:
            request: FastAPI request object
            
        Returns:
            bool: True if request should be rate limited
        """
        # Only rate limit API endpoints, not admin/health endpoints
        path = request.url.path
        
        # Skip rate limiting for certain paths
        skip_paths = [
            "/health",
            "/docs",
            "/redoc", 
            "/openapi.json",
            "/admin/"  # Admin endpoints have their own rate limiting
        ]
        
        for skip_path in skip_paths:
            if path.startswith(skip_path):
                return False
        
        # Rate limit API endpoints that have authentication
        return request.headers.get("authorization") is not None