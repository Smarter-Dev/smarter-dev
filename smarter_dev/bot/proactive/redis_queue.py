"""Redis producer primitives for guild-scoped proactive notifications.

Envelopes carry verbatim Discord message text, so the keys that hold them are
bounded by the content retention window where a bound is possible. The wake
and shadow streams hold no entry written more than the window ago: each is
trimmed exactly at the cutoff (approximate trimming skips a quiet stream whose
entries all sit in the open macro node) on every publish and again by
``trim_expired_envelopes`` on the bot's passive tick. A claimed batch expires
one window after the claim, not after the write, so its envelopes can outlive
their own write cutoff by up to one more window. The pending list is trimmed by
entry age on the same tick, and each envelope it drops is counted in
``pending-dropped`` so the agent is told; see ``trim_expired_envelopes``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from redis.exceptions import RedisError

from smarter_dev.bot.proactive.contracts import NotificationEnvelope
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.shared.message_content import CONTENT_RETENTION_MILLISECONDS
from smarter_dev.shared.message_content import oldest_retained_stream_id

logger = logging.getLogger(__name__)

KEY_PREFIX = "proactive:v1"
READY_GUILDS_KEY = f"{KEY_PREFIX}:guilds-with-wakes"
READY_STREAM_KEY = f"{KEY_PREFIX}:ready"
SHADOW_STREAM_KEY = f"{KEY_PREFIX}:shadow"
PENDING_LIMIT = 20
SHADOW_STREAM_MAX_ENTRIES = 10_000
WAKE_PAYLOAD_FIELD = "payload"
# The pending list's own expiry is only a backstop for a bot that stopped
# ticking: two 15-minute passive ticks past its newest envelope's window. Every
# push moves it out to the envelope it adds, and every trim to the newest one
# left, so it never deletes an envelope the tick has not had a chance to drop
# and count.
PENDING_EXPIRY_SLACK_MILLISECONDS = 30 * 60 * 1000

_PUSH_PENDING_LUA = """
local length = redis.call('RPUSH', KEYS[1], ARGV[1])
if redis.call('PTTL', KEYS[1]) < tonumber(ARGV[3]) then
  redis.call('PEXPIRE', KEYS[1], ARGV[3])
end
local limit = tonumber(ARGV[2])
local overflow = math.max(0, length - limit)
if overflow > 0 then
  redis.call('LTRIM', KEYS[1], overflow, -1)
  redis.call('INCRBY', KEYS[2], overflow)
  if redis.call('PTTL', KEYS[2]) < 0 then
    redis.call('PEXPIRE', KEYS[2], ARGV[4])
  end
end
return overflow
"""

_CLAIM_PENDING_LUA = """
if redis.call('EXISTS', KEYS[2]) == 0 then
  if redis.call('EXISTS', KEYS[1]) == 1 then
    redis.call('RENAME', KEYS[1], KEYS[2])
    redis.call('PEXPIRE', KEYS[2], ARGV[1])
  end
  local dropped = redis.call('GET', KEYS[3])
  if dropped then
    redis.call('SET', KEYS[4], dropped, 'PX', ARGV[1])
    redis.call('DEL', KEYS[3])
  end
end
local values = redis.call('LRANGE', KEYS[2], 0, -1)
local dropped = redis.call('GET', KEYS[4]) or '0'
table.insert(values, 1, dropped)
return values
"""

# Drop the named envelopes (read and judged too old by the caller) and count
# them, then move the list's backstop expiry to its newest remaining envelope.
# A claim that took the list meanwhile leaves nothing to remove or expire.
_TRIM_PENDING_LUA = """
local removed = 0
for i = 3, #ARGV do
  removed = removed + redis.call('LREM', KEYS[1], 1, ARGV[i])
end
if removed > 0 then
  redis.call('INCRBY', KEYS[2], removed)
  if redis.call('PTTL', KEYS[2]) < 0 then
    redis.call('PEXPIRE', KEYS[2], ARGV[2])
  end
