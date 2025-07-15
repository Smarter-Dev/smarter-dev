"""Base service classes and interfaces for the Discord bot services.

This module defines the foundational service architecture following SOLID principles.
All concrete services inherit from BaseService to ensure consistent behavior,
error handling, and caching strategies.
"""

from __future__ import annotations

import logging
from abc import ABC
from typing import Any, Dict, Optional, Type, TypeVar, Protocol

from smarter_dev.bot.services.exceptions import (
    CacheError,
    ConfigurationError,
    ServiceError
)
from smarter_dev.bot.services.models import ServiceHealth

# Type variable for service instances
ServiceType = TypeVar('ServiceType', bound='BaseService')

logger = logging.getLogger(__name__)


class UserProtocol(Protocol):
    """Protocol for Discord User objects to support planning document compatibility."""
    
    @property
    def id(self) -> str:
        """User ID as string."""
        ...
    
    def __str__(self) -> str:
        """User display name."""
        ...


class APIClientProtocol(Protocol):
    """Protocol defining the interface for API clients.
    
    This protocol ensures that any API client implementation
    provides the necessary methods for service communication.
    Following the Dependency Inversion Principle.
    """
    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> Any:
        """Execute GET request.
        
        Args:
            path: API endpoint path
            params: Query parameters
            headers: Request headers
            timeout: Request timeout in seconds
            
        Returns:
            Response object with status_code and json() method
            
        Raises:
            APIError: On API communication failures
        """
        ...
    
    async def post(
        self,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> Any:
        """Execute POST request.
        
        Args:
            path: API endpoint path
            json_data: JSON request body
            params: Query parameters
            headers: Request headers
            timeout: Request timeout in seconds
            
        Returns:
            Response object with status_code and json() method
            
        Raises:
            APIError: On API communication failures
        """
        ...
    
    async def put(
        self,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> Any:
        """Execute PUT request.
        
        Args:
            path: API endpoint path
            json_data: JSON request body
            params: Query parameters
            headers: Request headers
            timeout: Request timeout in seconds
            
        Returns:
            Response object with status_code and json() method
            
        Raises:
            APIError: On API communication failures
        """
        ...
    
    async def delete(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> Any:
        """Execute DELETE request.
        
        Args:
            path: API endpoint path
            params: Query parameters
            headers: Request headers
            timeout: Request timeout in seconds
            
        Returns:
            Response object with status_code and json() method
            
        Raises:
            APIError: On API communication failures
        """
        ...
    
    async def close(self) -> None:
        """Close the API client and cleanup resources."""
        ...
    
    async def health_check(self) -> ServiceHealth:
        """Check the health of the API connection.
        
        Returns:
            ServiceHealth: Health status information
        """
        ...


class CacheManagerProtocol(Protocol):
    """Protocol defining the interface for cache managers.
    
    This protocol ensures that any cache implementation provides
    the necessary methods for service caching operations.
    Following the Dependency Inversion Principle.
    """
    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache.
        
        Args:
            key: Cache key
            
        Returns:
            Cached value or None if not found
            
        Raises:
            CacheError: On cache operation failures
        """
        ...
    
    
    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None
    ) -> None:
        """Set value in cache.
        
        Args:
            key: Cache key
            value: Value to cache
            ttl: Time to live in seconds
            
        Raises:
            CacheError: On cache operation failures
        """
        ...
    
    
    async def delete(self, key: str) -> None:
        """Delete value from cache.
        
        Args:
            key: Cache key to delete
            
        Raises:
            CacheError: On cache operation failures
        """
        ...
    
    
    async def clear_pattern(self, pattern: str) -> int:
        """Clear all keys matching pattern.
        
        Args:
            pattern: Pattern to match (supports wildcards)
            
        Returns:
            Number of keys deleted
            
        Raises:
            CacheError: On cache operation failures
        """
        ...
    
    
    async def health_check(self) -> ServiceHealth:
        """Check the health of the cache connection.
        
        Returns:
            ServiceHealth: Health status information
        """
        ...


class BaseService(ABC):
    """Abstract base class for all bot services.
    
    This class provides common functionality for all services including:
    - Dependency injection for API client and cache manager
    - Error handling and logging
    - Health checking capabilities
    - Service lifecycle management
    
    Following SOLID principles:
    - Single Responsibility: Base service concerns only
    - Open/Closed: Extensible through inheritance
    - Liskov Substitution: All services can be used interchangeably
    - Interface Segregation: Minimal, focused interface
    - Dependency Inversion: Depends on abstractions (protocols)
    """
    
    def __init__(
        self,
        api_client: APIClientProtocol,
        cache_manager: Optional[CacheManagerProtocol] = None,
        service_name: Optional[str] = None
    ):
        """Initialize base service.
        
        Args:
            api_client: API client implementation
            cache_manager: Cache manager implementation (optional)
            service_name: Name of the service for logging
        """
        self._api_client = api_client
        self._cache_manager = cache_manager
        self._service_name = service_name or self.__class__.__name__
        self._logger = logging.getLogger(f"{__name__}.{self._service_name}")
        self._is_initialized = False
    
    @property
    def service_name(self) -> str:
        """Get the service name."""
        return self._service_name
    
    @property
    def has_cache(self) -> bool:
        """Check if service has caching enabled."""
        return self._cache_manager is not None
    
    # Compatibility properties for planning document compliance
    @property
    def api(self) -> APIClientProtocol:
        """Get API client for compatibility with planning document."""
        return self._api_client
    
    @property
    def redis(self) -> Optional[CacheManagerProtocol]:
        """Get cache manager as 'redis' for compatibility with planning document."""
        return self._cache_manager
    
    @property
    def _cache(self) -> Dict[str, Any]:
        """Get in-memory cache dictionary for compatibility with planning document.
        
        Note: This is a compatibility layer. The actual implementation uses
        the cache manager for better performance and features.
        """
        # Return empty dict as this is handled by cache manager
        # This property exists only for planning document compatibility
        return {}
    
    async def initialize(self) -> None:
        """Initialize the service.
        
        This method is called once before the service is used.
        Subclasses can override this to perform initialization tasks.
        
        Raises:
            ServiceError: If initialization fails
        """
        try:
            await self._validate_configuration()
            self._is_initialized = True
            self._logger.info(f"Service {self._service_name} initialized successfully")
        except Exception as e:
            self._logger.error(f"Failed to initialize service {self._service_name}: {e}")
            raise ServiceError(
                f"Service initialization failed: {e}",
                error_code="INIT_FAILED"
            ) from e
    
    async def cleanup(self) -> None:
        """Cleanup service resources.
        
        This method is called when the service is no longer needed.
        Subclasses can override this to perform cleanup tasks.
        """
        try:
            if self._api_client:
                await self._api_client.close()
            self._logger.info(f"Service {self._service_name} cleaned up successfully")
        except Exception as e:
            self._logger.error(f"Error during service cleanup: {e}")
        finally:
            # Always mark service as not initialized, even if cleanup fails
            self._is_initialized = False
    
    async def health_check(self) -> ServiceHealth:
        """Check the health of the service.
        
        Returns:
            ServiceHealth: Comprehensive health status
        """
        from datetime import datetime, timezone
        
        try:
            # Check if service is initialized
            if not self._is_initialized:
                return ServiceHealth(
                    service_name=self._service_name,
                    is_healthy=False,
                    last_check=datetime.now(timezone.utc),
                    details={"error": "Service not initialized"}
                )
            
            # Check API client health
            api_health = await self._api_client.health_check()
            if not api_health.is_healthy:
                return ServiceHealth(
                    service_name=self._service_name,
                    is_healthy=False,
                    response_time_ms=api_health.response_time_ms,
                    last_check=datetime.now(timezone.utc),
                    details={"error": "API client unhealthy", "api_details": api_health.details}
                )
            
            # Check cache health if available
            cache_healthy = True
            cache_response_time = None
            
            if self._cache_manager:
                cache_health = await self._cache_manager.health_check()
                cache_healthy = cache_health.is_healthy
                cache_response_time = cache_health.response_time_ms
            
            # Calculate overall response time
            response_time = api_health.response_time_ms
            if cache_response_time:
                response_time = max(response_time or 0, cache_response_time)
            
            return ServiceHealth(
                service_name=self._service_name,
                is_healthy=cache_healthy,
                response_time_ms=response_time,
                last_check=datetime.now(timezone.utc),
                details={
                    "api_healthy": api_health.is_healthy,
                    "cache_healthy": cache_healthy,
                    "cache_enabled": self.has_cache
                }
            )
        
        except Exception as e:
            self._logger.error(f"Health check failed for {self._service_name}: {e}")
            return ServiceHealth(
                service_name=self._service_name,
                is_healthy=False,
                last_check=datetime.now(timezone.utc),
                details={"error": str(e)}
            )
    
    async def _get_cached(self, key: str) -> Optional[Any]:
        """Get value from cache if available.
        
        Args:
            key: Cache key
            
        Returns:
            Cached value or None
        """
        if not self._cache_manager:
            return None
        
        try:
            return await self._cache_manager.get(key)
        except Exception as e:
            self._logger.warning(f"Cache get failed for key {key}: {e}")
            return None
    
    async def _set_cached(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None
    ) -> None:
        """Set value in cache if available.
        
        Args:
            key: Cache key
            value: Value to cache
            ttl: Time to live in seconds
        """
        if not self._cache_manager:
            return
        
        try:
            await self._cache_manager.set(key, value, ttl)
        except Exception as e:
            self._logger.warning(f"Cache set failed for key {key}: {e}")
    
    async def _invalidate_cache(self, key: str) -> None:
        """Invalidate cache entry.
        
        Args:
            key: Cache key to invalidate
        """
        if not self._cache_manager:
            return
        
        try:
            await self._cache_manager.delete(key)
        except Exception as e:
            self._logger.warning(f"Cache invalidation failed for key {key}: {e}")
    
    async def _invalidate_cache_pattern(self, pattern: str) -> int:
        """Invalidate cache entries matching pattern.
        
        Args:
            pattern: Pattern to match
            
        Returns:
            Number of entries invalidated
        """
        if not self._cache_manager:
            return 0
        
        try:
            return await self._cache_manager.clear_pattern(pattern)
        except Exception as e:
            self._logger.warning(f"Cache pattern invalidation failed for pattern {pattern}: {e}")
            return 0
    
    def _ensure_initialized(self) -> None:
        """Ensure service is initialized before use.
        
        Raises:
            ServiceError: If service is not initialized
        """
        if not self._is_initialized:
            raise ServiceError(
                f"Service {self._service_name} is not initialized. Call initialize() first.",
                error_code="NOT_INITIALIZED"
            )
    
    def _build_cache_key(self, *parts: str) -> str:
        """Build a cache key from parts.
        
        Args:
            *parts: Key parts to join
            
        Returns:
            Formatted cache key
        """
        return f"{self._service_name.lower()}:{':'.join(parts)}"
    
    async def _validate_configuration(self) -> None:
        """Validate service configuration.
        
        Subclasses can override this to perform specific validation.
        
        Raises:
            ConfigurationError: If configuration is invalid
        """
        if not self._api_client:
            raise ConfigurationError("api_client", "API client is required")
    
    def _log_operation(
        self,
        operation: str,
        **context: Any
    ) -> None:
        """Log a service operation with context.
        
        Args:
            operation: Operation name
            **context: Additional context data
        """
        self._logger.info(
            f"Service operation: {operation}",
            extra={
                "service": self._service_name,
                "operation": operation,
                **context
            }
        )
    
    def _log_error(
        self,
        operation: str,
        error: Exception,
        **context: Any
    ) -> None:
        """Log a service error with context.
        
        Args:
            operation: Operation that failed
            error: Exception that occurred
            **context: Additional context data
        """
        self._logger.error(
            f"Service operation failed: {operation} - {error}",
            extra={
                "service": self._service_name,
                "operation": operation,
                "error_type": type(error).__name__,
                "error_message": str(error),
                **context
            }
        )