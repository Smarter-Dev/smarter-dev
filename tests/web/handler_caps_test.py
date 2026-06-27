"""Tests for the Redis-backed windowed caps (atomic INCR + EXPIRE NX)."""

from __future__ import annotations

from smarter_dev.web.handler_caps import (
    WindowedLimiter,
    fires_per_min_for_trigger,
    HANDLER_FIRES_PER_MIN_MESSAGE,
    HANDLER_FIRES_PER_MIN_REACTION,
)


class _FakePipeline:
    """Mimics redis.asyncio pipeline: queued sync ops, awaited execute()."""

    def __init__(self, store: dict, expiries: dict):
        self._store = store
        self._expiries = expiries
        self._ops: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def expire(self, key, seconds, nx=False):
        self._ops.append(("expire", key, seconds, nx))
        return self

    async def execute(self):
        results = []
        for op in self._ops:
            if op[0] == "incr":
                self._store[op[1]] = self._store.get(op[1], 0) + 1
                results.append(self._store[op[1]])
            else:
                _, key, seconds, nx = op
                if nx and key in self._expiries:
                    results.append(False)
                else:
                    self._expiries[key] = seconds
                    results.append(True)
        self._ops.clear()
        return results


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.expiries: dict = {}

    def pipeline(self, transaction=True):
        return _FakePipeline(self.store, self.expiries)


async def test_limiter_allows_up_to_limit_then_denies():
    limiter = WindowedLimiter(redis=_FakeRedis())
    key = "hcap:test"
    allowed = [await limiter.hit(key, limit=3) for _ in range(5)]
    assert allowed == [True, True, True, False, False]


async def test_expiry_fixed_on_first_hit_not_slid():
    fake = _FakeRedis()
    limiter = WindowedLimiter(redis=fake, window_seconds=60)
    await limiter.hit("k", limit=10)
    await limiter.hit("k", limit=10)
    # EXPIRE was only set once (NX), so the window does not slide.
    assert fake.expiries == {"k": 60}
    assert fake.store["k"] == 2


def test_reaction_triggers_have_tighter_fire_ceiling():
    assert fires_per_min_for_trigger("reaction") == HANDLER_FIRES_PER_MIN_REACTION
    assert fires_per_min_for_trigger("message") == HANDLER_FIRES_PER_MIN_MESSAGE
    assert HANDLER_FIRES_PER_MIN_REACTION < HANDLER_FIRES_PER_MIN_MESSAGE
