"""Contract and Redis behavior for the extracted proactive-agent boundary."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.proactive.contracts import ControlCommand
from smarter_dev.bot.proactive.contracts import NotificationEnvelope
from smarter_dev.bot.proactive.notifications import Notification
from smarter_dev.bot.proactive.redis_queue import READY_GUILDS_KEY
from smarter_dev.bot.proactive.redis_queue import READY_STREAM_KEY
from smarter_dev.bot.proactive.redis_queue import SHADOW_STREAM_KEY
from smarter_dev.bot.proactive.redis_queue import RedisNotificationQueue
from smarter_dev.bot.proactive.redis_queue import ownership_key
from smarter_dev.bot.proactive.redis_queue import pending_key
from smarter_dev.bot.proactive.redis_queue import wake_stream_key

try:
    import fakeredis.aioredis as fakeredis_aioredis
except ImportError:  # pragma: no cover - dev-only dependency
    fakeredis_aioredis = None


pytestmark = pytest.mark.skipif(
    fakeredis_aioredis is None,
    reason="fakeredis is not installed",
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def redis_client():
    return fakeredis_aioredis.FakeRedis(decode_responses=False)


def _envelope(
    *,
    guild_id: str = "111",
    channel_id: str = "222",
    kind: str = "reaction",
    wakes: bool = False,
    body: str = "body",
) -> NotificationEnvelope:
    return NotificationEnvelope(
        guild_id=guild_id,
        channel_id=channel_id,
        channel_name="general",
        kind=kind,
        created_at=datetime(2026, 9, 1, 16, 0, tzinfo=UTC),
        body=body,
        message_ids=("333",),
        wakes=wakes,
    )


def test_notification_round_trip_preserves_wake_brief_fields():
    original = Notification(
        kind="mention",
        created_at=datetime(2026, 9, 1, 16, 0, tzinfo=UTC),
        body="verbatim mention",
        channel_id="222",
        channel_name="general",
        message_ids=("333",),
        wakes=True,
    )

    envelope = NotificationEnvelope.from_notification(
        original,
        guild_id="111",
        passive=True,
        watcher_usage={
            "watcher-model": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_tokens": 1,
            }
        },
    )
    restored = NotificationEnvelope.model_validate_json(
        envelope.model_dump_json()
    ).to_notification()

    assert restored == original
    assert envelope.passive is True
    assert envelope.watcher_usage["watcher-model"].input_tokens == 10


def test_wire_models_match_canonical_json_schemas():
    jsonschema = pytest.importorskip("jsonschema")
    notification_schema = json.loads(
        (ROOT / "contracts/proactive/v1/notification.schema.json").read_text()
    )
    control_schema = json.loads(
        (ROOT / "contracts/proactive/v1/control-command.schema.json").read_text()
    )
    envelope = _envelope(wakes=True, kind="mention")
    command = ControlCommand(
        guild_id="111",
        channel_id="222",
        mode="active",
        minutes=10,
        created_at=datetime(2026, 9, 1, 16, 0, tzinfo=UTC),
    )

    jsonschema.validate(envelope.model_dump(mode="json"), notification_schema)
    jsonschema.validate(command.model_dump(mode="json"), control_schema)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["wake", "pending", "shadow"])
async def test_redis_notification_timestamp_is_iso_utc(redis_client, route):
    notification = Notification(
        kind="mention",
        created_at=datetime(2026, 9, 2, 1, 2, 3, 456789,
                            tzinfo=timezone(timedelta(hours=5, minutes=30))),
        body="hello",
        channel_id="222",
        wakes=route == "wake",
    )
    envelope = NotificationEnvelope.from_notification(notification, guild_id="111")
    queue = RedisNotificationQueue(redis_client)
    if route == "shadow":
        await queue.publish_shadow(envelope)
        entries = await redis_client.xrange(SHADOW_STREAM_KEY)
        raw = entries[0][1][b"payload"]
    else:
        await queue.publish(envelope)
        if route == "wake":
            entries = await redis_client.xrange(wake_stream_key("111"))
            raw = entries[0][1][b"payload"]
        else:
            raw = await redis_client.lindex(pending_key("111"), 0)
    assert json.loads(raw)["created_at"] == "2026-09-01T19:32:03.456789Z"
    restored = NotificationEnvelope.model_validate_json(raw).to_notification()
    assert restored.created_at == notification.created_at


@pytest.mark.asyncio
async def test_non_waking_notification_only_enters_pending_list(redis_client):
    queue = RedisNotificationQueue(redis_client)

    stream_id = await queue.publish(_envelope())

    assert stream_id is None
    assert await redis_client.llen(pending_key("111")) == 1
    assert await redis_client.exists(wake_stream_key("111")) == 0
    assert await redis_client.smembers(READY_GUILDS_KEY) == set()
    assert await redis_client.exists(READY_STREAM_KEY) == 0


@pytest.mark.asyncio
async def test_waking_notification_signals_only_its_guild(redis_client):
    queue = RedisNotificationQueue(redis_client)

    stream_id = await queue.publish(
        _envelope(guild_id="111", wakes=True, kind="mention")
    )

    assert stream_id is not None
    entries = await redis_client.xrange(wake_stream_key("111"))
    assert len(entries) == 1
    payload = NotificationEnvelope.model_validate_json(entries[0][1][b"payload"])
    assert payload.guild_id == "111"
    assert await redis_client.exists(wake_stream_key("999")) == 0
    assert await redis_client.smembers(READY_GUILDS_KEY) == {b"111"}
    ready_entries = await redis_client.xrange(READY_STREAM_KEY)
    assert ready_entries[0][1][b"guild_id"] == b"111"


@pytest.mark.asyncio
async def test_pending_limit_keeps_newest_and_records_dropped(redis_client):
    queue = RedisNotificationQueue(redis_client, pending_limit=2)
    for index in range(4):
        await queue.publish(_envelope(body=f"notification-{index}"))

    claimed = await queue.claim_pending("111", "wake-1")

    assert [item.body for item in claimed.notifications] == [
        "notification-2",
        "notification-3",
    ]
    assert claimed.dropped == 2


@pytest.mark.asyncio
async def test_claim_is_crash_safe_and_new_pending_waits_for_next_wake(redis_client):
    queue = RedisNotificationQueue(redis_client)
    await queue.publish(_envelope(body="before-wake"))

    first_claim = await queue.claim_pending("111", "wake-1")
    await queue.publish(_envelope(body="after-wake"))
    retried_claim = await queue.claim_pending("111", "wake-1")
    next_claim = await queue.claim_pending("111", "wake-2")

    assert [item.body for item in first_claim.notifications] == ["before-wake"]
    assert retried_claim == first_claim
    assert [item.body for item in next_claim.notifications] == ["after-wake"]

    await queue.acknowledge_pending("111", "wake-1")
    assert await queue.claim_pending("111", "wake-1") == type(first_claim)(
        notifications=(), dropped=0
    )


def test_guild_ids_are_present_in_every_queue_key():
    assert "{guild:111}" in wake_stream_key("111")
    assert "{guild:222}" in wake_stream_key("222")
    assert wake_stream_key("111") != wake_stream_key("222")


def test_contract_rejects_cross_guild_or_unknown_data():
    payload = _envelope().model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValueError):
        NotificationEnvelope.model_validate(payload)

    with pytest.raises(ValueError):
        _envelope(guild_id="not-a-snowflake")

    assert isinstance(_envelope().notification_id, UUID)


@pytest.mark.asyncio
async def test_external_runtime_publishes_without_creating_embedded_agent(
    redis_client,
):
    runtime = proactive.ProactiveRuntime(
        SimpleNamespace(d={"chat_memory_redis": redis_client}),
        execution_mode=proactive.EXTERNAL_EXECUTION_MODE,
    )

    await runtime.enqueue_notification(
        "111", _envelope(wakes=True, kind="mention").to_notification()
    )

    assert runtime.guild_states == {}
    assert await redis_client.xlen(wake_stream_key("111")) == 1
    assert await redis_client.get(ownership_key("111")) == b"external"


@pytest.mark.asyncio
async def test_startup_ownership_sync_fences_worker_on_rollback(redis_client):
    cache = SimpleNamespace(get_guilds_view=lambda: {111: object()})
    external = proactive.ProactiveRuntime(
        SimpleNamespace(d={"chat_memory_redis": redis_client}, cache=cache),
        execution_mode=proactive.EXTERNAL_EXECUTION_MODE,
    )
    await external.sync_execution_ownership()
    assert await redis_client.get(ownership_key("111")) == b"external"

    embedded = proactive.ProactiveRuntime(
        SimpleNamespace(d={"chat_memory_redis": redis_client}, cache=cache),
        execution_mode=proactive.EMBEDDED_EXECUTION_MODE,
    )
    await embedded.sync_execution_ownership()
    assert await redis_client.get(ownership_key("111")) == b"embedded"


@pytest.mark.asyncio
async def test_shadow_runtime_keeps_embedded_owner_and_uses_non_consumed_stream(
    redis_client,
):
    runtime = proactive.ProactiveRuntime(
        SimpleNamespace(d={"chat_memory_redis": redis_client}),
        start_consumers=False,
        execution_mode=proactive.SHADOW_EXECUTION_MODE,
    )
    notification = _envelope(wakes=True, kind="mention").to_notification()

    await runtime.enqueue_notification("111", notification)

    assert runtime.guild_state_for(111).queue.items == [notification]
    assert await redis_client.xlen(SHADOW_STREAM_KEY) == 1
    assert await redis_client.xlen(wake_stream_key("111")) == 0
    assert await redis_client.xlen(READY_STREAM_KEY) == 0


def test_runtime_rejects_unknown_execution_mode():
    with pytest.raises(ValueError, match="PROACTIVE_AGENT_EXECUTION_MODE"):
        proactive.ProactiveRuntime(
            SimpleNamespace(d={}), execution_mode="both-consumers"
        )
