"""Tests for the event-dispatch cheap-guard cache."""

from __future__ import annotations

from smarter_dev.bot.plugins.handler_events import ActiveChannelsCache


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeAPI:
    def __init__(self, channels):
        self.channels = channels
        self.get_calls = 0

    async def get(self, path):
        self.get_calls += 1
        return _Resp({"channels": self.channels})


async def test_cache_reports_membership():
    api = _FakeAPI([["C1", "message"], ["C2", "reaction"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    assert await cache.has(api, "C1", "message") is True
    assert await cache.has(api, "C1", "reaction") is False
    assert await cache.has(api, "C9", "message") is False


async def test_cache_avoids_refetch_within_ttl():
    api = _FakeAPI([["C1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    await cache.has(api, "C1", "message")
    await cache.has(api, "C1", "message")
    assert api.get_calls == 1  # second lookup served from cache


async def test_invalidate_forces_refetch():
    api = _FakeAPI([["C1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    await cache.has(api, "C1", "message")
    cache.invalidate()
    await cache.has(api, "C1", "message")
    assert api.get_calls == 2
