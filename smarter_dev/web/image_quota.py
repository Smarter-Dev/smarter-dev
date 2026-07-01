"""Per-guild hourly quota for chat-agent image generation, backed by Redis.

Image generations are expensive, so each guild gets a small hourly budget. The
state must hold across the bot's turns and survive a bot restart, so it lives in
Redis (shared, durable) rather than in the bot process — the bot reaches it
through the API router in ``routers/image_quota.py``.

This is a fixed-window counter, same shape as :mod:`handler_caps`: an ``INCR``
per generation with ``EXPIRE ... NX`` so the first hit of an hour fixes the
window's expiry and later hits don't slide it. The key's remaining TTL is the
window's reset time, which is exactly the "when can I generate the next image"
the agent needs when the budget is spent.

Three operations:
- ``peek`` — read the remaining count without spending (for the per-turn
  ``<image-quota>`` the agent sees in its prompt),
- ``reserve`` — spend one slot atomically before a generation, and
- ``release`` — refund a reserved slot when the generation then fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

# Hourly budget per guild. Kept deliberately small — image generation is the
# most expensive thing the chat agent can do, and this is a cost guard.
IMAGES_PER_HOUR = 5
WINDOW_SECONDS = 60 * 60


def image_quota_key(guild_id: str) -> str:
    return f"imgquota:{guild_id}"


@dataclass
class ImageQuotaStatus:
    """A guild's current image budget, as reported by the limiter.

    ``resets_at`` and ``retry_after_seconds`` are None only when the window is
    empty (nothing spent this hour) — there's nothing to wait for. ``granted``
    is meaningful for ``reserve`` (did this call get a slot?); ``peek`` sets it
    from whether any budget remains.
    """

    limit: int
    remaining: int
    resets_at: datetime | None
    retry_after_seconds: int | None
    granted: bool = True


@dataclass
class ImageQuotaLimiter:
    """Fixed-window per-guild image budget over a shared Redis client."""

    redis: Redis
    limit: int = IMAGES_PER_HOUR
    window_seconds: int = WINDOW_SECONDS

    async def _window(self, key: str) -> tuple[datetime | None, int | None]:
        """The window's reset time + remaining seconds, from the key's TTL.

        Redis returns -2 (no key) or -1 (no expiry) as non-positive TTLs; both
        mean "no active window", so we report no reset time.
        """
        ttl = int(await self.redis.ttl(key))
        if ttl > 0:
            return datetime.now(UTC) + timedelta(seconds=ttl), ttl
        return None, None

    async def peek(self, guild_id: str) -> ImageQuotaStatus:
        """Report the remaining budget without spending any of it."""
        key = image_quota_key(guild_id)
        count = int(await self.redis.get(key) or 0)
        remaining = max(0, self.limit - count)
        resets_at, retry_after = await self._window(key)
        return ImageQuotaStatus(
            limit=self.limit,
            remaining=remaining,
            resets_at=resets_at,
            retry_after_seconds=retry_after,
            granted=remaining > 0,
        )

    async def reserve(self, guild_id: str) -> ImageQuotaStatus:
        """Spend one slot. ``granted`` is False when the hour is already full.

        Atomic ``INCR`` + ``EXPIRE NX`` fixes the window on its first hit. An
        increment that overshoots the limit is undone with ``DECR`` so a run of
        denied attempts can't inflate the counter (which would keep pushing the
        reported reset time — the TTL — off, though NX already pins it).
        """
        key = image_quota_key(guild_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, self.window_seconds, nx=True)
            count, _ = await pipe.execute()
        count = int(count)
        if count > self.limit:
            await self.redis.decr(key)
            resets_at, retry_after = await self._window(key)
            return ImageQuotaStatus(
                limit=self.limit,
                remaining=0,
                resets_at=resets_at,
                retry_after_seconds=retry_after,
                granted=False,
            )
        resets_at, retry_after = await self._window(key)
        return ImageQuotaStatus(
            limit=self.limit,
            remaining=self.limit - count,
            resets_at=resets_at,
            retry_after_seconds=retry_after,
            granted=True,
        )

    async def release(self, guild_id: str) -> None:
        """Refund one reserved slot (best-effort), never dropping below zero."""
        key = image_quota_key(guild_id)
        if int(await self.redis.get(key) or 0) > 0:
            await self.redis.decr(key)
