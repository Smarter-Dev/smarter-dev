"""Windowed (per-channel / global / per-handler) caps backed by Redis.

These are the frequency bounds members actually feel — distinct from the
per-fire :class:`~smarter_dev.web.handler_budget.HandlerBudget`. They must hold
across *concurrent* fires, so the state is shared and atomic: a Redis counter
per window, incremented with ``INCR`` and given a TTL with ``EXPIRE ... NX`` so
the first hit of a window fixes its expiry and subsequent hits don't slide it.

The limiter only counts and reports; callers decide what to do:
- the worker emitter raises :class:`~smarter_dev.web.handler_budget.CapExceeded`
  mid-flight when an emit would breach a window, and
- the web dispatch endpoint simply declines to enqueue a fire.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

WINDOW_SECONDS = 60

# Frequency ceilings (generous preset). Reaction triggers are tighter: reactions
# are free to add and people pile on, so the amplification ratio is worse.
CHANNEL_MESSAGES_PER_MIN = 10
GLOBAL_AGENT_CALLS_PER_MIN = 30
HANDLER_FIRES_PER_MIN_MESSAGE = 10
HANDLER_FIRES_PER_MIN_REACTION = 4
# Admin handlers monitor guild-wide (the script runs on every message), so they
# need a high fire ceiling; the global agent/min cap still bounds expensive work.
ADMIN_FIRES_PER_MIN = 120

# Guild-wide gate on member lifecycle events (join/leave/rules/role) before a
# fire is even enqueued, so a raid degrades to declined dispatches rather than a
# fire-queue explosion. member_leave draws from the same window (join and leave
# burst together in a raid + ban wave).
GUILD_MEMBER_EVENTS_PER_MIN = 60
# Guild-wide gate on mutating thread ops (create/close/lock/reopen/delete),
# enforced in the runtime wrapper before the REST call.
GUILD_THREAD_OPS_PER_MIN = 30

# Creation ceilings. Named handlers removed the single-listener bound, so the
# number of handlers is capped outright — enforced at the create endpoints.
MAX_HANDLERS_PER_CHANNEL = 10
MAX_ADMIN_HANDLERS_PER_GUILD = 20

# When a handler fire errors we post a notice in the channel — but a broken
# handler errors on every fire, so throttle the notice hard: at most one per
# handler per window. The window is long enough not to nag, short enough that the
# channel learns the handler is broken.
ERROR_NOTICE_WINDOW_SECONDS = 30 * 60
ERROR_NOTICES_PER_WINDOW = 1


def channel_message_key(channel_id: str) -> str:
    return f"hcap:chanmsg:{channel_id}"


def global_agent_key() -> str:
    return "hcap:agent:global"


def handler_fire_key(handler_id: str) -> str:
    return f"hcap:fire:{handler_id}"


def handler_error_notice_key(handler_id: str) -> str:
    return f"hcap:errnotice:{handler_id}"


def guild_member_events_key(guild_id: str) -> str:
    return f"hcap:memberevt:{guild_id}"


def guild_thread_ops_key(guild_id: str) -> str:
    return f"hcap:threadop:{guild_id}"


def fires_per_min_for_trigger(trigger_type: str) -> int:
    """Per-handler fire ceiling, tighter for reaction triggers.

    The five admin-only member/thread triggers fall through to the default
    message ceiling of 10 (§3.4) — no special-casing.
    """
    return (
        HANDLER_FIRES_PER_MIN_REACTION
        if trigger_type == "reaction"
        else HANDLER_FIRES_PER_MIN_MESSAGE
    )


@dataclass
class WindowedLimiter:
    """Atomic fixed-window counters over a shared Redis client."""

    redis: Redis
    window_seconds: int = WINDOW_SECONDS

    async def hit(self, key: str, limit: int) -> bool:
        """Count one event against ``key``; return whether it stays within ``limit``.

        Atomic: ``INCR`` then ``EXPIRE key window NX`` in one pipeline, so the
        window's expiry is fixed by its first hit and never extended.
        """
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, self.window_seconds, nx=True)
            count, _ = await pipe.execute()
        return int(count) <= limit
