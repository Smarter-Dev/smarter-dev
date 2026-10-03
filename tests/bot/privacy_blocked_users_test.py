"""The blocked-users cache, its refresh, and the bot-API client calls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from smarter_dev.bot.privacy.blocked_users import ENFORCING_KEY
from smarter_dev.bot.privacy.blocked_users import BlockedUsersCache
from smarter_dev.bot.privacy.blocked_users import BlockedUsersSnapshot
from smarter_dev.bot.privacy.blocked_users import redact_blocked_mentions
from smarter_dev.bot.privacy.blocked_users import refresh_loop
from smarter_dev.bot.privacy.blocked_users import refresh_once
from smarter_dev.bot.privacy.blocked_users import report_enforcing
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.privacy_service import MalformedBlockedUsersResponse
from smarter_dev.bot.services.privacy_service import PrivacyApiService
from smarter_dev.bot.services.privacy_service import parse_blocked_users
from smarter_dev.shared.privacy_purge import PurgeAck

KAI = "111111111111111111"
NIA = "222222222222222222"


def test_cold_cache_blocks_everyone_until_a_list_loads():
    cache = BlockedUsersCache()
    assert not cache.loaded
    assert cache.is_blocked(NIA)

    cache.load(3, [KAI])

    assert cache.loaded and cache.revision == 3
    assert cache.is_blocked(int(KAI))
    assert not cache.is_blocked(NIA)


async def test_standby_cannot_raise_the_aggregate_above_a_stale_acting_process():
    redis = fakeredis.aioredis.FakeRedis()
    # The acting process loaded revision 4 and has not refreshed since.
    await report_enforcing(redis, 4, process_id="acting-1")
    # A standby (or a new pod) fetches the newer revision 5.
    await report_enforcing(redis, 5, process_id="standby-2")

    assert await redis.get(ENFORCING_KEY) == b"4"
    assert await redis.get(f"{ENFORCING_KEY}:standby-2") == b"5"
    assert 0 < await redis.ttl(f"{ENFORCING_KEY}:acting-1") <= 180

    # Once the acting process's own key expires (it stopped fetching), the
    # aggregate follows the processes still reporting.
    await redis.delete(f"{ENFORCING_KEY}:acting-1")
    await report_enforcing(redis, 5, process_id="standby-2")
    assert await redis.get(ENFORCING_KEY) == b"5"


async def test_stale_list_gives_the_model_no_input():
    now = [1000.0]
    cache = BlockedUsersCache(clock=lambda: now[0])
    cache.load(3, [KAI])
    assert cache.loaded and not cache.is_blocked(NIA)

    now[0] += 179
    assert cache.loaded
    now[0] += 2  # last successful fetch is now older than the 180 s report
    assert not cache.loaded
    assert cache.is_blocked(NIA)  # every author: no Discord input at all

    await refresh_once(
        cache, AsyncMock(return_value=BlockedUsersSnapshot(3, frozenset({KAI}))), None
    )
    assert cache.loaded and not cache.is_blocked(NIA)


async def test_stale_process_routes_nobody_to_the_chat_or_proactive_model(
    monkeypatch,
):
    from smarter_dev.bot.plugins import mention
    from smarter_dev.bot.plugins import proactive
    from smarter_dev.bot.privacy import blocked_users as module

    now = [0.0]
    stale = BlockedUsersCache(clock=lambda: now[0])
    stale.load(3, [])
    now[0] = 500.0
    monkeypatch.setattr(mention, "get_blocked_users", lambda: stale)
    monkeypatch.setattr(proactive, "get_blocked_users", lambda: stale)
    assert module.get_blocked_users is not None
    registry = AsyncMock()
    monkeypatch.setattr(mention, "get_chat_engine_registry", registry)
    message = SimpleNamespace(
        id=1, author=SimpleNamespace(id=int(NIA), is_bot=False), content="hi"
    )

    await mention.on_message_create(
        SimpleNamespace(message=message, guild_id=2, channel_id=1, content="hi")
    )
    converted = proactive.channel_message_from_hikari(
        SimpleNamespace(
            id=1, created_at=None, author=message.author, content="hi",
            referenced_message=None,
        )
    )

    registry.assert_not_called()
    assert converted.blocked and converted.content == ""


async def test_refresh_sets_enforcing_key_with_ttl():
    cache = BlockedUsersCache()
    redis = fakeredis.aioredis.FakeRedis()

    ok = await refresh_once(
        cache,
        AsyncMock(return_value=BlockedUsersSnapshot(7, frozenset({KAI}))),
        redis,
    )

    assert ok
    assert await redis.get(ENFORCING_KEY) == b"7"
    assert ENFORCING_KEY == "privacy:v1:enforcing:bot"
    assert 0 < await redis.ttl(ENFORCING_KEY) <= 180


async def test_failed_refresh_keeps_last_list_and_sets_no_key(caplog):
    cache = BlockedUsersCache()
    cache.load(4, [KAI])
    redis = fakeredis.aioredis.FakeRedis()

    ok = await refresh_once(cache, AsyncMock(side_effect=APIError("down")), redis)

    assert not ok
    assert cache.revision == 4 and cache.is_blocked(KAI)
    assert await redis.get(ENFORCING_KEY) is None
    assert KAI not in caplog.text


async def test_failed_first_refresh_stays_cold():
    cache = BlockedUsersCache()

    await refresh_once(cache, AsyncMock(side_effect=ConnectionError()), None)

    assert not cache.loaded and cache.is_blocked(NIA)


async def test_refresh_loop_polls_on_its_interval():
    cache = BlockedUsersCache()
    fetch = AsyncMock(return_value=BlockedUsersSnapshot(1, frozenset()))
    task = asyncio.create_task(refresh_loop(cache, fetch, None, interval=0.01))
    await cache.wait_loaded()
    await asyncio.sleep(0.05)
    task.cancel()

    assert fetch.await_count >= 2


def test_snapshot_repr_hides_ids():
    assert KAI not in repr(BlockedUsersSnapshot(1, frozenset({KAI})))


def test_parse_accepts_contract_and_rejects_without_echo():
    snapshot = parse_blocked_users({"revision": 2, "user_ids": [KAI]})
    assert snapshot.user_ids == frozenset({KAI})

    for bad in (
        {"revision": -1, "user_ids": []},
        {"revision": 1, "user_ids": ["kai"]},
        {"user_ids": [KAI]},
        [KAI],
    ):
        with pytest.raises(MalformedBlockedUsersResponse) as error:
            parse_blocked_users(bad)
        assert KAI not in str(error.value)


async def test_service_calls_the_two_endpoints():
    client = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(json=lambda: {"revision": 1, "user_ids": []})
        ),
        post=AsyncMock(),
    )
    service = PrivacyApiService(client)
    ack = PurgeAck(component="bot", guild_id="3" * 18, outcome="unchanged")

    assert (await service.fetch_blocked_users()).revision == 1
    assert await service.post_purge_ack("run-1", ack) is True

    client.get.assert_awaited_once_with("/privacy/blocked-users")
    path = client.post.await_args.args[0]
    assert path == "/privacy/purges/run-1/acks"
    assert client.post.await_args.kwargs["json_data"]["component"] == "bot"

    client.post.side_effect = APIError("gone", status_code=404)
    assert await service.post_purge_ack("run-1", ack) is False
    client.post.side_effect = APIError("boom", status_code=500)
    with pytest.raises(APIError):
        await service.post_purge_ack("run-1", ack)


def test_mentions_of_blocked_users_are_redacted():
    cache = BlockedUsersCache()
    cache.load(1, [KAI])

    text = f"<@{KAI}> and <@!{KAI}> vs <@{NIA}> <@&{KAI}> <#{KAI}> <@ broken"

    assert redact_blocked_mentions(text, cache) == (
        f"@[blocked user] and @[blocked user] vs <@{NIA}> <@&{KAI}> <#{KAI}> <@ broken"
    )
