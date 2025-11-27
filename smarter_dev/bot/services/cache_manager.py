"""Cache manager for Discord bot services.

This module provides a production-grade caching layer with Redis backend,
comprehensive error handling, serialization, and monitoring capabilities.
"""

from __future__ import annotations

import json
import logging
import pickle
import time
from datetime import UTC
from datetime import datetime
from typing import Any

import redis.asyncio as redis
from redis.exceptions import ConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError

from smarter_dev.bot.services.base import CacheManagerProtocol
from smarter_dev.bot.services.exceptions import CacheError
from smarter_dev.bot.services.models import ServiceHealth

logger = logging.getLogger(__name__)


class SerializationStrategy:
    """Strategy pattern for data serialization."""

    @staticmethod
    def serialize_json(data: Any) -> bytes:
        """Serialize data as JSON."""
        return json.dumps(data, default=str).encode("utf-8")

    @staticmethod
    def deserialize_json(data: bytes) -> Any:
        """Deserialize JSON data."""
        return json.loads(data.decode("utf-8"))

    @staticmethod
    def serialize_pickle(data: Any) -> bytes:
        """Serialize data using pickle."""
        return pickle.dumps(data)

    @staticmethod
    def deserialize_pickle(data: bytes) -> Any:
        """Deserialize pickle data."""
        return pickle.loads(data)


