"""Tests for base service functionality.

This module tests the BaseService abstract class and its common functionality
including initialization, health checks, caching, and error handling.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.exceptions import ConfigurationError, ServiceError
from smarter_dev.bot.services.models import ServiceHealth


class TestBaseService:
    """Test the BaseService abstract class functionality."""
    
    class ConcreteService(BaseService):
        """Concrete implementation for testing."""
        
        async def test_method(self) -> str:
            self._ensure_initialized()
            return "test_result"
    
    @pytest.fixture
    def concrete_service(self, mock_api_client, mock_cache_manager):
        """Create concrete service instance for testing."""
        return self.ConcreteService(
            api_client=mock_api_client,
            cache_manager=mock_cache_manager,
            service_name="TestService"
        )
    
    async def test_service_initialization_success(self, concrete_service):
        """Test successful service initialization."""
        assert not concrete_service._is_initialized
        assert concrete_service.service_name == "TestService"
        assert concrete_service.has_cache is True
        
        await concrete_service.initialize()
        
        assert concrete_service._is_initialized is True
    
    async def test_service_initialization_without_cache(self, mock_api_client):
        """Test service initialization without cache manager."""
        service = self.ConcreteService(
            api_client=mock_api_client,
            cache_manager=None,
            service_name="NoCacheService"
        )
        
        assert service.has_cache is False
        
        await service.initialize()
        
        assert service._is_initialized is True
    
    async def test_service_initialization_failure_no_api_client(self):
        """Test service initialization failure when API client is missing."""
        service = self.ConcreteService(
            api_client=None,
            cache_manager=None
        )
        
        with pytest.raises(ServiceError, match="Service initialization failed"):
            await service.initialize()
    
    async def test_ensure_initialized_success(self, concrete_service):
        """Test _ensure_initialized with initialized service."""
        await concrete_service.initialize()
        
        # Should not raise an exception
        result = await concrete_service.test_method()
        assert result == "test_result"
    
    async def test_ensure_initialized_failure(self, concrete_service):
        """Test _ensure_initialized with uninitialized service."""
        with pytest.raises(ServiceError, match="Service.*is not initialized"):
            await concrete_service.test_method()
    
    async def test_health_check_success(self, concrete_service, mock_api_client, mock_cache_manager):
        """Test successful health check."""
        await concrete_service.initialize()
        
        # Mock healthy responses
        mock_api_client.health_check.return_value = ServiceHealth(
            service_name="MockAPIClient",
            is_healthy=True,
            response_time_ms=15.0
        )
        
        mock_cache_manager.health_check.return_value = ServiceHealth(
            service_name="MockCacheManager",
            is_healthy=True,
            response_time_ms=5.0
        )
        
        health = await concrete_service.health_check()
        
        assert health.service_name == "TestService"
        assert health.is_healthy is True
        assert health.response_time_ms == 15.0  # Max of API and cache
        assert health.details["api_healthy"] is True
        assert health.details["cache_healthy"] is True
        assert health.details["cache_enabled"] is True
    
    async def test_health_check_api_unhealthy(self, concrete_service, mock_api_client, mock_cache_manager):
        """Test health check with unhealthy API."""
        await concrete_service.initialize()
        
        # Mock unhealthy API response
        mock_api_client.health_check.return_value = ServiceHealth(
            service_name="MockAPIClient",
            is_healthy=False,
            response_time_ms=100.0,
            details={"error": "Connection failed"}
        )
        
        health = await concrete_service.health_check()
        
        assert health.is_healthy is False
        assert "API client unhealthy" in health.details["error"]
        assert health.details["api_details"]["error"] == "Connection failed"
    
    async def test_health_check_cache_unhealthy(self, concrete_service, mock_api_client, mock_cache_manager):
        """Test health check with unhealthy cache."""
        await concrete_service.initialize()
        
        # Mock healthy API but unhealthy cache
        mock_api_client.health_check.return_value = ServiceHealth(
            service_name="MockAPIClient",
            is_healthy=True,
            response_time_ms=10.0
        )
        
        mock_cache_manager.health_check.return_value = ServiceHealth(
            service_name="MockCacheManager",
            is_healthy=False,
            response_time_ms=50.0
        )
        
        health = await concrete_service.health_check()
        
        assert health.is_healthy is False
        assert health.response_time_ms == 50.0
        assert health.details["api_healthy"] is True
        assert health.details["cache_healthy"] is False
    
    async def test_health_check_not_initialized(self, concrete_service):
        """Test health check on uninitialized service."""
        health = await concrete_service.health_check()
        
        assert health.is_healthy is False
        assert "Service not initialized" in health.details["error"]
    
    async def test_health_check_exception(self, concrete_service, mock_api_client):
        """Test health check with exception."""
        await concrete_service.initialize()
        
        # Mock API health check to raise exception
        mock_api_client.health_check.side_effect = Exception("Network error")
        
        health = await concrete_service.health_check()
        
        assert health.is_healthy is False
        assert "Network error" in health.details["error"]
    
    async def test_cache_operations_with_cache(self, concrete_service, mock_cache_manager):
        """Test cache operations when cache is available."""
        await concrete_service.initialize()
        
        # Test cache get - override the side_effect to use return_value
        mock_cache_manager.get.side_effect = None
        mock_cache_manager.get.return_value = "cached_value"
        result = await concrete_service._get_cached("test_key")
        assert result == "cached_value"
        mock_cache_manager.get.assert_called_once_with("test_key")
        
        # Test cache set
        await concrete_service._set_cached("test_key", "new_value", ttl=300)
        mock_cache_manager.set.assert_called_once_with("test_key", "new_value", 300)
        
        # Test cache invalidation
        await concrete_service._invalidate_cache("test_key")
        mock_cache_manager.delete.assert_called_once_with("test_key")
        
        # Test pattern invalidation
        mock_cache_manager.clear_pattern.side_effect = None
        mock_cache_manager.clear_pattern.return_value = 5
        result = await concrete_service._invalidate_cache_pattern("test:*")
        assert result == 5
        mock_cache_manager.clear_pattern.assert_called_once_with("test:*")
    
    async def test_cache_operations_without_cache(self, mock_api_client):
        """Test cache operations when cache is not available."""
        service = self.ConcreteService(
            api_client=mock_api_client,
            cache_manager=None
        )
        await service.initialize()
        
        # All cache operations should return None or 0 without error
        result = await service._get_cached("test_key")
        assert result is None
        
        # These should not raise exceptions
        await service._set_cached("test_key", "value")
        await service._invalidate_cache("test_key")
        
        result = await service._invalidate_cache_pattern("test:*")
        assert result == 0
    
    async def test_cache_operations_with_exceptions(self, concrete_service, mock_cache_manager):
        """Test cache operations handle exceptions gracefully."""
        await concrete_service.initialize()
        
        # Mock cache operations to raise exceptions
        mock_cache_manager.get.side_effect = Exception("Cache error")
        mock_cache_manager.set.side_effect = Exception("Cache error")
        mock_cache_manager.delete.side_effect = Exception("Cache error")
        mock_cache_manager.clear_pattern.side_effect = Exception("Cache error")
        
        # Operations should not raise exceptions, just log warnings
        result = await concrete_service._get_cached("test_key")
        assert result is None
        
        await concrete_service._set_cached("test_key", "value")
        await concrete_service._invalidate_cache("test_key")
        
        result = await concrete_service._invalidate_cache_pattern("test:*")
        assert result == 0
    
    async def test_build_cache_key(self, concrete_service):
        """Test cache key building."""
        key = concrete_service._build_cache_key("type", "id1", "id2")
        assert key == "testservice:type:id1:id2"
    
    async def test_cleanup_success(self, concrete_service, mock_api_client):
        """Test successful service cleanup."""
        await concrete_service.initialize()
        assert concrete_service._is_initialized is True
        
        await concrete_service.cleanup()
        
        assert concrete_service._is_initialized is False
        mock_api_client.close.assert_called_once()
    
    async def test_cleanup_with_exception(self, concrete_service, mock_api_client):
        """Test service cleanup with exception."""
        await concrete_service.initialize()
        
        # Mock API client close to raise exception
        mock_api_client.close.side_effect = Exception("Close error")
        
        # Should not raise exception, just log error
        await concrete_service.cleanup()
        
        assert concrete_service._is_initialized is False
    
    async def test_logging_methods(self, concrete_service):
        """Test logging helper methods."""
        await concrete_service.initialize()
        
        # Test operation logging
        concrete_service._log_operation("test_op", param1="value1", param2="value2")
        
        # Test error logging
        test_error = ValueError("Test error")
        concrete_service._log_error("test_op", test_error, param1="value1")
        
        # These should not raise exceptions
        assert True
    
    async def test_default_service_name(self, mock_api_client):
        """Test default service name generation."""
        service = self.ConcreteService(api_client=mock_api_client)
        assert service.service_name == "ConcreteService"
    
    async def test_validate_configuration_override(self, mock_api_client):
        """Test custom configuration validation."""
        
        class CustomService(BaseService):
            async def _validate_configuration(self):
                await super()._validate_configuration()
                # Custom validation that always fails
                raise ConfigurationError("custom_setting", "Custom validation failed")
        
        service = CustomService(api_client=mock_api_client)
        
        with pytest.raises(ServiceError, match="Service initialization failed"):
            await service.initialize()
    
    async def test_context_manager_protocol(self, mock_api_client, mock_cache_manager):
        """Test that services can be used as async context managers."""
        
        class ContextService(BaseService):
            async def __aenter__(self):
                await self.initialize()
                return self
            
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                await self.cleanup()
        
        service = ContextService(
            api_client=mock_api_client,
            cache_manager=mock_cache_manager
        )
        
        async with service as ctx_service:
            assert ctx_service._is_initialized is True
            assert ctx_service is service
        
        assert service._is_initialized is False