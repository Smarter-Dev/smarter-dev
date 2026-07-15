"""Per-channel LLM token budgets, backed by Redis fixed-window counters.

An admin can cap how many chat tokens a channel spends per hour and per day
(see the ``/setmodel`` override). Enforcement runs bot-side inside
:mod:`chat_engine`, so this module talks to the bot's Redis directly rather than
round-tripping the web API every turn.

Same shape as :mod:`smarter_dev.web.image_quota`: an ``INCRBY`` per turn with
``EXPIRE ... NX`` so the first hit of a window fixes its expiry and later hits
don't slide it. Two independent windows run per channel — an hour (3600s) and a
day (86400s) — each aligned to the wall clock via ``window index = epoch //
window_seconds`` so windows reset on clean hour/day boundaries.

A ``0`` budget means unlimited: that window is never checked and never blocks.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from redis.asyncio import Redis

HOUR_WINDOW_SECONDS = 60 * 60
DAY_WINDOW_SECONDS = 24 * 60 * 60


def _window_index(now_epoch: float, window_seconds: int) -> int:
    """Wall-clock-aligned window index for ``now_epoch``."""
    return int(now_epoch) // window_seconds


def budget_key(channel_id: str, scope: str, window_index: int) -> str:
    """Redis key for one channel's ``scope`` ("hour"/"day") window."""
    return f"modelbudget:{channel_id}:{scope}:{window_index}"


# Ordered (scope, window length) pairs — the two windows every channel tracks.
_WINDOWS: tuple[tuple[str, int], ...] = (
    ("hour", HOUR_WINDOW_SECONDS),
    ("day", DAY_WINDOW_SECONDS),
)


async def _consumed(redis: Redis, key: str) -> int:
    """Tokens already spent in the window at ``key`` (0 when the key is unset)."""
    return int(await redis.get(key) or 0)


async def is_over_budget(
    redis: Redis,
    channel_id: str,
    daily_budget: int,
    hourly_budget: int,
) -> bool:
    """Whether ``channel_id`` has met or exceeded either active budget.

    Returns True if a non-zero budget's current window has consumed tokens
    ``>=`` that budget. A ``0`` budget is unlimited and never blocks, so with
    both budgets ``0`` this always returns False.
    """
    now_epoch = datetime.now(UTC).timestamp()
    checks = (
        (hourly_budget, "hour", HOUR_WINDOW_SECONDS),
        (daily_budget, "day", DAY_WINDOW_SECONDS),
    )
    for budget, scope, window_seconds in checks:
        if budget <= 0:
            continue
        key = budget_key(channel_id, scope, _window_index(now_epoch, window_seconds))
        if await _consumed(redis, key) >= budget:
            return True
    return False


async def add_usage(redis: Redis, channel_id: str, tokens: int) -> None:
    """Add ``tokens`` to ``channel_id``'s current hour and day windows.

    Each window is incremented with ``INCRBY`` and given a fresh TTL only on the
    first write (``EXPIRE ... NX``) so the window expires exactly one length
    after it opened. A non-positive ``tokens`` is a no-op.
    """
    if tokens <= 0:
        return
    now_epoch = datetime.now(UTC).timestamp()
    for scope, window_seconds in _WINDOWS:
        key = budget_key(channel_id, scope, _window_index(now_epoch, window_seconds))
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incrby(key, tokens)
            pipe.expire(key, window_seconds, nx=True)
            await pipe.execute()