class CacheManager(CacheManagerProtocol):
    """Production-grade Redis cache manager for bot services.

    Features:
    - Multiple serialization strategies (JSON, pickle)
    - Automatic key prefixing and namespacing
    - Connection pooling and retry logic
    - Health monitoring and metrics
    - Pattern-based cache invalidation
    - TTL management and expiration
    - Compression for large values
    """

    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "bot",
        default_ttl: int = 300,  # 5 minutes
        serialization: str = "json",
        max_connections: int = 10,
        socket_connect_timeout: float = 5.0,
        socket_timeout: float = 5.0,
        retry_on_timeout: bool = True,
        health_check_interval: int = 30
    ):
        """Initialize cache manager.

        Args:
            redis_url: Redis connection URL
            key_prefix: Prefix for all cache keys
            default_ttl: Default time-to-live in seconds
            serialization: Serialization strategy ('json' or 'pickle')
            max_connections: Maximum Redis connections
            socket_connect_timeout: Connection timeout in seconds
            socket_timeout: Socket timeout in seconds
            retry_on_timeout: Whether to retry on timeout
            health_check_interval: Health check interval in seconds
        """
        self._redis_url = redis_url
        self._key_prefix = key_prefix
        self._default_ttl = default_ttl
        self._max_connections = max_connections
        self._socket_connect_timeout = socket_connect_timeout
        self._socket_timeout = socket_timeout
        self._retry_on_timeout = retry_on_timeout
        self._health_check_interval = health_check_interval

        # Serialization strategy
        if serialization == "json":
            self._serialize = SerializationStrategy.serialize_json
            self._deserialize = SerializationStrategy.deserialize_json
        elif serialization == "pickle":
            self._serialize = SerializationStrategy.serialize_pickle
            self._deserialize = SerializationStrategy.deserialize_pickle
        else:
            raise ValueError(f"Unsupported serialization strategy: {serialization}")

        # Redis client
        self._redis: redis.Redis | None = None

        # Metrics
        self._operations_count = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._errors_count = 0
        self._total_response_time = 0.0
        self._last_health_check = datetime.now(UTC)

        self._logger = logging.getLogger(f"{__name__}.CacheManager")

    async def __aenter__(self) -> CacheManager:
        """Async context manager entry."""
        await self._ensure_connection()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    async def _ensure_connection(self) -> None:
        """Ensure Redis connection is established."""
        if self._redis is None:
            try:
                self._redis = redis.from_url(
                    self._redis_url,
                    max_connections=self._max_connections,
                    socket_connect_timeout=self._socket_connect_timeout,
                    socket_timeout=self._socket_timeout,
                    retry_on_timeout=self._retry_on_timeout,
                    decode_responses=False  # We handle encoding/decoding ourselves
                )

                # Test connection
                await self._redis.ping()

                self._logger.info(
                    "Redis connection established",
                    extra={"redis_url": self._redis_url}
                )

            except Exception as e:
                self._logger.error(f"Failed to connect to Redis: {e}")
                raise CacheError(f"Redis connection failed: {e}") from e

    def _build_key(self, key: str) -> str:
        """Build full cache key with prefix.

        Args:
            key: Original cache key

        Returns:
            Prefixed cache key
        """
        return f"{self._key_prefix}:{key}"

    async def get(self, key: str) -> Any | None:
        """Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found

        Raises:
            CacheError: On cache operation failures
        """
        await self._ensure_connection()

        full_key = self._build_key(key)

        try:
            start_time = time.time()
            self._operations_count += 1

            # Get value from Redis
            raw_value = await self._redis.get(full_key)

            # Track response time
            response_time = (time.time() - start_time) * 1000
            self._total_response_time += response_time

            if raw_value is None:
                self._cache_misses += 1
                self._logger.debug(f"Cache miss: {key}")
                return None

            # Deserialize value
            try:
                value = self._deserialize(raw_value)
                self._cache_hits += 1

                self._logger.debug(
                    f"Cache hit: {key} ({response_time:.1f}ms)",
                    extra={
                        "key": key,
                        "response_time_ms": response_time,
                        "value_size": len(raw_value)
                    }
                )

                return value

            except Exception as e:
                self._logger.warning(f"Failed to deserialize cached value for key {key}: {e}")
                # Delete corrupted value
                await self._redis.delete(full_key)
                self._cache_misses += 1
                return None

        except (ConnectionError, TimeoutError) as e:
            self._errors_count += 1
            self._logger.error(f"Redis connection error during get({key}): {e}")
            raise CacheError(f"Cache get operation failed: {e}") from e

        except RedisError as e:
            self._errors_count += 1
            self._logger.error(f"Redis error during get({key}): {e}")
            raise CacheError(f"Cache get operation failed: {e}") from e

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None
    ) -> None:
        """Set value in cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time to live in seconds (defaults to default_ttl)

        Raises:
            CacheError: On cache operation failures
        """
        await self._ensure_connection()

        full_key = self._build_key(key)
        expire_time = ttl or self._default_ttl

        try:
            start_time = time.time()
            self._operations_count += 1

            # Serialize value
            try:
                serialized_value = self._serialize(value)
            except Exception as e:
                self._logger.error(f"Failed to serialize value for key {key}: {e}")
                raise CacheError(f"Serialization failed: {e}") from e

            # Set value in Redis with TTL
            await self._redis.setex(full_key, expire_time, serialized_value)

            # Track response time
            response_time = (time.time() - start_time) * 1000
            self._total_response_time += response_time

            self._logger.debug(
                f"Cache set: {key} (TTL: {expire_time}s, {response_time:.1f}ms)",
                extra={
                    "key": key,
                    "ttl": expire_time,
                    "response_time_ms": response_time,
                    "value_size": len(serialized_value)
                }
            )

        except (ConnectionError, TimeoutError) as e:
            self._errors_count += 1
            self._logger.error(f"Redis connection error during set({key}): {e}")
            raise CacheError(f"Cache set operation failed: {e}") from e

        except RedisError as e:
            self._errors_count += 1
            self._logger.error(f"Redis error during set({key}): {e}")
            raise CacheError(f"Cache set operation failed: {e}") from e

    async def delete(self, key: str) -> None:
        """Delete value from cache.

        Args:
            key: Cache key to delete

        Raises:
            CacheError: On cache operation failures
        """
        await self._ensure_connection()

        full_key = self._build_key(key)

        try:
            start_time = time.time()
            self._operations_count += 1

            # Delete from Redis
            deleted_count = await self._redis.delete(full_key)

            # Track response time
            response_time = (time.time() - start_time) * 1000
            self._total_response_time += response_time

            self._logger.debug(
                f"Cache delete: {key} ({'found' if deleted_count > 0 else 'not found'}, {response_time:.1f}ms)",
                extra={
                    "key": key,
                    "deleted": deleted_count > 0,
                    "response_time_ms": response_time
                }
            )

        except (ConnectionError, TimeoutError) as e:
            self._errors_count += 1
            self._logger.error(f"Redis connection error during delete({key}): {e}")
            raise CacheError(f"Cache delete operation failed: {e}") from e

        except RedisError as e:
            self._errors_count += 1
            self._logger.error(f"Redis error during delete({key}): {e}")
            raise CacheError(f"Cache delete operation failed: {e}") from e

    async def clear_pattern(self, pattern: str) -> int:
        """Clear all keys matching pattern.

        Args:
            pattern: Pattern to match (supports wildcards)

        Returns:
            Number of keys deleted

        Raises:
            CacheError: On cache operation failures
        """
        await self._ensure_connection()

        full_pattern = self._build_key(pattern)

        try:
            start_time = time.time()
            self._operations_count += 1

            # Scan for matching keys (safer than KEYS command)
            keys_to_delete = []
            async for key in self._redis.scan_iter(match=full_pattern, count=100):
                keys_to_delete.append(key)

            # Delete matching keys in batches
            deleted_count = 0
            if keys_to_delete:
                # Use pipeline for batch deletion
                pipe = self._redis.pipeline()
                for key in keys_to_delete:
                    pipe.delete(key)
                results = await pipe.execute()
                deleted_count = sum(results)

            # Track response time
            response_time = (time.time() - start_time) * 1000
            self._total_response_time += response_time

            self._logger.debug(
                f"Cache clear pattern: {pattern} (deleted {deleted_count} keys, {response_time:.1f}ms)",
                extra={
                    "pattern": pattern,
                    "deleted_count": deleted_count,
                    "response_time_ms": response_time
                }
            )

            return deleted_count

        except (ConnectionError, TimeoutError) as e:
            self._errors_count += 1
            self._logger.error(f"Redis connection error during clear_pattern({pattern}): {e}")
            raise CacheError(f"Cache clear pattern operation failed: {e}") from e

        except RedisError as e:
            self._errors_count += 1
            self._logger.error(f"Redis error during clear_pattern({pattern}): {e}")
            raise CacheError(f"Cache clear pattern operation failed: {e}") from e

    async def exists(self, key: str) -> bool:
        """Check if key exists in cache.

        Args:
            key: Cache key to check

        Returns:
            True if key exists, False otherwise

        Raises:
            CacheError: On cache operation failures
        """
        await self._ensure_connection()

        full_key = self._build_key(key)

        try:
            start_time = time.time()
            self._operations_count += 1

            exists = await self._redis.exists(full_key)

            # Track response time
            response_time = (time.time() - start_time) * 1000
            self._total_response_time += response_time

            return bool(exists)

        except (ConnectionError, TimeoutError) as e:
            self._errors_count += 1
            self._logger.error(f"Redis connection error during exists({key}): {e}")
            raise CacheError(f"Cache exists operation failed: {e}") from e

        except RedisError as e:
            self._errors_count += 1
            self._logger.error(f"Redis error during exists({key}): {e}")
            raise CacheError(f"Cache exists operation failed: {e}") from e

    async def get_ttl(self, key: str) -> int | None:
        """Get time-to-live for a key.

        Args:
            key: Cache key

        Returns:
            TTL in seconds, None if key doesn't exist, -1 if no expiration

        Raises:
            CacheError: On cache operation failures
        """
        await self._ensure_connection()

        full_key = self._build_key(key)

        try:
            ttl = await self._redis.ttl(full_key)

            if ttl == -2:  # Key doesn't exist
                return None
            elif ttl == -1:  # Key exists but has no expiration
                return -1
            else:
                return ttl

        except (ConnectionError, TimeoutError) as e:
            self._errors_count += 1
            self._logger.error(f"Redis connection error during get_ttl({key}): {e}")
            raise CacheError(f"Cache get_ttl operation failed: {e}") from e

        except RedisError as e:
            self._errors_count += 1
            self._logger.error(f"Redis error during get_ttl({key}): {e}")
            raise CacheError(f"Cache get_ttl operation failed: {e}") from e

    async def health_check(self) -> ServiceHealth:
        """Check the health of the cache connection.

        Returns:
            ServiceHealth: Health status information
        """
        current_time = datetime.now(UTC)

        try:
            await self._ensure_connection()

            # Perform health check operations
            start_time = time.time()

            # Test basic operations
            test_key = self._build_key("__health_check__")
            test_value = {"timestamp": current_time.isoformat()}

            # Set test value
            await self._redis.setex(test_key, 10, self._serialize(test_value))

            # Get test value
            retrieved_value = await self._redis.get(test_key)
            if retrieved_value:
                self._deserialize(retrieved_value)

            # Clean up
            await self._redis.delete(test_key)

            response_time = (time.time() - start_time) * 1000

            # Calculate metrics
            hit_rate = 0.0
            avg_response_time = 0.0
            error_rate = 0.0

            total_cache_ops = self._cache_hits + self._cache_misses
            if total_cache_ops > 0:
                hit_rate = self._cache_hits / total_cache_ops

            if self._operations_count > 0:
                avg_response_time = self._total_response_time / self._operations_count
                error_rate = self._errors_count / self._operations_count

            self._last_health_check = current_time

            return ServiceHealth(
                service_name="CacheManager",
                is_healthy=True,
                response_time_ms=response_time,
                error_rate=error_rate,
                last_check=current_time,
                details={
                    "redis_url": self._redis_url.split("@")[-1] if "@" in self._redis_url else self._redis_url,  # Hide credentials
                    "total_operations": self._operations_count,
                    "cache_hits": self._cache_hits,
                    "cache_misses": self._cache_misses,
                    "hit_rate": hit_rate,
                    "avg_response_time_ms": avg_response_time,
                    "total_errors": self._errors_count,
                    "key_prefix": self._key_prefix,
                    "default_ttl": self._default_ttl
                }
            )

        except Exception as e:
            return ServiceHealth(
                service_name="CacheManager",
                is_healthy=False,
                last_check=current_time,
                details={
                    "error": str(e),
                    "total_operations": self._operations_count,
                    "total_errors": self._errors_count
                }
            )

    async def get_stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dictionary containing cache statistics
        """
        total_cache_ops = self._cache_hits + self._cache_misses
        hit_rate = 0.0
        avg_response_time = 0.0
        error_rate = 0.0

        if total_cache_ops > 0:
            hit_rate = self._cache_hits / total_cache_ops

        if self._operations_count > 0:
            avg_response_time = self._total_response_time / self._operations_count
            error_rate = self._errors_count / self._operations_count

        return {
            "total_operations": self._operations_count,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate": hit_rate,
            "avg_response_time_ms": avg_response_time,
            "total_errors": self._errors_count,
            "error_rate": error_rate,
            "last_health_check": self._last_health_check.isoformat() if self._last_health_check else None
        }

    async def close(self) -> None:
        """Close the cache manager and cleanup resources."""
        if self._redis:
            await self._redis.close()
            self._redis = None

        self._logger.info(
            "Cache manager closed",
            extra={
                "total_operations": self._operations_count,
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "total_errors": self._errors_count
            }
        )
