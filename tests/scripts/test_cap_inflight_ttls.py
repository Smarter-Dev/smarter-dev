"""Tests for the one-off that brings old Redis keys onto the in-flight bound."""

from __future__ import annotations

import json
import sys
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts import cap_inflight_ttls  # noqa: E402
from smarter_dev.shared.retention_policy import IN_FLIGHT_MAX  # noqa: E402

fakeredis_aioredis = pytest.importorskip("fakeredis.aioredis")

BATCH = "proactive:v1:{guild:1}:batch:wake-1"
TWO_DAYS_MS = int(timedelta(days=2).total_seconds() * 1000)


def _envelope(age: timedelta) -> str:
    return json.dumps({"created_at": (datetime.now(UTC) - age).isoformat()})


@pytest.fixture
def redis_client():
    return fakeredis_aioredis.FakeRedis(decode_responses=False)


@pytest.mark.asyncio
async def test_an_old_batch_expires_six_hours_after_its_oldest_envelope(redis_client):
    oldest = timedelta(hours=2)
    await redis_client.rpush(BATCH, _envelope(timedelta(hours=1)), _envelope(oldest))
    await redis_client.pexpire(BATCH, TWO_DAYS_MS)
    await redis_client.set(f"{BATCH}:dropped", 3, px=TWO_DAYS_MS)

    counts = await cap_inflight_ttls._cap_batches(redis_client)

    left = await redis_client.pttl(BATCH) / 1000
    assert counts == {"batches_capped": 1, "batches_deleted": 0}
    assert left == pytest.approx((IN_FLIGHT_MAX - oldest).total_seconds(), abs=5)
    assert await redis_client.pttl(f"{BATCH}:dropped") > TWO_DAYS_MS - 60_000


@pytest.mark.asyncio
async def test_a_batch_past_the_bound_goes_now(redis_client):
    await redis_client.rpush(BATCH, _envelope(timedelta(hours=7)))
    await redis_client.pexpire(BATCH, TWO_DAYS_MS)

    await cap_inflight_ttls._cap_batches(redis_client)

    assert not await redis_client.exists(BATCH)


@pytest.mark.asyncio
async def test_a_batch_with_no_readable_time_is_deleted(redis_client):
    await redis_client.rpush(BATCH, "not json")

    counts = await cap_inflight_ttls._cap_batches(redis_client)

    assert counts["batches_deleted"] == 1
    assert not await redis_client.exists(BATCH)


@pytest.mark.asyncio
async def test_a_shorter_expiry_is_never_extended(redis_client):
    await redis_client.set("mediaread:a:b", "read", ex=60)
    await redis_client.set("mediaread:c:d", "read", ex=86_400)
    await redis_client.set("chat_agent:5:topic", "topic", ex=86_400)

    assert await cap_inflight_ttls._cap(redis_client, "mediaread:*", 3600) == 2
    await cap_inflight_ttls._cap(redis_client, "chat_agent:*:topic", 6 * 3600)

    assert await redis_client.ttl("mediaread:a:b") <= 60
    assert 3590 <= await redis_client.ttl("mediaread:c:d") <= 3600
    assert await redis_client.ttl("chat_agent:5:topic") <= 6 * 3600
