"""Tests for the ChatMemory Redis wrapper.

Uses ``fakeredis`` so the tests don't need a live Redis server. The Redis
client we install is the async version so it matches the production code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

try:
    import fakeredis.aioredis as fakeredis_aioredis
except ImportError:  # pragma: no cover - fakeredis optional dependency
    fakeredis_aioredis = None

from smarter_dev.bot.services.chat_memory import (
    TOPIC_STALE_AFTER,
    TOPIC_STALE_AFTER_MESSAGES,
    ChatMemory,
    Topic,
)

pytestmark = pytest.mark.skipif(
    fakeredis_aioredis is None,
    reason="fakeredis is not installed",
)


@pytest.fixture
async def memory():
    client = fakeredis_aioredis.FakeRedis()
    return ChatMemory(client)


@pytest.mark.asyncio
async def test_write_and_get_topic_round_trips(memory):
    await memory.write_topic(42, "Talking about async/await")
    topic = await memory.get_topic(42)
    assert isinstance(topic, Topic)
    assert topic.text == "Talking about async/await"
    assert (datetime.now(UTC) - topic.written_at).total_seconds() < 5


@pytest.mark.asyncio
async def test_get_topic_returns_none_when_missing(memory):
    assert await memory.get_topic(99) is None


@pytest.mark.asyncio
async def test_notes_round_trip_and_clear(memory):
    await memory.write_notes(7, "Alice asked about FastAPI; Bob wants a small example.")
    assert await memory.get_notes(7) == "Alice asked about FastAPI; Bob wants a small example."
    await memory.clear_notes(7)
    assert await memory.get_notes(7) is None


@pytest.mark.asyncio
async def test_idle_counter_increments_and_resets(memory):
    assert await memory.get_idle_counter(11) == 0
    for _ in range(3):
        await memory.increment_idle_counter(11)
    assert await memory.get_idle_counter(11) == 3
    await memory.reset_idle_counter(11)
    assert await memory.get_idle_counter(11) == 0


@pytest.mark.asyncio
async def test_topic_for_activation_returns_text_when_fresh(memory):
    await memory.write_topic(5, "fresh topic")
    assert await memory.topic_for_activation(5) == "fresh topic"


@pytest.mark.asyncio
async def test_topic_for_activation_none_when_too_old(memory):
    # Write a topic with a manually-aged timestamp.
    aged = (datetime.now(UTC) - TOPIC_STALE_AFTER - timedelta(minutes=1)).isoformat()
    await memory._redis.set(memory._topic_key(8), "ancient topic")
    await memory._redis.set(memory._topic_ts_key(8), aged)
    assert await memory.topic_for_activation(8) is None


@pytest.mark.asyncio
async def test_topic_for_activation_none_when_too_many_idle_messages(memory):
    await memory.write_topic(13, "topic that got buried")
    for _ in range(TOPIC_STALE_AFTER_MESSAGES + 1):
        await memory.increment_idle_counter(13)
    assert await memory.topic_for_activation(13) is None
