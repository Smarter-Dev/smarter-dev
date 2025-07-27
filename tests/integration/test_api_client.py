"""Test API client for integration tests.

This module provides a test-specific API client that avoids async context manager
nesting issues while still providing the same interface as the production client.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, Mock

import httpx

from smarter_dev.bot.services.base import APIClientProtocol
from smarter_dev.bot.services.exceptions import APIError, NetworkError, ValidationError
from smarter_dev.bot.services.models import ServiceHealth

logger = logging.getLogger(__name__)


class IntegrationAPIClient(APIClientProtocol):
    """Integration test API client that avoids async context manager conflicts.
    
    This client is specifically designed for integration testing and avoids
    the nested async context manager issues that cause conflicts in tests.
    """
    
    def __init__(
        self,
        httpx_client: httpx.AsyncClient,
        base_url: str = "http://test",
        bot_token: str = "test_bot_token_12345"
    ):
        """Initialize test API client.
        
        Args:
            httpx_client: Pre-configured httpx AsyncClient
            base_url: Base URL for API endpoints
            bot_token: Bot authentication token
        """
        self._httpx_client = httpx_client
        self._base_url = base_url.rstrip('/')
        self._bot_token = bot_token
        self._is_closed = False
        
        # Performance tracking
        self._request_count = 0
        self._error_count = 0
        
        self._logger = logging.getLogger(f"{__name__}.IntegrationAPIClient")
    
    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> httpx.Response:
        """Execute GET request."""
        return await self._request("GET", path, params=params, headers=headers, timeout=timeout)
    
    async def post(
        self,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> httpx.Response:
        """Execute POST request."""
        return await self._request("POST", path, json_data=json_data, params=params, headers=headers, timeout=timeout)
    
    async def put(
        self,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> httpx.Response:
        """Execute PUT request."""
        return await self._request("PUT", path, json_data=json_data, params=params, headers=headers, timeout=timeout)
    
    async def delete(
        self,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> httpx.Response:
        """Execute DELETE request."""
        return await self._request("DELETE", path, json_data=json_data, params=params, headers=headers, timeout=timeout)
    
    async def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> httpx.Response:
        """Execute HTTP request with simplified error handling."""
        if self._is_closed:
            raise APIError("API client is closed")
        
        # Prepare request - no need for /api prefix in test environment
        url = path if path.startswith('/') else f"/{path}"
        request_headers = {
            "Authorization": f"Bearer {self._bot_token}",
            "Content-Type": "application/json"
        }
        if headers:
            request_headers.update(headers)
        
        try:
            self._request_count += 1
            
            self._logger.debug(f"Integration API request: {method} {url}")
            
            # Make the request using the pre-configured httpx client
            response = await self._httpx_client.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                headers=request_headers,
                timeout=timeout or 30.0,
                follow_redirects=True
            )
            
            self._logger.debug(f"Integration API response: {response.status_code}")
            
            # Handle error status codes
            if response.status_code >= 400:
                self._error_count += 1
                raise APIError(
                    f"HTTP {response.status_code}: {response.text}",
                    status_code=response.status_code,
                    response_body=response.text
                )
            
            return response
            
        except httpx.TimeoutException as e:
            self._error_count += 1
            raise NetworkError(f"Request timeout: {e}")
        
        except httpx.ConnectError as e:
            self._error_count += 1
            raise NetworkError(f"Connection failed: {e}")
        
        except APIError:
            # Re-raise API errors
            raise
        
        except Exception as e:
            self._error_count += 1
            raise APIError(f"Unexpected error: {e}")
    
    async def health_check(self) -> ServiceHealth:
        """Check the health of the API connection."""
        from datetime import datetime, timezone
        
        try:
            response = await self.get("/health", timeout=5.0)
            
            error_rate = 0.0
            if self._request_count > 0:
                error_rate = self._error_count / self._request_count
            
            return ServiceHealth(
                service_name="IntegrationAPIClient",
                is_healthy=True,
                error_rate=error_rate,
                last_check=datetime.now(timezone.utc),
                details={
                    "total_requests": self._request_count,
                    "total_errors": self._error_count,
                    "base_url": self._base_url
                }
            )
            
        except Exception as e:
            return ServiceHealth(
                service_name="IntegrationAPIClient",
                is_healthy=False,
                last_check=datetime.now(timezone.utc),
                details={
                    "error": str(e),
                    "total_requests": self._request_count,
                    "total_errors": self._error_count
                }
            )
    
    async def close(self) -> None:
        """Close the API client."""
        self._is_closed = True
        self._logger.info(
            "Integration API client closed",
            extra={
                "total_requests": self._request_count,
                "total_errors": self._error_count
            }
        )
    
    def __del__(self):
        """Destructor to ensure cleanup."""
        if not self._is_closed:
            # Don't log warnings during cleanup - it causes issues in tests
            pass


class MockCacheManager:
    """Mock cache manager for testing."""
    
    def __init__(self):
        self._cache = {}
        self._logger = logging.getLogger(f"{__name__}.MockCacheManager")
    
    async def get(self, key: str) -> Any:
        """Get value from cache."""
        return self._cache.get(key)
    
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Set value in cache."""
        self._cache[key] = value
    
    async def delete(self, key: str) -> None:
        """Delete value from cache."""
        self._cache.pop(key, None)
    
    async def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        return key in self._cache
    
    async def clear(self) -> None:
        """Clear all cache."""
        self._cache.clear()
    
    async def invalidate_pattern(self, pattern: str) -> None:
        """Invalidate cache entries matching pattern."""
        import fnmatch
        keys_to_delete = [key for key in self._cache.keys() if fnmatch.fnmatch(key, pattern)]
        for key in keys_to_delete:
            del self._cache[key]
    
    async def clear_pattern(self, pattern: str) -> int:
        """Clear cache entries matching pattern and return count."""
        import fnmatch
        keys_to_delete = [key for key in self._cache.keys() if fnmatch.fnmatch(key, pattern)]
        for key in keys_to_delete:
            del self._cache[key]
        return len(keys_to_delete)
    
    async def health_check(self) -> ServiceHealth:
        """Check cache health."""
        from datetime import datetime, timezone
        
        return ServiceHealth(
            service_name="MockCacheManager",
            is_healthy=True,
            last_check=datetime.now(timezone.utc),
            details={
                "cache_size": len(self._cache)
            }
        )
    
    async def close(self) -> None:
        """Close cache manager."""
        await self.clear()