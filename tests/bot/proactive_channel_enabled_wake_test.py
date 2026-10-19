"""The bot turns the web's channel-enabled request into the /proactive on wake.

The web (the /configure form, the admin overview) switches a channel on in
another process and asks for the wake on a Redis stream; the bot's control
loop reads it and enqueues the same notification /proactive on does.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.proactive.contracts import ControlCommand
from smarter_dev.shared.proactive_channel_wake import CHANNEL_ENABLED_MAX_AGE
from smarter_dev.shared.proactive_channel_wake import CHANNEL_ENABLED_STREAM
from smarter_dev.shared.proactive_channel_wake import request_channel_enabled_wake

GUILD = "2"
CHANNEL = "1"


class _SettingsService:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.invalidated: list[tuple[str, str]] = []

    def invalidate(self, guild_id, channel_id):
        self.invalidated.append((guild_id, channel_id))

    async def get_settings(self, guild_id, channel_id):
        return SimpleNamespace(enabled=self.enabled)


def _redis() -> fakeredis.aioredis.FakeRedis:
    """fakeredis ignores XREADGROUP's block; Redis waits, so the loop yields."""
    redis = fakeredis.aioredis.FakeRedis()
    read = redis.xreadgroup

    async def xreadgroup(*args, **kwargs):
        result = await read(*args, **kwargs)
        if not result and kwargs.get("block"):
            await asyncio.sleep(kwargs["block"] / 1000)
        return result

    redis.xreadgroup = xreadgroup
    return redis


def _runtime(redis, service) -> proactive.ProactiveRuntime:
    bot = SimpleNamespace(
        d={"chat_memory_redis": redis, "proactive_settings_service": service},
        cache=SimpleNamespace(
            get_guild_channel=lambda _cid: SimpleNamespace(name="general")
        ),
    )
    run = proactive.ProactiveRuntime(bot, start_consumers=False)
    run.enqueue_notification = AsyncMock()
    return run


@pytest.fixture
def fast_loop(monkeypatch):
    monkeypatch.setattr(proactive, "CONTROL_BLOCK_MS", 20)


async def _run_loop_until(run, condition, timeout: float = 2.0) -> None:
    loop = asyncio.create_task(proactive._control_loop(run))
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            assert asyncio.get_running_loop().time() < deadline, "loop never applied it"
            await asyncio.sleep(0.01)
    finally:
        loop.cancel()


async def _pending(redis, stream: str) -> int:
    return (await redis.xpending(stream, proactive.CONTROL_GROUP))["pending"]


async def _drain(run, redis, stream: str) -> None:
    loop = asyncio.create_task(proactive._control_loop(run))
    try:
        for _ in range(200):
            if await redis.xlen(stream) == 0:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"{stream} was never acked and deleted")
    finally:
        loop.cancel()


async def test_a_web_request_wakes_the_agent_as_proactive_on_does(fast_loop):
    redis = _redis()
    service = _SettingsService(enabled=True)
    run = _runtime(redis, service)

    await request_channel_enabled_wake(
        redis, guild_id=GUILD, channel_id=CHANNEL, now=datetime.now(UTC)
    )
    await _run_loop_until(run, lambda: run.enqueue_notification.await_count)

    guild_id, notification = run.enqueue_notification.await_args.args
    assert guild_id == GUILD
    assert notification.kind == "channel_enabled"
    assert notification.wakes is True
    assert (notification.channel_id, notification.channel_name) == (CHANNEL, "general")
    assert service.invalidated == [(GUILD, CHANNEL)], "fresh read, not the cached off"
    assert await redis.xlen(CHANNEL_ENABLED_STREAM) == 0, "acked and deleted"


async def test_the_web_wake_matches_the_slash_command_wake(fast_loop, monkeypatch):
    """Both paths build the notification in one place."""
    redis = _redis()
    run = _runtime(redis, _SettingsService(enabled=True))
    await request_channel_enabled_wake(
        redis, guild_id=GUILD, channel_id=CHANNEL, now=datetime.now(UTC)
    )
    await _run_loop_until(run, lambda: run.enqueue_notification.await_count)
    from_web = run.enqueue_notification.await_args.args[1]

    service = SimpleNamespace(
        get_settings=AsyncMock(return_value=SimpleNamespace(enabled=False)),
        set_enabled=AsyncMock(),
    )
    run.settings_service = lambda: service
    monkeypatch.setattr(proactive, "runtime", run)
    monkeypatch.setattr(
        proactive, "deny_without_moderator_permissions", AsyncMock(return_value=False)
    )
    ctx = SimpleNamespace(guild_id=int(GUILD), channel_id=int(CHANNEL), respond=AsyncMock())
    await proactive._set_enabled(ctx, True)
    from_slash = run.enqueue_notification.await_args.args[1]

    assert (from_web.kind, from_web.body, from_web.wakes, from_web.channel_id) == (
        from_slash.kind,
        from_slash.body,
        from_slash.wakes,
        from_slash.channel_id,
    )


