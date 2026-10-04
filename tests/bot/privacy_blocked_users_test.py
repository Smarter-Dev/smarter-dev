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
from smarter_dev.bot.privacy.blocked_users import process_key
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


async def test_failing_fetches_keep_the_last_list_in_force_for_ten_minutes(
    monkeypatch, caplog
):
    """Zech's decision: the bot keeps going on the last list it loaded. Ten
    minutes of failed fetches later the models still get input, kai (on the
    old list) is still blocked, and the per-process key still reports the
    revision this process actually holds."""
    from smarter_dev.bot.agents import chat_context
    from smarter_dev.bot.agents.chat_input_format import build_agent_call
    from smarter_dev.bot.plugins import proactive
    from smarter_dev.bot.privacy import blocked_users as module
    from tests.bot.privacy_model_input_test import _channel_messages
    from tests.bot.privacy_model_input_test import _fake_bot
    from tests.bot.privacy_model_input_test import _Memory

    now = [1000.0]
    cache = BlockedUsersCache(clock=lambda: now[0])
    cache.load(3, [KAI])
    redis = fakeredis.aioredis.FakeRedis()
    failing = AsyncMock(side_effect=ConnectionError("web down"))
    monkeypatch.setattr(module, "_last_stale_log", {})
    for _minute in range(11):
        now[0] += 60
        assert not await refresh_once(cache, failing, redis)

    assert cache.loaded and cache.age_seconds() >= 600
    assert await redis.get(process_key()) == b"3"
    assert 0 < await redis.ttl(process_key()) <= 180
    assert await redis.get(ENFORCING_KEY) == b"3"

    monkeypatch.setattr(chat_context, "get_blocked_users", lambda: cache)
    monkeypatch.setattr(
        chat_context,
        "fetch_channel_info",
        AsyncMock(return_value={"channel_name": "general"}),
    )
    messages = _channel_messages()
    agent_input = await chat_context.build_followup_input(
        bot=_fake_bot(messages), channel_id=1, guild_id=2, queued=messages,
        memory=_Memory(),
    )
    prompt, history = build_agent_call(agent_input, [])
    rendered = prompt + str(history)
    assert "anyone benchmarked tokio?" in rendered  # nia still reaches it
    assert "[BLOCKED BY USER]" in rendered and KAI not in rendered

    monkeypatch.setattr(proactive, "get_blocked_users", lambda: cache)
    assert not proactive.channel_message_from_hikari(messages[0]).blocked
    assert proactive.channel_message_from_hikari(messages[1]).blocked


async def test_stale_state_is_logged_at_most_once_a_minute(monkeypatch, caplog):
    from smarter_dev.bot.privacy import blocked_users as module

    monkeypatch.setattr(module, "_last_stale_log", {})
    cache = BlockedUsersCache()
    cache.load(7, [KAI])
    failing = AsyncMock(side_effect=ConnectionError())
    for _ in range(5):
        await refresh_once(cache, failing, None)

    lines = [r for r in caplog.records if "refresh failed" in r.getMessage()]
    assert len(lines) == 1
    assert "revision=7" in lines[0].getMessage()
    assert KAI not in caplog.text


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


async def test_failed_refresh_keeps_last_list_and_still_reports_it(caplog):
    cache = BlockedUsersCache()
    cache.load(4, [KAI])
    redis = fakeredis.aioredis.FakeRedis()

    ok = await refresh_once(cache, AsyncMock(side_effect=APIError("down")), redis)

    assert not ok
    assert cache.revision == 4 and cache.is_blocked(KAI)
    assert await redis.get(process_key()) == b"4"
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
        # Any other blocked id as a digit run is replaced too.
        f"@[blocked user] and @[blocked user] vs <@{NIA}> <@&[blocked user]> "
        "<#[blocked user]> <@ broken"
    )
