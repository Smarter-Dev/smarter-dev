"""Redis client setup and configuration."""

from __future__ import annotations

import json
import logging
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

import redis.asyncio as redis
from redis.asyncio import Redis

from smarter_dev.shared.config import Settings
from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)

# Global Redis client
_redis_client: Optional[Redis] = None


def create_redis_client(settings: Settings) -> Redis:
    """Create Redis client with proper configuration."""
    redis_url = settings.effective_redis_url
    
    # Parse Redis URL to get connection parameters
    pool = redis.ConnectionPool.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
        retry_on_timeout=True,
        socket_keepalive=True,
        socket_keepalive_options={},
    )
    
    client = Redis(connection_pool=pool)
    
    return client


def get_redis_client() -> Redis:
    """Get the global Redis client."""
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = create_redis_client(settings)
    return _redis_client


async def init_redis() -> None:
    """Initialize Redis connection."""
    global _redis_client
    
    settings = get_settings()
    logger.info(f"Initializing Redis connection to {settings.effective_redis_url}")
    
    _redis_client = create_redis_client(settings)
    
    # Test connection
    try:
        await _redis_client.ping()
        logger.info("Redis connection successful")
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        raise


async def close_redis() -> None:
    """Close Redis connections."""
    global _redis_client
    
    if _redis_client:
        logger.info("Closing Redis connections")
        await _redis_client.close()
        _redis_client = None


class RedisManager:
    """Redis manager for handling connections and operations."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Optional[Redis] = None

    async def init(self) -> None:
        """Initialize Redis connection."""
        logger.info("Initializing Redis manager")
        self.client = create_redis_client(self.settings)
        
        # Test connection
        try:
            await self.client.ping()
            logger.info("Redis connection successful")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

    async def close(self) -> None:
        """Close Redis connections."""
        if self.client:
            logger.info("Closing Redis manager")
            await self.client.close()
            self.client = None

    async def get(self, key: str) -> Optional[str]:
        """Get value from Redis."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ex: Optional[int] = None,
        px: Optional[int] = None,
        nx: bool = False,
        xx: bool = False,
    ) -> bool:
        """Set value in Redis."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.set(key, value, ex=ex, px=px, nx=nx, xx=xx)

    async def delete(self, *keys: str) -> int:
        """Delete keys from Redis."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.delete(*keys)

    async def exists(self, *keys: str) -> int:
        """Check if keys exist in Redis."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.exists(*keys)

    async def expire(self, key: str, seconds: int) -> bool:
        """Set expiration for key."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.expire(key, seconds)

    async def ttl(self, key: str) -> int:
        """Get time to live for key."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.ttl(key)

    # Hash operations
    async def hget(self, name: str, key: str) -> Optional[str]:
        """Get hash field value."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.hget(name, key)

    async def hset(self, name: str, key: str, value: str) -> int:
        """Set hash field value."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.hset(name, key, value)

    async def hgetall(self, name: str) -> Dict[str, str]:
        """Get all hash fields and values."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.hgetall(name)

    async def hdel(self, name: str, *keys: str) -> int:
        """Delete hash fields."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.hdel(name, *keys)

    # List operations
    async def lpush(self, name: str, *values: str) -> int:
        """Push values to left of list."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.lpush(name, *values)

    async def rpush(self, name: str, *values: str) -> int:
        """Push values to right of list."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.rpush(name, *values)

    async def lpop(self, name: str) -> Optional[str]:
        """Pop value from left of list."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.lpop(name)

    async def rpop(self, name: str) -> Optional[str]:
        """Pop value from right of list."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.rpop(name)

    async def lrange(self, name: str, start: int, end: int) -> List[str]:
        """Get range of list elements."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.lrange(name, start, end)

    # Set operations
    async def sadd(self, name: str, *values: str) -> int:
        """Add values to set."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.sadd(name, *values)

    async def srem(self, name: str, *values: str) -> int:
        """Remove values from set."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.srem(name, *values)

    async def smembers(self, name: str) -> set:
        """Get all set members."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.smembers(name)

    async def sismember(self, name: str, value: str) -> bool:
        """Check if value is in set."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.sismember(name, value)

    # Pub/Sub operations
    async def publish(self, channel: str, message: str) -> int:
        """Publish message to channel."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return await self.client.publish(channel, message)

    def pubsub(self) -> redis.client.PubSub:
        """Get pub/sub client."""
        if not self.client:
            raise RuntimeError("Redis manager not initialized")
        return self.client.pubsub()


class CacheManager:
    """High-level cache manager with JSON serialization."""

    def __init__(self, redis_manager: RedisManager, prefix: str = "cache"):
        self.redis_manager = redis_manager
        self.prefix = prefix

    def _make_key(self, key: str) -> str:
        """Create prefixed cache key."""
        return f"{self.prefix}:{key}"

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache with JSON deserialization."""
        cache_key = self._make_key(key)
        value = await self.redis_manager.get(cache_key)
        
        if value is None:
            return None
        
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            logger.warning(f"Failed to deserialize cached value for key: {key}")
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
    ) -> bool:
        """Set value in cache with JSON serialization."""
        cache_key = self._make_key(key)
        
        try:
            serialized_value = json.dumps(value)
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize value for key {key}: {e}")
            return False
        
        return await self.redis_manager.set(cache_key, serialized_value, ex=ttl)

    async def delete(self, key: str) -> bool:
        """Delete key from cache."""
        cache_key = self._make_key(key)
        return await self.redis_manager.delete(cache_key) > 0

    async def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        cache_key = self._make_key(key)
        return await self.redis_manager.exists(cache_key) > 0

    async def expire(self, key: str, seconds: int) -> bool:
        """Set expiration for cached key."""
        cache_key = self._make_key(key)
        return await self.redis_manager.expire(cache_key, seconds)

    async def ttl(self, key: str) -> int:
        """Get time to live for cached key."""
        cache_key = self._make_key(key)
        return await self.redis_manager.ttl(cache_key)

    async def clear_pattern(self, pattern: str) -> int:
        """Clear all keys matching pattern."""
        if not self.redis_manager.client:
            raise RuntimeError("Redis manager not initialized")
        
        cache_pattern = self._make_key(pattern)
        keys = []
        
        async for key in self.redis_manager.client.scan_iter(match=cache_pattern):
            keys.append(key)
        
        if keys:
            return await self.redis_manager.delete(*keys)
        return 0


# Utility functions for common cache operations
async def cache_user_balance(guild_id: str, user_id: str, balance_data: Dict[str, Any], ttl: int = 300) -> bool:
    """Cache user balance data."""
    cache_manager = CacheManager(RedisManager(get_settings()), "balance")
    key = f"{guild_id}:{user_id}"
    return await cache_manager.set(key, balance_data, ttl=ttl)


async def get_cached_user_balance(guild_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Get cached user balance data."""
    cache_manager = CacheManager(RedisManager(get_settings()), "balance")
    key = f"{guild_id}:{user_id}"
    return await cache_manager.get(key)


async def invalidate_user_balance_cache(guild_id: str, user_id: str) -> bool:
    """Invalidate cached user balance data."""
    cache_manager = CacheManager(RedisManager(get_settings()), "balance")
    key = f"{guild_id}:{user_id}"
    return await cache_manager.delete(key)