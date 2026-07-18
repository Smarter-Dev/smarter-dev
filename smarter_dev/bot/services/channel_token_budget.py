"""Per-channel LLM token usage windows and budgets, backed by Redis counters.

Every channel's chat-token spend is metered into hour and day windows (threads
count as their own channels). An admin can additionally cap the spend per hour
and per day via the ``/chat-bot-settings`` override — only channels with an override
have budgets enforced. Enforcement runs bot-side inside :mod:`chat_engine`, so
this module talks to the bot's Redis directly rather than round-tripping the
web API every turn.

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


def fallback_flag_key(channel_id: str) -> str:
    """Redis key for a channel's active free-fallback flag.

    Present (with EXAT = the budget reset epoch) while a member has opted the
    channel into its fallback model because the primary's budget is spent. Its
    presence tells the engine to skip budget enforcement and run the fallback.
    """
    return f"modelbudget-fallback:{channel_id}"


def fallback_ended_key(channel_id: str) -> str:
    """Redis key for a channel's "notify when the primary returns" marker.

    Set alongside :func:`fallback_flag_key` (EXAT = reset epoch + a day) so the
    engine can announce the primary model is back on the first turn after the
    fallback window closes, then clear the marker.
    """
    return f"modelbudget-fallback-ended:{channel_id}"


def next_window_reset_epoch(now_epoch: float, window_seconds: int) -> int:
    """Epoch second the current wall-aligned ``window_seconds`` window rolls over."""
    return (_window_index(now_epoch, window_seconds) + 1) * window_seconds


# Ordered (scope, window length) pairs — the two enforced windows every channel
# tracks. These are the counters ``over_budget_reset_epoch`` checks.
_WINDOWS: tuple[tuple[str, int], ...] = (
    ("hour", HOUR_WINDOW_SECONDS),
    ("day", DAY_WINDOW_SECONDS),
)

# Parallel display-only windows for free-fallback spend. A member opting a
# channel into its fallback model runs turns whose tokens must NOT count toward
# the primary's cap (otherwise the "free" opt-in would silently push the day
# window over budget and re-block the primary the moment the fallback closes).
# These windows are never read by ``over_budget_reset_epoch`` — they only feed
# ``/bot-usage`` so the display still reflects every token the channel spent.
_FALLBACK_WINDOWS: tuple[tuple[str, int], ...] = (
    ("hour-fallback", HOUR_WINDOW_SECONDS),
    ("day-fallback", DAY_WINDOW_SECONDS),
)


async def _consumed(redis: Redis, key: str) -> int:
    """Tokens already spent in the window at ``key`` (0 when the key is unset)."""
    return int(await redis.get(key) or 0)


async def over_budget_reset_epoch(
    redis: Redis,
    channel_id: str,
    daily_budget: int,
    hourly_budget: int,
) -> int | None:
    """When ``channel_id``'s spent budget frees, or None while under budget.

    Checks each non-zero budget's current window; when one or more have
    consumed tokens ``>=`` their budget, returns the epoch second at which the
    last of those windows rolls over — the moment the channel unblocks (the
    wall-clock boundaries make this exact). A ``0`` budget is unlimited and
    never blocks, so with both budgets ``0`` this always returns None.
    """
    now_epoch = datetime.now(UTC).timestamp()
    checks = (
        (hourly_budget, "hour", HOUR_WINDOW_SECONDS),
        (daily_budget, "day", DAY_WINDOW_SECONDS),
    )
    latest_reset_epoch: int | None = None
    for budget, scope, window_seconds in checks:
        if budget <= 0:
            continue
        window_index = _window_index(now_epoch, window_seconds)
        key = budget_key(channel_id, scope, window_index)
        if await _consumed(redis, key) >= budget:
            window_reset_epoch = next_window_reset_epoch(now_epoch, window_seconds)
            if latest_reset_epoch is None or window_reset_epoch > latest_reset_epoch:
                latest_reset_epoch = window_reset_epoch
    return latest_reset_epoch


async def current_window_usage(redis: Redis, channel_id: str) -> tuple[int, int]:
    """Tokens ``channel_id`` has consumed in the current (hour, day) windows.

    Reads the live counters without mutating them — backs the ``/bot-usage``
    display. Each figure sums the enforced window and its parallel free-fallback
    window so the display reflects every token spent, enforced or free. Windows
    with no writes yet read as 0.
    """
    now_epoch = datetime.now(UTC).timestamp()
    totals: list[int] = []
    for enforced_scope, fallback_scope, window_seconds in (
        ("hour", "hour-fallback", HOUR_WINDOW_SECONDS),
        ("day", "day-fallback", DAY_WINDOW_SECONDS),
    ):
        window_index = _window_index(now_epoch, window_seconds)
        enforced = await _consumed(
            redis, budget_key(channel_id, enforced_scope, window_index)
        )
        fallback = await _consumed(
            redis, budget_key(channel_id, fallback_scope, window_index)
        )
        totals.append(enforced + fallback)
    hour_used, day_used = totals
    return hour_used, day_used


async def _add_windowed_usage(
    redis: Redis,
    channel_id: str,
    tokens: int,
    windows: tuple[tuple[str, int], ...],
) -> None:
    """Add ``tokens`` to each of ``windows`` for ``channel_id``.

    Each window is incremented with ``INCRBY`` and given a fresh TTL only on the
    first write (``EXPIRE ... NX``) so the window expires exactly one length
    after it opened. A non-positive ``tokens`` is a no-op.
    """
    if tokens <= 0:
        return
    now_epoch = datetime.now(UTC).timestamp()
    for scope, window_seconds in windows:
        key = budget_key(channel_id, scope, _window_index(now_epoch, window_seconds))
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incrby(key, tokens)
            pipe.expire(key, window_seconds, nx=True)
            await pipe.execute()


async def add_usage(redis: Redis, channel_id: str, tokens: int) -> None:
    """Add ``tokens`` to ``channel_id``'s current enforced hour and day windows.

    These are the counters ``over_budget_reset_epoch`` enforces; a non-positive
    ``tokens`` is a no-op.
    """
    await _add_windowed_usage(redis, channel_id, tokens, _WINDOWS)


async def add_fallback_usage(redis: Redis, channel_id: str, tokens: int) -> None:
    """Add a free-fallback turn's ``tokens`` to the display-only windows.

    Metered separately from :func:`add_usage` so free-fallback spend shows in
    ``/bot-usage`` but never counts toward the enforced budget — the whole point
    of the opt-in is that its spend is free.
    """
    await _add_windowed_usage(redis, channel_id, tokens, _FALLBACK_WINDOWS)
