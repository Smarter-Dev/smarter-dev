"""Redis-backed memory for the chat agent.

Per-channel:
- `topic` (with timestamp) — 1-2 sentence summary written on every agent turn.
  Considered "stale" if older than 6 hours OR if more than 25 channel messages
  have arrived since the agent was last active.
- `notes` — 1-5 sentence working notes, written on each SendResponse and
  carried into the next turn within the same engagement. Cleared when the
  agent deactivates.
- `idle_msg_count` — number of channel messages observed while the agent is
  not actively watching the channel. Drives the staleness check above.

The store is non-critical: persistence loss only forfeits the topic/notes
context for ongoing conversations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "chat_agent"
TOPIC_TTL_SECONDS = int(timedelta(hours=24).total_seconds())
NOTES_TTL_SECONDS = int(timedelta(hours=2).total_seconds())
COUNTER_TTL_SECONDS = int(timedelta(hours=24).total_seconds())

TOPIC_STALE_AFTER = timedelta(hours=6)
TOPIC_STALE_AFTER_MESSAGES = 25


@dataclass(frozen=True)
class Topic:
    text: str
    written_at: datetime


class ChatMemory:
    """Thin wrapper over Redis for chat-agent per-channel memory."""

    def __init__(self, client: redis.Redis):
        self._redis = client

    @staticmethod
    def _topic_key(channel_id: int) -> str:
        return f"{KEY_PREFIX}:{channel_id}:topic"

    @staticmethod
    def _topic_ts_key(channel_id: int) -> str:
        return f"{KEY_PREFIX}:{channel_id}:topic_ts"

    @staticmethod
    def _notes_key(channel_id: int) -> str:
        return f"{KEY_PREFIX}:{channel_id}:notes"

    @staticmethod
    def _counter_key(channel_id: int) -> str:
        return f"{KEY_PREFIX}:{channel_id}:idle_msg_count"

    async def get_topic(self, channel_id: int) -> Topic | None:
        text_raw, ts_raw = await self._redis.mget(
            self._topic_key(channel_id),
            self._topic_ts_key(channel_id),
        )
        if not text_raw or not ts_raw:
            return None
        try:
            written_at = datetime.fromisoformat(_decode(ts_raw))
        except ValueError:
            logger.warning("Discarding malformed topic timestamp for channel %s", channel_id)
            return None
        return Topic(text=_decode(text_raw), written_at=written_at)

    async def write_topic(self, channel_id: int, text: str) -> None:
        now = datetime.now(UTC).isoformat()
        pipe = self._redis.pipeline()
        pipe.set(self._topic_key(channel_id), text, ex=TOPIC_TTL_SECONDS)
        pipe.set(self._topic_ts_key(channel_id), now, ex=TOPIC_TTL_SECONDS)
        await pipe.execute()

    async def get_notes(self, channel_id: int) -> str | None:
        raw = await self._redis.get(self._notes_key(channel_id))
        return _decode(raw) if raw else None

    async def write_notes(self, channel_id: int, text: str) -> None:
        await self._redis.set(self._notes_key(channel_id), text, ex=NOTES_TTL_SECONDS)

    async def clear_notes(self, channel_id: int) -> None:
        await self._redis.delete(self._notes_key(channel_id))

    async def increment_idle_counter(self, channel_id: int) -> int:
        count = await self._redis.incr(self._counter_key(channel_id))
        await self._redis.expire(self._counter_key(channel_id), COUNTER_TTL_SECONDS)
        return int(count)

    async def reset_idle_counter(self, channel_id: int) -> None:
        await self._redis.delete(self._counter_key(channel_id))

    async def get_idle_counter(self, channel_id: int) -> int:
        raw = await self._redis.get(self._counter_key(channel_id))
        return int(_decode(raw)) if raw else 0

    async def topic_for_activation(self, channel_id: int) -> str | None:
        """Return the topic if it isn't stale; otherwise None.

        Stale = older than 6h OR more than 25 idle channel messages observed
        since the agent was last active in this channel.
        """
        topic = await self.get_topic(channel_id)
        if topic is None:
            return None
        if datetime.now(UTC) - topic.written_at > TOPIC_STALE_AFTER:
            return None
        idle = await self.get_idle_counter(channel_id)
        if idle > TOPIC_STALE_AFTER_MESSAGES:
            return None
        return topic.text


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


_memory: ChatMemory | None = None


def init_chat_memory(client: redis.Redis) -> ChatMemory:
    """Install the global ChatMemory wrapping the provided Redis client."""
    global _memory
    _memory = ChatMemory(client)
    return _memory


def get_chat_memory() -> ChatMemory:
    """Return the installed ChatMemory, raising if init wasn't called."""
    if _memory is None:
        raise RuntimeError(
            "ChatMemory not initialised — call init_chat_memory() during bot startup."
        )
    return _memory