async def test_a_channel_switched_off_again_before_the_wake_is_not_woken(fast_loop):
    redis = _redis()
    run = _runtime(redis, _SettingsService(enabled=False))

    await request_channel_enabled_wake(
        redis, guild_id=GUILD, channel_id=CHANNEL, now=datetime.now(UTC)
    )
    await _drain(run, redis, CHANNEL_ENABLED_STREAM)

    run.enqueue_notification.assert_not_awaited()


async def test_an_unreadable_wake_request_is_dropped_not_redelivered(fast_loop):
    """This repo writes both ends of the wake stream: no bot will read it."""
    redis = _redis()
    run = _runtime(redis, _SettingsService(enabled=True))
    await redis.xadd(CHANNEL_ENABLED_STREAM, {"payload": '{"kind": "garbled"}'})

    await _drain(run, redis, CHANNEL_ENABLED_STREAM)

    pending = await redis.xpending(CHANNEL_ENABLED_STREAM, proactive.CONTROL_GROUP)
    assert pending["pending"] == 0
    run.enqueue_notification.assert_not_awaited()


async def test_a_worker_command_this_bot_cannot_read_stays_pending(
    fast_loop, monkeypatch
):
    """A newer worker's shape waits for a bot that can read it."""
    monkeypatch.setattr(proactive, "_apply_control_command", AsyncMock())
    redis = _redis()
    run = _runtime(redis, _SettingsService(enabled=True))
    command = ControlCommand(
        guild_id=GUILD, channel_id=CHANNEL, mode="active", minutes=5,
        created_at=datetime.now(UTC),
    ).model_dump(mode="json")
    command["schema_version"] = 2
    command["new_field"] = "from a newer worker"
    await redis.xadd(proactive.CONTROL_STREAM_KEY, {"payload": json.dumps(command)})
    await proactive._create_control_groups(redis)

    loop = asyncio.create_task(proactive._control_loop(run))
    for _ in range(200):
        pending = await _pending(redis, proactive.CONTROL_STREAM_KEY)
        if pending:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    loop.cancel()

    assert await _pending(redis, proactive.CONTROL_STREAM_KEY) == 1, "read, not acked"
    assert await redis.xlen(proactive.CONTROL_STREAM_KEY) == 1, "not deleted"
    proactive._apply_control_command.assert_not_awaited()


async def test_a_stale_wake_request_is_dropped(fast_loop):
    """A backlog replayed after a long outage no longer means 'just enabled'."""
    redis = _redis()
    run = _runtime(redis, _SettingsService(enabled=True))
    await request_channel_enabled_wake(
        redis,
        guild_id=GUILD,
        channel_id=CHANNEL,
        now=datetime.now(UTC) - CHANNEL_ENABLED_MAX_AGE - timedelta(seconds=1),
    )

    await _drain(run, redis, CHANNEL_ENABLED_STREAM)

    run.enqueue_notification.assert_not_awaited()


async def test_a_wake_request_inside_the_max_age_still_wakes(fast_loop):
    redis = _redis()
    run = _runtime(redis, _SettingsService(enabled=True))
    await request_channel_enabled_wake(
        redis,
        guild_id=GUILD,
        channel_id=CHANNEL,
        now=datetime.now(UTC) - CHANNEL_ENABLED_MAX_AGE + timedelta(minutes=1),
    )

    await _run_loop_until(run, lambda: run.enqueue_notification.await_count)


