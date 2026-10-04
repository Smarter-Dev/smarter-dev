"""Redis-backed memory for the chat agent.

Per-channel:
- `topic` (with timestamp) — 1-2 sentence summary written on every agent turn.
  Considered "stale" if older than 6 hours OR if more than 25 channel messages
  have arrived since the agent was last active.
- `notes` — 1-5 sentence topic-tracker, written on each SendResponse and
  carried forward across activations until the engine deactivates.
- `history` — full Pydantic AI ``list[ModelMessage]`` from the last
  ``result.all_messages()``. Loaded at the start of every follow-up turn,
  written at the end. Cleared on engine deactivation so a new activation
  starts a fresh conversation.
- `idle_msg_count` — number of channel messages observed while the agent is
  not actively watching the channel. Drives the staleness check above.

The store is non-critical: persistence loss only forfeits the topic/notes/
history context for ongoing conversations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

logger = logging.getLogger(__name__)

KEY_PREFIX = "chat_agent"
TOPIC_TTL_SECONDS = int(timedelta(hours=24).total_seconds())
NOTES_TTL_SECONDS = int(timedelta(hours=2).total_seconds())
HISTORY_TTL_SECONDS = int(timedelta(hours=2).total_seconds())
COUNTER_TTL_SECONDS = int(timedelta(hours=24).total_seconds())

# Held per guild by a privacy purge while it rewrites chat memory; a turn
# that would read history defers while it exists (see ChannelEngine).
CHAT_PRIVACY_LOCK_PREFIX = "privacy:v1:chat-lock"

# Compare-and-set for a purge rewrite: only replace the history if it is
# still byte-for-byte what the purge folded.
_REPLACE_IF_UNCHANGED = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "redis.call('set', KEYS[1], ARGV[2], 'KEEPTTL') return 1 else return 0 end"
)

# A turn's history write that loaded ``expected`` ('' = absent).
_WRITE_IF_UNCHANGED = (
    "local current = redis.call('get', KEYS[1]) "
    "if (current == false and ARGV[1] == '') or current == ARGV[1] then "
    "redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3]) return 1 end return 0"
)
_UNCONDITIONAL = object()
# read_history_versioned's version for a stored history it could not parse.
HISTORY_UNREADABLE = object()

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
    def _history_key(channel_id: int) -> str:
        return f"{KEY_PREFIX}:{channel_id}:history"

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
            text = _decode(text_raw)
        except (ValueError, UnicodeDecodeError) as error:
            logger.warning(
                "Ignoring unreadable topic for channel %s (%s)",
                channel_id,
                type(error).__name__,
            )
            return None
        return Topic(text=text, written_at=written_at)

    async def get_topic_text(self, channel_id: int) -> str | None:
        """The stored topic text whatever its timestamp (privacy purge)."""
        raw = await self._redis.get(self._topic_key(channel_id))
        return _decode(raw) if raw else None

    async def get_notes_text(self, channel_id: int) -> str | None:
        """The stored notes; raises UnicodeDecodeError when unreadable
        (privacy purge: unreadable is reported, never treated as empty)."""
        raw = await self._redis.get(self._notes_key(channel_id))
        return _decode(raw) if raw else None

    async def _unreadable(self, key: str) -> bool:
        """A stored text value that is not valid UTF-8. It is never
        overwritten (or deleted) by a turn: it may be someone's memory."""
        raw = await self._redis.get(key)
        if not raw or isinstance(raw, str):
            return False
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Leaving unreadable chat memory in place (UnicodeDecodeError)")
            return True
        return False

    async def write_topic(self, channel_id: int, text: str) -> None:
        if await self._unreadable(self._topic_key(channel_id)):
            return
        now = datetime.now(UTC).isoformat()
        pipe = self._redis.pipeline()
        pipe.set(self._topic_key(channel_id), text, ex=TOPIC_TTL_SECONDS)
        pipe.set(self._topic_ts_key(channel_id), now, ex=TOPIC_TTL_SECONDS)
        await pipe.execute()

    async def get_notes(self, channel_id: int) -> str | None:
        raw = await self._redis.get(self._notes_key(channel_id))
        if not raw:
            return None
        try:
            return _decode(raw)
        except UnicodeDecodeError:
            logger.warning(
                "Ignoring unreadable notes for channel %s (UnicodeDecodeError)",
                channel_id,
            )
            return None

    async def write_notes(self, channel_id: int, text: str) -> None:
        if await self._unreadable(self._notes_key(channel_id)):
            return
        await self._redis.set(self._notes_key(channel_id), text, ex=NOTES_TTL_SECONDS)

    async def clear_notes(self, channel_id: int) -> None:
        await self._redis.delete(self._notes_key(channel_id))

    async def read_history(self, channel_id: int) -> list[ModelMessage]:
        """Return the persisted Pydantic AI message history, or [] if missing
        or unreadable (an unreadable one is left in place, see below)."""
        messages, _raw = await self.read_history_versioned(channel_id)
        return messages

    async def read_history_versioned(
        self, channel_id: int
    ) -> tuple[list[ModelMessage], bytes | None | object]:
        """The history plus the exact bytes it was read from (None if none),
        for a later ``write_history(..., expected_raw=...)``.

        An unreadable history is never deleted (it may hold someone's memory
        a purge must still see) and never logged beyond the error type: the
        turn runs with an empty history and ``HISTORY_UNREADABLE`` as its
        version, which makes the turn's write a no-op.
        """
        raw = await self._redis.get(self._history_key(channel_id))
        if not raw:
            return [], None
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        try:
            return list(ModelMessagesTypeAdapter.validate_json(raw)), raw
        except Exception as error:  # noqa: BLE001 — any shape a build wrote
            logger.warning(
                "Chat history for channel %s is unreadable (%s); left in place, "
                "this turn keeps no history",
                channel_id,
                type(error).__name__,
            )
            return [], HISTORY_UNREADABLE

    async def write_history(
        self,
        channel_id: int,
        messages: list[ModelMessage],
        *,
        expected_raw: bytes | None | object = _UNCONDITIONAL,
    ) -> bool:
        """Persist the full message list for the next turn to pick up.

        With ``expected_raw`` the write happens only if the stored history is
        still what the turn loaded (``None``: still absent). A privacy purge
        that rewrote it meanwhile wins and the turn's write is dropped.
        Returns whether it was written.
        """
        if expected_raw is HISTORY_UNREADABLE:
            return False
        payload = ModelMessagesTypeAdapter.dump_json(messages)
        if expected_raw is _UNCONDITIONAL:
            await self._redis.set(
                self._history_key(channel_id), payload, ex=HISTORY_TTL_SECONDS
            )
            return True
        return bool(
            await self._redis.eval(
                _WRITE_IF_UNCHANGED,
                1,
                self._history_key(channel_id),
                expected_raw or b"",
                payload,
                HISTORY_TTL_SECONDS,
            )
        )

    async def clear_history(self, channel_id: int) -> None:
        await self._redis.delete(self._history_key(channel_id))

    # -- privacy purge rewrites: same keys, the original expiry is kept so a
    # purge never extends how long memory lives.

    async def read_history_raw(self, channel_id: int) -> bytes | None:
        """The stored bytes, untouched (a purge never discards history)."""
        raw = await self._redis.get(self._history_key(channel_id))
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return raw

    async def replace_history_if_unchanged(
        self, channel_id: int, expected_raw: bytes, messages: list[ModelMessage]
    ) -> bool:
        """Replace the history only if it still equals ``expected_raw``."""
        payload = ModelMessagesTypeAdapter.dump_json(messages)
        return bool(
            await self._redis.eval(
                _REPLACE_IF_UNCHANGED,
                1,
                self._history_key(channel_id),
                expected_raw,
                payload,
            )
        )

    async def privacy_locked(self, guild_id: int | str) -> bool:
        """Whether a privacy purge is rewriting this guild's chat memory."""
        return bool(await self._redis.exists(chat_privacy_lock_key(guild_id)))

    async def replace_topic(self, channel_id: int, text: str) -> bool:
        """Rewrite the topic text, keeping its written-at stamp and expiry.

        Only an existing key is rewritten (XX): one that expired meanwhile is
        not recreated without an expiry. Returns whether it was written.
        """
        return bool(
            await self._redis.set(
                self._topic_key(channel_id), text, keepttl=True, xx=True
            )
        )

    async def replace_notes(self, channel_id: int, text: str) -> bool:
        return bool(
            await self._redis.set(
                self._notes_key(channel_id), text, keepttl=True, xx=True
            )
        )

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


def chat_privacy_lock_key(guild_id: int | str) -> str:
    return f"{CHAT_PRIVACY_LOCK_PREFIX}:{guild_id}"


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


async def chat_privacy_locked(guild_id: int | str) -> bool:
    """Whether a privacy purge holds this guild's chat lock. False when no
    chat memory is installed: then there is no stored history to protect."""
    if _memory is None:
        return False
    return await _memory.privacy_locked(guild_id)


def get_chat_memory() -> ChatMemory:
    """Return the installed ChatMemory, raising if init wasn't called."""
    if _memory is None:
        raise RuntimeError(
            "ChatMemory not initialised — call init_chat_memory() during bot startup."
        )
    return _memory
