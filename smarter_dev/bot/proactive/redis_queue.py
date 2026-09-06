"""Redis producer primitives for guild-scoped proactive notifications.

Envelopes carry verbatim Discord message text, so every key that holds them is
bounded to ``CONTENT_RETENTION_WINDOW``: the wake and shadow streams are
trimmed exactly at the cutoff (approximate trimming skips a quiet stream whose
entries all sit in the open macro node) on every publish and again by
``trim_expired_envelopes`` on the bot's passive tick, and a claimed batch
expires at claim time. The pending list is the one exception; see
``publish``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

from smarter_dev.bot.proactive.contracts import NotificationEnvelope
from smarter_dev.shared.message_content import CONTENT_RETENTION_WINDOW
from smarter_dev.shared.message_content import oldest_retained_stream_id

KEY_PREFIX = "proactive:v1"
READY_GUILDS_KEY = f"{KEY_PREFIX}:guilds-with-wakes"
READY_STREAM_KEY = f"{KEY_PREFIX}:ready"
SHADOW_STREAM_KEY = f"{KEY_PREFIX}:shadow"
PENDING_LIMIT = 20
SHADOW_STREAM_MAX_ENTRIES = 10_000
RETENTION_WINDOW_MILLISECONDS = int(CONTENT_RETENTION_WINDOW.total_seconds() * 1000)
WAKE_PAYLOAD_FIELD = "payload"

_PUSH_PENDING_LUA = """
local length = redis.call('RPUSH', KEYS[1], ARGV[1])
local limit = tonumber(ARGV[2])
local overflow = math.max(0, length - limit)
if overflow > 0 then
  redis.call('LTRIM', KEYS[1], overflow, -1)
  redis.call('INCRBY', KEYS[2], overflow)
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


def _guild_tag(guild_id: str) -> str:
    if not guild_id.isdigit() or len(guild_id) > 20:
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

        The pending list a non-waking envelope enters is capped by count, not
        age: it is drained by the next wake, and a guild that never wakes
        again keeps up to ``pending_limit`` envelopes until it does.
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
        leaving that index untouched. Returns the number of envelopes dropped.
        """
        age_bound = _exact_retention_age_bound()
        indexed_guild_ids = await self._redis.smembers(READY_GUILDS_KEY)
        stream_guild_ids = sorted(
            {*guild_ids, *(_decode(guild_id) for guild_id in indexed_guild_ids)}
        )
        async with self._redis.pipeline(transaction=False) as pipeline:
            for guild_id in stream_guild_ids:
                pipeline.xtrim(wake_stream_key(guild_id), **age_bound)
            pipeline.xtrim(SHADOW_STREAM_KEY, **age_bound)
            return sum(await pipeline.execute())

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
            RETENTION_WINDOW_MILLISECONDS,
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


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
