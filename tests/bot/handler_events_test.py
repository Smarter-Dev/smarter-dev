"""Tests for the event-dispatch cheap-guard cache."""

from __future__ import annotations

from smarter_dev.bot.plugins.handler_events import (
    ActiveChannelsCache,
    _snowflake_created_at,
)


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeAPI:
    def __init__(self, channels, guild_triggers=None):
        self.channels = channels
        self.guild_triggers = guild_triggers or []
        self.get_calls = 0

    async def get(self, path):
        self.get_calls += 1
        return _Resp({"channels": self.channels, "guild_triggers": self.guild_triggers})


async def test_cache_reports_channel_membership():
    api = _FakeAPI([["C1", "message"], ["C2", "reaction"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    assert await cache.has(api, "C1", "G1", "message") is True
    assert await cache.has(api, "C1", "G1", "reaction") is False
    assert await cache.has(api, "C9", "G1", "message") is False


async def test_cache_reports_guild_wide_admin_trigger():
    # An all-channel admin handler => every channel in that guild dispatches.
    api = _FakeAPI(channels=[], guild_triggers=[["G1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    assert await cache.has(api, "ANY", "G1", "message") is True
    assert await cache.has(api, "ANY", "G2", "message") is False  # different guild
    assert await cache.has(api, "ANY", "G1", "reaction") is False


async def test_cache_avoids_refetch_within_ttl():
    api = _FakeAPI([["C1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    await cache.has(api, "C1", "G1", "message")
    await cache.has(api, "C1", "G1", "message")
    assert api.get_calls == 1


async def test_invalidate_forces_refetch():
    api = _FakeAPI([["C1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    await cache.has(api, "C1", "G1", "message")
    cache.invalidate()
    await cache.has(api, "C1", "G1", "message")
    assert api.get_calls == 2


def test_snowflake_created_at():
    # Discord snowflake -> ISO creation time (2015+).
    iso = _snowflake_created_at(733364234141827073)
    assert iso.startswith("20")  # a real UTC timestamp
