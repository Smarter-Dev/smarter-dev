"""Redis persistence for the proactive agent's cross-wake history.

Mirrors ChatMemory.write_history (the chat bot's working-history store):
the full pydantic-ai message list, JSON-dumped under a per-channel key on
the same Redis the chat memory uses. A longer TTL than chat's 2h — the
proactive agent's history is day-scale context, already bounded by the
100k-token compaction.
"""

from __future__ import annotations

from datetime import timedelta

import pydantic
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

HISTORY_TTL_SECONDS = int(timedelta(hours=24).total_seconds())
KEY_PREFIX = "proactive"


class ProactiveHistoryStore:
    """Per-channel agent history on the shared chat-memory Redis."""

    def __init__(self, redis_client):
        self._redis = redis_client

    @staticmethod
    def _history_key(channel_id: int) -> str:
        return f"{KEY_PREFIX}:{channel_id}:history"

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
        await self._redis.set(
            self._history_key(channel_id), payload, ex=HISTORY_TTL_SECONDS
        )

    async def clear(self, channel_id: int) -> None:
        await self._redis.delete(self._history_key(channel_id))