end
if ARGV[1] ~= '' and redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('PEXPIREAT', KEYS[1], ARGV[1])
end
return removed
"""


def _is_snowflake(guild_id: str) -> bool:
    return guild_id.isdigit() and len(guild_id) <= 20


def _guild_tag(guild_id: str) -> str:
    if not _is_snowflake(guild_id):
        raise ValueError("guild_id must be a Discord snowflake")
    return f"{{guild:{guild_id}}}"


def wake_stream_key(guild_id: str) -> str:
    return f"{KEY_PREFIX}:{_guild_tag(guild_id)}:wake"


def pending_key(guild_id: str) -> str:
    return f"{KEY_PREFIX}:{_guild_tag(guild_id)}:pending"


def pending_dropped_key(guild_id: str) -> str:
    return f"{KEY_PREFIX}:{_guild_tag(guild_id)}:pending-dropped"


def ownership_key(guild_id: str) -> str:
    return f"{KEY_PREFIX}:{_guild_tag(guild_id)}:owner"


def batch_key(guild_id: str, wake_id: str) -> str:
    return f"{KEY_PREFIX}:{_guild_tag(guild_id)}:batch:{wake_id}"


def batch_dropped_key(guild_id: str, wake_id: str) -> str:
    return f"{batch_key(guild_id, wake_id)}:dropped"


@dataclass(frozen=True)
class ClaimedPending:
    notifications: tuple[NotificationEnvelope, ...]
    dropped: int


class RedisNotificationQueue:
    """Publish and atomically claim notifications for isolated guild queues."""

    def __init__(self, redis_client, *, pending_limit: int = PENDING_LIMIT):
        if pending_limit < 1:
            raise ValueError("pending_limit must be positive")
        self._redis = redis_client
        self._pending_limit = pending_limit

    async def set_execution_owner(self, guild_id: str, mode: str) -> None:
        """Fence worker side effects to guilds explicitly owned externally."""
        owner = "external" if mode == "external" else "embedded"
        await self._redis.set(ownership_key(guild_id), owner)

    async def publish(self, envelope: NotificationEnvelope) -> str | None:
        """Queue a non-waking envelope, or wake the guild with a bounded stream.

        The pending list a non-waking envelope enters is capped at
        ``pending_limit`` envelopes. ``trim_expired_envelopes`` drops each
        envelope once it is older than the retention window and counts it; the
        list's own expiry, moved out by each push to the envelope it adds and
        reset by each trim to the newest one left, is a backstop for a bot that
        stopped ticking. Its dropped counter
        expires one window after its first count.
        """
        payload = envelope.model_dump_json()
        if not envelope.wakes:
            await self._redis.eval(
                _PUSH_PENDING_LUA,
                2,
                pending_key(envelope.guild_id),
                pending_dropped_key(envelope.guild_id),
                payload,
                self._pending_limit,
                CONTENT_RETENTION_MILLISECONDS + PENDING_EXPIRY_SLACK_MILLISECONDS,
                CONTENT_RETENTION_MILLISECONDS,
            )
            return None

        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xadd(
                wake_stream_key(envelope.guild_id),
                {WAKE_PAYLOAD_FIELD: payload},
                **_exact_retention_age_bound(),
            )
            pipeline.sadd(READY_GUILDS_KEY, envelope.guild_id)
            pipeline.xadd(
                READY_STREAM_KEY,
                {"guild_id": envelope.guild_id},
            )
            stream_id, _, _ = await pipeline.execute()
        return _decode(stream_id)

    async def publish_shadow(self, envelope: NotificationEnvelope) -> str:
        """Record a canary envelope where production workers cannot consume it."""
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xadd(
                SHADOW_STREAM_KEY,
                {
                    "guild_id": envelope.guild_id,
                    WAKE_PAYLOAD_FIELD: envelope.model_dump_json(),
                },
                maxlen=SHADOW_STREAM_MAX_ENTRIES,
                approximate=True,
            )
            pipeline.xtrim(SHADOW_STREAM_KEY, **_exact_retention_age_bound())
            stream_id, _ = await pipeline.execute()
        return _decode(stream_id)

    async def trim_expired_envelopes(self, guild_ids: Iterable[str]) -> int:
        """Drop past-window envelopes from every stream no publish still reaches.

        A publish only bounds the stream it writes, so a guild whose last wake
        was its final one, and the shadow stream after canary mode ends, keep
        their envelopes until this runs. Visits the wake stream of every guild
        in ``guild_ids`` (the guilds the caller can see) and of every guild the
        ready index still names, so retention does not depend on the worker
        leaving that index untouched. The index is shared with the external
        worker, so a member that is not a snowflake is skipped with a warning
        rather than aborting the trim of every well-formed guild. The same
        guilds' pending lists are trimmed by envelope age
        (:meth:`trim_expired_pending`). Returns the number of envelopes dropped.
        """
        age_bound = _exact_retention_age_bound()
        indexed_guild_ids = {
            _decode(guild_id)
            for guild_id in await self._redis.smembers(READY_GUILDS_KEY)
        }
        stream_guild_ids = sorted(
            {*guild_ids, *_well_formed_guild_ids(indexed_guild_ids)}
        )
        async with self._redis.pipeline(transaction=False) as pipeline:
            for guild_id in stream_guild_ids:
                pipeline.xtrim(wake_stream_key(guild_id), **age_bound)
            pipeline.xtrim(SHADOW_STREAM_KEY, **age_bound)
            dropped = sum(await pipeline.execute())
        for guild_id in stream_guild_ids:
            try:
                dropped += await self.trim_expired_pending(guild_id)
            except RedisError:
                raise
            except Exception:
                # One guild's list must not stop the trim of every later one.
                log_exception(logger, "pending trim failed guild=%s", guild_id)
        return dropped

    async def trim_expired_pending(self, guild_id: str, *, now: datetime | None = None) -> int:
        """Drop and count the pending envelopes older than the retention window.

        An envelope's age is its ``created_at``; one that does not parse is
        dropped too, so is one carrying a field this version does not know.
        Each drop is added to ``pending-dropped``, which the next claim hands
        the agent. The list's backstop expiry is reset to two ticks past its
        newest remaining envelope's window. Returns the number dropped.
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(milliseconds=CONTENT_RETENTION_MILLISECONDS)
        expired: list[str] = []
        newest: datetime | None = None
        for raw in await self._redis.lrange(pending_key(guild_id), 0, -1):
            value = _decode(raw)
            try:
                created_at = NotificationEnvelope.model_validate_json(value).created_at
            except ValueError:
                expired.append(value)
                continue
            if created_at <= cutoff:
                expired.append(value)
            elif newest is None or created_at > newest:
                newest = created_at
        expire_at = (
            ""
            if newest is None
            else int(newest.timestamp() * 1000)
            + CONTENT_RETENTION_MILLISECONDS
            + PENDING_EXPIRY_SLACK_MILLISECONDS
        )
        if not expired and newest is None:
            return 0
        removed = await self._redis.eval(
            _TRIM_PENDING_LUA,
            2,
            pending_key(guild_id),
            pending_dropped_key(guild_id),
            expire_at,
            CONTENT_RETENTION_MILLISECONDS,
            *expired,
        )
        return int(removed)

    async def claim_pending(self, guild_id: str, wake_id: str) -> ClaimedPending:
        """Move the pending list into a batch that a retry of ``wake_id`` reads
        again and that expires with the retention window if never acknowledged."""
        raw = await self._redis.eval(
            _CLAIM_PENDING_LUA,
            4,
            pending_key(guild_id),
            batch_key(guild_id, wake_id),
            pending_dropped_key(guild_id),
            batch_dropped_key(guild_id, wake_id),
            CONTENT_RETENTION_MILLISECONDS,
        )
        dropped = int(_decode(raw[0]))
        notifications = tuple(
            NotificationEnvelope.model_validate_json(_decode(value))
            for value in raw[1:]
        )
        return ClaimedPending(notifications=notifications, dropped=dropped)

    async def acknowledge_pending(self, guild_id: str, wake_id: str) -> None:
        await self._redis.delete(
            batch_key(guild_id, wake_id),
            batch_dropped_key(guild_id, wake_id),
        )


def _exact_retention_age_bound() -> dict[str, object]:
    """Approximate trimming only drops whole macro nodes, so it spares a quiet
    stream whose entries all sit in the open head node."""
    return {
        "minid": oldest_retained_stream_id(datetime.now(UTC)),
        "approximate": False,
    }


def _well_formed_guild_ids(indexed_guild_ids: set[str]) -> set[str]:
    corrupt_members = {
        guild_id for guild_id in indexed_guild_ids if not _is_snowflake(guild_id)
    }
    for member in sorted(corrupt_members):
        logger.warning(
            "proactive ready index member is not a guild snowflake, skipped: %r",
            member,
        )
    return indexed_guild_ids - corrupt_members


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