async def test_a_failed_wake_stays_pending_for_a_retry(fast_loop):
    """A transient failure (the settings API down) is retried, not dropped."""
    redis = _redis()
    service = _SettingsService(enabled=True)
    service.get_settings = AsyncMock(side_effect=RuntimeError("api down"))
    run = _runtime(redis, service)
    await request_channel_enabled_wake(
        redis, guild_id=GUILD, channel_id=CHANNEL, now=datetime.now(UTC)
    )

    await _run_loop_until(run, lambda: service.get_settings.await_count)
    await asyncio.sleep(0.05)

    pending = await redis.xpending(CHANNEL_ENABLED_STREAM, proactive.CONTROL_GROUP)
    assert pending["pending"] == 1
    assert await redis.xlen(CHANNEL_ENABLED_STREAM) == 1


async def test_worker_control_commands_still_apply_beside_web_requests(
    fast_loop, monkeypatch
):
    applied: list[str] = []

    async def apply(_run, command) -> None:
        applied.append(command.mode)

    monkeypatch.setattr(proactive, "_apply_control_command", apply)
    redis = _redis()
    run = _runtime(redis, _SettingsService(enabled=True))
    command = ControlCommand(
        guild_id=GUILD, channel_id=CHANNEL, mode="active", minutes=5,
        created_at=datetime.now(UTC),
    )
    await redis.xadd(proactive.CONTROL_STREAM_KEY, {"payload": command.model_dump_json()})
    await request_channel_enabled_wake(
        redis, guild_id=GUILD, channel_id=CHANNEL, now=datetime.now(UTC)
    )

    await _run_loop_until(
        run, lambda: applied and run.enqueue_notification.await_count
    )

    assert applied == ["active"]
    assert run.enqueue_notification.await_args.args[1].kind == "channel_enabled"


# --- the loop survives Redis errors ----------------------------------------------


@pytest.fixture
def fast_retry(monkeypatch):
    monkeypatch.setattr(proactive, "CONTROL_RETRY_MIN_SECONDS", 0.01)
    monkeypatch.setattr(proactive, "CONTROL_RETRY_MAX_SECONDS", 0.05)


async def test_a_failed_read_backs_off_and_the_loop_carries_on(fast_loop, fast_retry):
    redis = _redis()
    read = redis.xreadgroup
    failures = []

    async def flaky_xreadgroup(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise RedisConnectionError("connection reset")
        return await read(*args, **kwargs)

    redis.xreadgroup = flaky_xreadgroup
    run = _runtime(redis, _SettingsService(enabled=True))
    await request_channel_enabled_wake(
        redis, guild_id=GUILD, channel_id=CHANNEL, now=datetime.now(UTC)
    )

    await _run_loop_until(run, lambda: run.enqueue_notification.await_count)

    assert failures == [1], "the failing read happened, and the next one applied it"


async def test_a_deleted_consumer_group_is_recreated(fast_loop, fast_retry):
    redis = _redis()
    run = _runtime(redis, _SettingsService(enabled=True))
    loop = asyncio.create_task(proactive._control_loop(run))
    try:
        for _ in range(200):
            groups = await redis.xinfo_groups(CHANNEL_ENABLED_STREAM)
            if groups:
                break
            await asyncio.sleep(0.01)
        await redis.xgroup_destroy(CHANNEL_ENABLED_STREAM, proactive.CONTROL_GROUP)
        await request_channel_enabled_wake(
            redis, guild_id=GUILD, channel_id=CHANNEL, now=datetime.now(UTC)
        )
        for _ in range(300):
            if run.enqueue_notification.await_count:
                break
            await asyncio.sleep(0.01)
    finally:
        loop.cancel()

    run.enqueue_notification.assert_awaited_once()


async def test_cancelling_the_loop_still_stops_it(fast_loop, fast_retry):
    redis = _redis()
    run = _runtime(redis, _SettingsService(enabled=True))
    loop = asyncio.create_task(proactive._control_loop(run))
    await asyncio.sleep(0.05)

    loop.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(loop, timeout=1)


async def test_cancelling_during_a_backoff_stops_the_loop(fast_loop, monkeypatch):
    monkeypatch.setattr(proactive, "CONTROL_RETRY_MIN_SECONDS", 30)
    redis = _redis()
    redis.xreadgroup = AsyncMock(side_effect=RedisConnectionError("down"))
    run = _runtime(redis, _SettingsService(enabled=True))
    loop = asyncio.create_task(proactive._control_loop(run))
    for _ in range(100):
        if redis.xreadgroup.await_count:
            break
        await asyncio.sleep(0.01)

    loop.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(loop, timeout=1)
