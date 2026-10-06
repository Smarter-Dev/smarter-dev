"""Redis persistence for the proactive agent's cross-wake history.

Mirrors ChatMemory.write_history (the chat bot's working-history store):
the full pydantic-ai message list, JSON-dumped under a per-channel key on
the same Redis the chat memory uses. History keys never expire — the
rolling context IS the agent's extended memory, bounded in size by the
100k-token compaction and in age by the idle sweep: every guild-history
write stamps the guild in ``IDLE_INDEX_KEY`` (score = epoch seconds of the
write), and once a guild's score is older than the idle window the sweep
folds the history to its memory note alone (``plugins.proactive``).

A write the wake makes right after its own compaction (note + kept tail,
nothing newer) also flags the guild in ``FRESH_SET_KEY``; any other write
clears it. The index and flag are written before the history, so a failure
between them can only make the sweep act sooner (or with a model call where
it needed none), never leave a history unswept.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pydantic
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

CURSOR_TTL_SECONDS = int(timedelta(days=7).total_seconds())
KEY_PREFIX = "proactive"
# Outside the ``proactive:guild-history:*`` pattern the purge scans.
IDLE_INDEX_KEY = f"{KEY_PREFIX}:guild-history-idle"
FRESH_SET_KEY = f"{KEY_PREFIX}:guild-history-fresh"


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


class HistoryUnreadable(ValueError):
    """Stored history bytes that do not parse. Content-free on purpose."""


def _parse(raw) -> list[ModelMessage]:
    if not raw:
        return []
    try:
        return list(ModelMessagesTypeAdapter.validate_json(raw))
    except (pydantic.ValidationError, ValueError) as error:
        # Never chain the validation error: its text quotes the stored input.
        raise HistoryUnreadable(type(error).__name__) from None


def _as_bytes(value) -> bytes | None:
    if value is None:
        return None
    return value.encode() if isinstance(value, str) else value


class ProactiveHistoryStore:
    """Agent history and recovery cursors on the shared chat-memory Redis."""

    def __init__(self, redis_client):
        self._redis = redis_client

    @staticmethod
    def _history_key(channel_id: int) -> str:
        return f"{KEY_PREFIX}:{channel_id}:history"

    @staticmethod
    def _guild_history_key(guild_id: int) -> str:
        return f"{KEY_PREFIX}:guild-history:{guild_id}"

    async def read(self, channel_id: int) -> list[ModelMessage]:
        """The stored history; [] when absent. Raises ``HistoryUnreadable``
        when the bytes do not parse: unreadable is not empty, and the caller
        must not write over it (it may be memory a purge still has to see)."""
        return _parse(await self._redis.get(self._history_key(channel_id)))

    async def read_raw(self, channel_id: int) -> bytes | None:
        """The stored bytes, unparsed (a purge must tell unreadable from
        empty, and never discard either)."""
        return _as_bytes(await self._redis.get(self._history_key(channel_id)))

    async def read_guild_raw(self, guild_id: int) -> bytes | None:
        return _as_bytes(await self._redis.get(self._guild_history_key(guild_id)))

    async def write(self, channel_id: int, messages: list[ModelMessage]) -> None:
        payload = ModelMessagesTypeAdapter.dump_json(messages)
        await self._redis.set(self._history_key(channel_id), payload)

    async def rewrite(self, channel_id: int, messages: list[ModelMessage]) -> None:
        """Replace a legacy channel history only while it still exists, so a
        purge that read it before the sweep deleted it cannot bring it back."""
        payload = ModelMessagesTypeAdapter.dump_json(messages)
        await self._redis.set(self._history_key(channel_id), payload, xx=True)

    async def read_guild(self, guild_id: int) -> list[ModelMessage]:
        """Like ``read``: [] when absent, ``HistoryUnreadable`` when the
        stored bytes do not parse."""
        return _parse(await self._redis.get(self._guild_history_key(guild_id)))

    async def write_guild(
        self,
        guild_id: int,
        messages: list[ModelMessage],
        *,
        freshly_compacted: bool = False,
        keep_clock: bool = False,
    ) -> None:
        """Store the guild history and restart its idle clock.

        ``freshly_compacted`` marks a history that is exactly a compaction's
        output (note + kept tail); any write without it clears the mark.
        ``keep_clock`` (a privacy purge's rewrite) leaves the idle clock and
        the mark as they were: a purge is not activity, and must not extend
        how long the rest of the history stays verbatim.
        """
        payload = ModelMessagesTypeAdapter.dump_json(messages)
        member = str(guild_id)
        if keep_clock:
            await self._redis.zadd(IDLE_INDEX_KEY, {member: time.time()}, nx=True)
            await self._redis.set(self._guild_history_key(guild_id), payload)
            return
        await self._redis.zadd(IDLE_INDEX_KEY, {member: time.time()})
        if freshly_compacted:
            await self._redis.sadd(FRESH_SET_KEY, member)
        else:
            await self._redis.srem(FRESH_SET_KEY, member)
        await self._redis.set(self._guild_history_key(guild_id), payload)

    async def write_guild_idle_compacted(
        self, guild_id: int, messages: list[ModelMessage]
    ) -> None:
        """Store the idle sweep's summary-only history. It holds nothing
        verbatim, so the guild leaves the idle index until its next write."""
        payload = ModelMessagesTypeAdapter.dump_json(messages)
        await self._redis.set(self._guild_history_key(guild_id), payload)
        await self.forget_idle(guild_id)

    async def delete_guild(self, guild_id: int) -> None:
        """Drop the guild history and its idle state."""
        await self._redis.delete(self._guild_history_key(guild_id))
        await self.forget_idle(guild_id)

    async def forget_idle(self, guild_id: int) -> None:
        member = str(guild_id)
        await self._redis.srem(FRESH_SET_KEY, member)
        await self._redis.zrem(IDLE_INDEX_KEY, member)

    async def idle_guild_ids(self, *, written_before: float) -> list[int]:
        """Guilds whose history was last written before ``written_before``."""
        members = await self._redis.zrangebyscore(
            IDLE_INDEX_KEY, "-inf", written_before
        )
        return [int(_decode(member)) for member in members]

    async def guild_idle_state(self, guild_id: int) -> tuple[float | None, bool]:
        """(epoch of the last write or None if unindexed, fresh flag)."""
        member = str(guild_id)
        written_at = await self._redis.zscore(IDLE_INDEX_KEY, member)
        fresh = bool(await self._redis.sismember(FRESH_SET_KEY, member))
        return written_at, fresh

    async def index_unindexed_guild_histories(self, *, now: float) -> int:
        """Start the idle clock, at ``now``, for guild histories written
        before the index existed (or by anything that skipped it)."""
        added = 0
        async for key in self._redis.scan_iter(
            match=f"{KEY_PREFIX}:guild-history:*"
        ):
            guild_id = _decode(key).rsplit(":", 1)[-1]
            if guild_id.isdigit():
                added += await self._redis.zadd(
                    IDLE_INDEX_KEY, {guild_id: now}, nx=True
                )
        return added

    async def delete_legacy_channel_histories(self) -> int:
        """Delete every per-channel ``proactive:{channel}:history`` key.

        Nothing has written them since the history moved to one key per
        guild, and they carry verbatim text with no age bound. The pattern
        also matches the worker's ``proactive:v1:{guild:g}:history``; only a
        numeric middle part (a channel id) is deleted."""
        deleted = 0
        async for key in self._redis.scan_iter(match=f"{KEY_PREFIX}:*:history"):
            parts = _decode(key).split(":")
            if len(parts) == 3 and parts[1].isdigit():
                deleted += await self._redis.delete(key)
        return deleted

    async def clear(self, channel_id: int) -> None:
        await self._redis.delete(self._history_key(channel_id))

    # -- active-ingest window (survives restarts) --

    @staticmethod
    def _active_until_key(channel_id: int) -> str:
        return f"{KEY_PREFIX}:{channel_id}:active-until"

    async def read_active_until(self, channel_id: int) -> float | None:
        """Wall-clock epoch when the channel's active window ends."""
        raw = await self._redis.get(self._active_until_key(channel_id))
        if not raw:
            return None
        try:
            return float(_decode(raw))
        except ValueError:
            return None

    async def write_active_until(
        self, channel_id: int, *, until_epoch: float, ttl_seconds: int
    ) -> None:
        await self._redis.set(
            self._active_until_key(channel_id),
            str(until_epoch),
            ex=ttl_seconds,
        )

    # -- last-processed cursor (restart recovery) --

    @staticmethod
    def _cursor_key(channel_id: int) -> str:
        return f"{KEY_PREFIX}:{channel_id}:cursor"

    async def read_cursor(self, channel_id: int) -> dict | None:
        raw = await self._redis.get(self._cursor_key(channel_id))
        if not raw:
            return None
        try:
            return json.loads(_decode(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    async def write_cursor(
        self, channel_id: int, *, guild_id: str, last_message_id: str
    ) -> None:
        await self._redis.set(
            self._cursor_key(channel_id),
            json.dumps({"guild_id": guild_id, "last_message_id": last_message_id}),
            ex=CURSOR_TTL_SECONDS,
        )

    async def cursor_channel_ids(self) -> list[int]:
        """Channels with a stored cursor — the restart-recovery scan set."""
        channel_ids = []
        async for key in self._redis.scan_iter(
            match=f"{KEY_PREFIX}:*:cursor"
        ):
            middle = _decode(key).split(":")[1]
            if middle.isdigit():
                channel_ids.append(int(middle))
        return sorted(channel_ids)
