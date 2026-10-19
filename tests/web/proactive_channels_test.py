"""Switching a channel on from the web requests the bot's orientation wake.

Only an off-to-on change requests it, and only after the commit, so the
agent never wakes into a channel the bot still reads as off.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import fakeredis.aioredis
import pytest

from smarter_dev.shared.proactive_channel_wake import CHANNEL_ENABLED_STREAM
from smarter_dev.shared.proactive_channel_wake import ChannelEnabledRequest
from smarter_dev.web import proactive_channels

GUILD = "644299523686006834"
CHANNEL = "644299524151443487"


def _row(enabled: bool, watch_addendum: str = "") -> SimpleNamespace:
    return SimpleNamespace(enabled=enabled, watch_addendum=watch_addendum)


@pytest.fixture
def crud():
    with (
        patch.object(
            proactive_channels,
            "get_proactive_channel_settings",
            new=AsyncMock(return_value=None),
        ) as get,
        patch.object(
            proactive_channels, "upsert_proactive_channel_settings", new=AsyncMock()
        ) as upsert,
    ):
        yield SimpleNamespace(get=get, upsert=upsert)


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


@pytest.mark.parametrize("existing", [None, _row(enabled=False)])
async def test_switching_a_channel_on_requests_the_wake(crud, existing):
    crud.get.return_value = existing
    redis = fakeredis.aioredis.FakeRedis()

    assert await _toggle(AsyncMock(), redis, True) is True

    [request] = await _requests(redis)
    assert (request.guild_id, request.channel_id) == (GUILD, CHANNEL)
    assert crud.upsert.await_args.kwargs["enabled"] is True


async def test_a_channel_already_on_is_not_woken_again(crud):
    crud.get.return_value = _row(enabled=True)
    redis = fakeredis.aioredis.FakeRedis()

    assert await _toggle(AsyncMock(), redis, True) is False

    assert await _requests(redis) == []
    crud.upsert.assert_awaited_once()


@pytest.mark.parametrize("existing", [None, _row(enabled=False), _row(enabled=True)])
async def test_switching_a_channel_off_never_wakes(crud, existing):
    crud.get.return_value = existing
    redis = fakeredis.aioredis.FakeRedis()

    assert await _toggle(AsyncMock(), redis, False) is False

    assert await _requests(redis) == []
    assert crud.upsert.await_args.kwargs["enabled"] is False


async def test_the_wake_is_requested_after_the_commit(crud):
    order: list[str] = []
    session = AsyncMock()
    session.commit.side_effect = lambda: order.append("commit")
    redis = AsyncMock()
    redis.xadd.side_effect = lambda *a, **k: order.append("wake")

    await _toggle(session, redis, True)

    assert order == ["commit", "wake"]


async def test_a_toggle_keeps_the_agents_watch_instructions(crud):
    crud.get.return_value = _row(enabled=False, watch_addendum='[{"id": "w1"}]')

    await _toggle(AsyncMock(), fakeredis.aioredis.FakeRedis(), True)

    assert crud.upsert.await_args.kwargs["watch_addendum"] == '[{"id": "w1"}]'


async def test_a_failed_wake_request_still_saves_the_switch(crud):
    session = AsyncMock()
    redis = AsyncMock()
    redis.xadd.side_effect = ConnectionError("redis down")

    assert await _toggle(session, redis, True) is False

    session.commit.assert_awaited_once()
