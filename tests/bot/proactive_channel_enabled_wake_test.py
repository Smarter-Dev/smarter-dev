"""The bot turns the web's channel-enabled request into the /proactive on wake.

The web (the /configure form, the admin overview) switches a channel on in
another process and asks for the wake on a Redis stream; the bot's control
loop reads it and enqueues the same notification /proactive on does.
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from smarter_dev.bot.plugins import proactive
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


@pytest.mark.parametrize(
    "stream", [CHANNEL_ENABLED_STREAM, proactive.CONTROL_STREAM_KEY]
)
async def test_an_unreadable_command_is_dropped_not_redelivered(fast_loop, stream):
    """A payload no retry can parse is acked and deleted, never left pending."""
    redis = _redis()
    run = _runtime(redis, _SettingsService(enabled=True))
    await redis.xadd(stream, {"payload": '{"kind": "from-a-newer-sender"}'})

    await _drain(run, redis, stream)

    pending = await redis.xpending(stream, proactive.CONTROL_GROUP)
    assert pending["pending"] == 0
    run.enqueue_notification.assert_not_awaited()


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
    from smarter_dev.bot.proactive.contracts import ControlCommand

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
