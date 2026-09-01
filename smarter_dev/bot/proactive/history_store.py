"""Redis persistence for the proactive agent's cross-wake history.

Mirrors ChatMemory.write_history (the chat bot's working-history store):
the full pydantic-ai message list, JSON-dumped under a per-channel key on
the same Redis the chat memory uses. History keys never expire — the
rolling context IS the agent's extended memory, and it is already bounded
in size by the 100k-token compaction.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pydantic
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

CURSOR_TTL_SECONDS = int(timedelta(days=7).total_seconds())
KEY_PREFIX = "proactive"


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


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
        raw = await self._redis.get(self._history_key(channel_id))
        if not raw:
            return []
        try:
            return list(ModelMessagesTypeAdapter.validate_json(raw))
        except pydantic.ValidationError:
            # A pydantic-ai upgrade can invalidate stored messages; stale
            # history is a cache, not a source of truth — start fresh.
            return []

    async def write(self, channel_id: int, messages: list[ModelMessage]) -> None:
        payload = ModelMessagesTypeAdapter.dump_json(messages)
        await self._redis.set(self._history_key(channel_id), payload)

    async def read_guild(self, guild_id: int) -> list[ModelMessage]:
        raw = await self._redis.get(self._guild_history_key(guild_id))
        if not raw:
            return []
        try:
            return list(ModelMessagesTypeAdapter.validate_json(raw))
        except pydantic.ValidationError:
            return []

    async def write_guild(
        self, guild_id: int, messages: list[ModelMessage]
    ) -> None:
        payload = ModelMessagesTypeAdapter.dump_json(messages)
        await self._redis.set(self._guild_history_key(guild_id), payload)

    async def clear(self, channel_id: int) -> None:
        await self._redis.delete(self._history_key(channel_id))

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
