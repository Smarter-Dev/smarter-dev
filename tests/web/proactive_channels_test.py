"""Switching a channel on from the web requests the bot's orientation wake.

The database decides the off-to-on change (``UPDATE ... WHERE enabled IS NOT
TRUE`` / ``INSERT ... ON CONFLICT DO NOTHING``), so only the write that
turned the channel on wakes it; the wake goes out only after the commit, so
the agent never wakes into a channel the bot still reads as off. These run
the statements against the suite's SQLite database.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from sqlalchemy import select

from smarter_dev.shared.proactive_channel_wake import CHANNEL_ENABLED_STREAM
from smarter_dev.shared.proactive_channel_wake import ChannelEnabledRequest
from smarter_dev.web import proactive_channels
from smarter_dev.web.crud import set_proactive_channel_enabled_flag
from smarter_dev.web.models import ProactiveChannelSettings

GUILD = "644299523686006834"
CHANNEL = "644299524151443487"


async def _seed(session, *, enabled: bool, watch_addendum: str = "") -> None:
    session.add(
        ProactiveChannelSettings(
            guild_id=GUILD,
            channel_id=CHANNEL,
            enabled=enabled,
            watch_addendum=watch_addendum,
        )
    )
    await session.commit()


async def _row(session) -> ProactiveChannelSettings:
    session.expire_all()
    result = await session.execute(
        select(ProactiveChannelSettings).where(
            ProactiveChannelSettings.channel_id == CHANNEL
        )
    )
    return result.scalar_one()


async def _requests(redis) -> list[ChannelEnabledRequest]:
    entries = await redis.xrange(CHANNEL_ENABLED_STREAM)
    return [
        ChannelEnabledRequest.model_validate_json(fields[b"payload"])
        for _id, fields in entries
    ]


async def _toggle(session, redis, enabled: bool) -> bool:
    return await proactive_channels.set_proactive_channel_enabled(
        session, redis, guild_id=GUILD, channel_id=CHANNEL, enabled=enabled
    )


# --- the statement ------------------------------------------------------------


async def test_enabling_a_channel_with_no_row_inserts_it_on(db_session):
    assert await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True
    )
    row = await _row(db_session)
    assert (row.guild_id, row.enabled, row.watch_addendum) == (GUILD, True, "")


async def test_enabling_an_off_channel_turns_it_on(db_session):
    await _seed(db_session, enabled=False)
    assert await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True
    )
    assert (await _row(db_session)).enabled is True


async def test_enabling_twice_turns_it_on_once(db_session):
    """A double-click: the second enable finds the row already on."""
    first = await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True
    )
    second = await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True
    )
    assert (first, second) == (True, False)


@pytest.mark.parametrize("seeded", [None, False, True])
async def test_disabling_never_reports_a_turn_on(db_session, seeded):
    if seeded is not None:
        await _seed(db_session, enabled=seeded)
    assert not await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=False
    )
    assert (await _row(db_session)).enabled is False


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_switch_never_touches_the_agents_watch_instructions(
    db_session, enabled
):
    await _seed(db_session, enabled=not enabled, watch_addendum='[{"id": "w1"}]')
    await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=enabled
    )
    assert (await _row(db_session)).watch_addendum == '[{"id": "w1"}]'


# --- the helper -----------------------------------------------------------------


@pytest.mark.parametrize("seeded", [None, False])
async def test_switching_a_channel_on_requests_the_wake(db_session, seeded):
    if seeded is not None:
        await _seed(db_session, enabled=seeded)
    redis = fakeredis.aioredis.FakeRedis()

    assert await _toggle(db_session, redis, True) is True

    [request] = await _requests(redis)
    assert (request.guild_id, request.channel_id) == (GUILD, CHANNEL)


async def test_a_channel_already_on_is_not_woken_again(db_session):
    await _seed(db_session, enabled=True)
    redis = fakeredis.aioredis.FakeRedis()

    assert await _toggle(db_session, redis, True) is False

    assert await _requests(redis) == []


async def test_a_double_click_wakes_once(db_session):
    redis = fakeredis.aioredis.FakeRedis()
    await _toggle(db_session, redis, True)
    await _toggle(db_session, redis, True)
    assert len(await _requests(redis)) == 1


@pytest.mark.parametrize("seeded", [None, False, True])
async def test_switching_a_channel_off_never_wakes(db_session, seeded):
    if seeded is not None:
        await _seed(db_session, enabled=seeded)
    redis = fakeredis.aioredis.FakeRedis()

    assert await _toggle(db_session, redis, False) is False

    assert await _requests(redis) == []


async def test_the_wake_is_requested_after_the_commit(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        proactive_channels,
        "set_proactive_channel_enabled_flag",
        AsyncMock(return_value=True),
    )
    session = AsyncMock()
    session.commit.side_effect = lambda: order.append("commit")
    redis = AsyncMock()
    redis.xadd.side_effect = lambda *a, **k: order.append("wake")

    await _toggle(session, redis, True)

    assert order == ["commit", "wake"]


async def test_a_failed_wake_request_still_saves_the_switch(db_session):
    redis = AsyncMock()
    redis.xadd.side_effect = ConnectionError("redis down")

    assert await _toggle(db_session, redis, True) is False

    assert (await _row(db_session)).enabled is True


async def test_a_stalled_redis_cannot_hold_the_page(db_session, monkeypatch):
    """Redis accepts the connection but never answers the XADD."""
    monkeypatch.setattr(proactive_channels, "WAKE_REQUEST_TIMEOUT_SECONDS", 0.2)
    never = asyncio.Event()

    async def stalled_xadd(*_args, **_kwargs):
        await never.wait()

    redis = AsyncMock()
    redis.xadd.side_effect = stalled_xadd

    started = time.monotonic()
    turned_on = await asyncio.wait_for(_toggle(db_session, redis, True), timeout=5)
    elapsed = time.monotonic() - started

    assert turned_on is False
    assert elapsed < 1.0
    assert (await _row(db_session)).enabled is True, "committed before the wake"


# --- a whole forum's row (#248) ---------------------------------------------------


@pytest.mark.parametrize("seeded", [None, False, True])
async def test_switching_a_forum_on_marks_its_row_and_wakes_nobody(
    db_session, seeded
):
    if seeded is not None:
        await _seed(db_session, enabled=seeded)
    redis = fakeredis.aioredis.FakeRedis()

    assert (
        await proactive_channels.set_proactive_channel_enabled(
            db_session,
            redis,
            guild_id=GUILD,
            channel_id=CHANNEL,
            enabled=True,
            is_forum=True,
        )
        is False
    )

    row = await _row(db_session)
    assert (row.enabled, row.is_forum) == (True, True)
    assert await _requests(redis) == []


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_switch_without_a_mark_keeps_the_rows_mark(db_session, enabled):
    await _seed(db_session, enabled=not enabled)
    await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True, is_forum=True
    )
    await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=enabled
    )

    assert (await _row(db_session)).is_forum is True


async def test_the_list_leaves_out_forums_for_a_consumer_that_did_not_ask(
    db_session,
):
    from smarter_dev.web.crud import list_enabled_proactive_channel_settings

    other = "644299524151443488"
    await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True, is_forum=True
    )
    await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=other, enabled=True
    )
    await db_session.commit()

    everything = await list_enabled_proactive_channel_settings(db_session, GUILD)
    plain = await list_enabled_proactive_channel_settings(
        db_session, GUILD, include_forums=False
    )

    assert [row.channel_id for row in everything] == [CHANNEL, other]
    assert [row.channel_id for row in plain] == [other]


async def test_the_addendum_upsert_keeps_the_forum_mark(db_session):
    from smarter_dev.web.crud import upsert_proactive_channel_settings

    await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True, is_forum=True
    )
    # The worker's PUT of the forum's watch instructions sends no mark.
    await upsert_proactive_channel_settings(
        db_session,
        guild_id=GUILD,
        channel_id=CHANNEL,
        enabled=True,
        watch_addendum='[{"id": "w1"}]',
    )

    assert (await _row(db_session)).is_forum is True


async def test_the_addendum_upsert_keeps_new_posts_to_agent(db_session):
    """#297: the worker's watch-instruction PUT never sends the setting, so
    it must not switch a forum's new posts back to the watcher."""
    from smarter_dev.web.crud import upsert_proactive_channel_settings

    await upsert_proactive_channel_settings(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True,
        watch_addendum="", is_forum=True, new_posts_to_agent=True,
    )
    await upsert_proactive_channel_settings(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True,
        watch_addendum='[{"id": "w1"}]',
    )
    assert (await _row(db_session)).new_posts_to_agent is True

    await upsert_proactive_channel_settings(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True,
        watch_addendum="", new_posts_to_agent=False,
    )
    assert (await _row(db_session)).new_posts_to_agent is False


async def test_new_posts_to_agent_is_off_for_a_new_row(db_session):
    await set_proactive_channel_enabled_flag(
        db_session, guild_id=GUILD, channel_id=CHANNEL, enabled=True, is_forum=True
    )

    assert (await _row(db_session)).new_posts_to_agent is False
