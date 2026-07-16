"""Per-user rolling limit on messages directed at the chat bot.

A single user gets :data:`USER_MESSAGE_LIMIT` bot-directed messages per
rolling :data:`LIMIT_WINDOW_SECONDS`, counted across all channels. Each
counted message is a member of a per-user Redis sorted set
(``chatlimit:{user_id}``) whose score is the message's send epoch. Members
are Discord message ids, so recording the same message twice — once at the
mention gate, again from the agent's rankings — never double-counts.

Unlike :mod:`channel_token_budget` (fixed wall-clock windows), this is a true
rolling window: every check trims entries older than the window and counts
the survivors. The user unblocks the exact second enough old messages age
out, so the over-limit notice can embed that moment as a live Discord
countdown tag.

Enforcement lives in the mention plugin (drop + notice before the engine sees
the message); charging happens both there (mention/reply engagements) and in
:mod:`chat_engine` (messages the agent ranked as directed at the bot).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

from redis.asyncio import Redis

USER_MESSAGE_LIMIT = 60
LIMIT_WINDOW_SECONDS = 4 * 60 * 60

# Mirrors the response gate in ``chat_models.TurnDecision``: a ranked message
# scoring >= 5 was directed at the bot and is eligible for a reply, so it is
# exactly what this limit counts.
DIRECTED_SCORE_THRESHOLD = 5

# ``> -#`` renders as quoted subtext, ``<@id>`` pings the user, and
# ``{retry_tag}`` is a Discord relative timestamp (``<t:epoch:R>``) that the
# client renders as a live countdown to the moment the limit frees.
_OVER_LIMIT_NOTICE_TEMPLATE = (
    "> -# <@{user_id}> you've sent {limit} messages to the bot in the last "
    "{span}, you can try again {retry_tag}"
)

# Below this many minutes the notice span reads in minutes; from here up it
# rounds to whole hours ("2 hours" beats "127 minutes").
_SPAN_HOURS_CUTOFF_MINUTES = 120


def limit_key(user_id: str) -> str:
    """Redis sorted-set key holding one user's counted messages."""
    return f"chatlimit:{user_id}"


def notice_throttle_key(user_id: str) -> str:
    """Redis key claiming one user's over-limit ping for the current episode."""
    return f"chatlimit-notice:{user_id}"


@dataclass(frozen=True)
class OverLimitStatus:
    """A user is at/over the limit: the counted span and when it frees."""

    # Send epoch of the oldest message still counted against the limit — the
    # start of the "you've sent N messages in the last X" span, and the entry
    # whose ageing-out frees a slot.
    window_started_epoch: float
    # The exact epoch second the user drops back under the limit.
    retry_epoch: int


async def record_directed_messages(
    redis: Redis, user_id: str, message_epochs: dict[str, float]
) -> None:
    """Count ``message_epochs`` (message id → send epoch) against ``user_id``.

    ``ZADD`` treats members as a set, so re-recording a message id only
    updates its score — never inflates the count. The key's TTL is refreshed
    to one full window on every write since the newest entry is what keeps
    the set relevant; an idle user's set simply expires.
    """
    if not message_epochs:
        return
    key = limit_key(user_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zadd(key, message_epochs)
        pipe.expire(key, LIMIT_WINDOW_SECONDS)
        await pipe.execute()


async def over_limit_status(redis: Redis, user_id: str) -> OverLimitStatus | None:
    """``user_id``'s over-limit details, or None while they may still send.

    Trims entries that have aged out of the rolling window, then counts the
    survivors. At or past the limit, the (count - limit)-th oldest survivor is
    the one whose expiry drops the user back under — and equivalently the
    oldest of the ``limit`` newest messages, i.e. the start of the span the
    notice reports.
    """
    now_epoch = datetime.now(UTC).timestamp()
    key = limit_key(user_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, "-inf", now_epoch - LIMIT_WINDOW_SECONDS)
        pipe.zcard(key)
        _, counted = await pipe.execute()
    if counted < USER_MESSAGE_LIMIT:
        return None
    freeing_index = counted - USER_MESSAGE_LIMIT
    entries = await redis.zrange(key, freeing_index, freeing_index, withscores=True)
    if not entries:
        return None
    _, window_started_epoch = entries[0]
    return OverLimitStatus(
        window_started_epoch=float(window_started_epoch),
        retry_epoch=int(window_started_epoch) + LIMIT_WINDOW_SECONDS,
    )


def format_over_limit_notice(
    user_id: str, status: OverLimitStatus, now_epoch: float
) -> str:
    """The user-facing over-limit ping for ``status``.

    The span since the oldest counted message reads in rounded minutes, or in
    rounded whole hours once minute-rounding reaches two hours.
    """
    span_seconds = max(0.0, now_epoch - status.window_started_epoch)
    span_minutes = max(1, round(span_seconds / 60))
    if span_minutes >= _SPAN_HOURS_CUTOFF_MINUTES:
        span = f"{round(span_seconds / 3600)} hours"
    elif span_minutes == 1:
        span = "1 minute"
    else:
        span = f"{span_minutes} minutes"
    return _OVER_LIMIT_NOTICE_TEMPLATE.format(
        user_id=user_id,
        limit=USER_MESSAGE_LIMIT,
        span=span,
        retry_tag=f"<t:{status.retry_epoch}:R>",
    )


async def claim_notice_throttle(
    redis: Redis, user_id: str, retry_epoch: int
) -> bool:
    """True exactly once per over-limit episode (``SET NX EX``).

    The claim lives until ``retry_epoch`` — the user is pinged once when they
    hit the limit and stays unpinged for the rest of that episode; re-hitting
    the limit later starts a fresh episode with a fresh ping.
    """
    now_epoch = int(datetime.now(UTC).timestamp())
    ttl_seconds = max(60, retry_epoch - now_epoch)
    return bool(
        await redis.set(notice_throttle_key(user_id), "1", nx=True, ex=ttl_seconds)
    )
