"""Tests for the per-guild hourly image-generation quota (Redis fixed window)."""

from __future__ import annotations

from smarter_dev.web.image_quota import IMAGES_PER_HOUR, ImageQuotaLimiter


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

    async def get(self, key):
        return self.store.get(key)

    async def ttl(self, key):
        if key not in self.store:
            return -2
        return self.expiries.get(key, -1)

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def decr(self, key):
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]


async def test_peek_fresh_guild_reports_full_budget():
    limiter = ImageQuotaLimiter(redis=_FakeRedis())
    status = await limiter.peek("guild")
    assert status.remaining == IMAGES_PER_HOUR
    assert status.limit == IMAGES_PER_HOUR
    assert status.granted is True
    # Nothing spent yet, so there's no window to wait on.
    assert status.resets_at is None
    assert status.retry_after_seconds is None


async def test_peek_does_not_spend():
    limiter = ImageQuotaLimiter(redis=_FakeRedis())
    for _ in range(3):
        await limiter.peek("guild")
    assert (await limiter.peek("guild")).remaining == IMAGES_PER_HOUR


async def test_reserve_counts_down_then_denies_without_inflating():
    limiter = ImageQuotaLimiter(redis=_FakeRedis())
    outcomes = [(r.granted, r.remaining) for r in
                [await limiter.reserve("g") for _ in range(IMAGES_PER_HOUR + 2)]]
    assert outcomes == [
        (True, 4), (True, 3), (True, 2), (True, 1), (True, 0),
        (False, 0), (False, 0),
    ]
    # Over-limit reserves are undone, so the window still resets normally.
    exhausted = await limiter.peek("g")
    assert exhausted.remaining == 0
    assert exhausted.resets_at is not None
    assert exhausted.retry_after_seconds == 3600


async def test_reserve_fixes_window_on_first_hit():
    fake = _FakeRedis()
    limiter = ImageQuotaLimiter(redis=fake)
    await limiter.reserve("g")
    await limiter.reserve("g")
    # EXPIRE was NX, so the window doesn't slide with each spend.
    assert fake.expiries == {"imgquota:g": 3600}


async def test_release_refunds_one_slot():
    limiter = ImageQuotaLimiter(redis=_FakeRedis())
    for _ in range(IMAGES_PER_HOUR):
        await limiter.reserve("g")
    assert (await limiter.peek("g")).remaining == 0
    await limiter.release("g")
    assert (await limiter.peek("g")).remaining == 1


async def test_release_never_goes_below_zero():
    limiter = ImageQuotaLimiter(redis=_FakeRedis())
    await limiter.release("g")  # nothing spent
    assert (await limiter.peek("g")).remaining == IMAGES_PER_HOUR


async def test_guilds_have_independent_budgets():
    limiter = ImageQuotaLimiter(redis=_FakeRedis())
    for _ in range(IMAGES_PER_HOUR):
        await limiter.reserve("a")
    assert (await limiter.peek("a")).remaining == 0
    assert (await limiter.peek("b")).remaining == IMAGES_PER_HOUR
