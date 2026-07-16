"""Tests for the per-channel token budget (Redis fixed-window counters)."""

from __future__ import annotations

import time

import pytest

from smarter_dev.bot.services.channel_token_budget import (
    DAY_WINDOW_SECONDS,
    HOUR_WINDOW_SECONDS,
    add_usage,
    budget_key,
    over_budget_reset_epoch,
)


def _assert_is_next_boundary(reset_epoch: int, window_seconds: int) -> None:
    """The epoch is wall-aligned to ``window_seconds`` and in its next window."""
    assert reset_epoch % window_seconds == 0
    assert time.time() < reset_epoch <= time.time() + window_seconds


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

    def incrby(self, key, amount):
        self._ops.append(("incrby", key, amount))
        return self

    def expire(self, key, seconds, nx=False):
        self._ops.append(("expire", key, seconds, nx))
        return self

    async def execute(self):
        results = []
        for op in self._ops:
            if op[0] == "incrby":
                self._store[op[1]] = self._store.get(op[1], 0) + op[2]
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


@pytest.mark.asyncio
async def test_add_usage_increments_both_windows_and_sets_ttls():
    redis = _FakeRedis()
    await add_usage(redis, "chan", 100)

    # Exactly one hour key and one day key exist, each holding the tokens.
    hour_keys = [k for k in redis.store if ":hour:" in k]
    day_keys = [k for k in redis.store if ":day:" in k]
    assert len(hour_keys) == 1 and len(day_keys) == 1
    assert redis.store[hour_keys[0]] == 100
    assert redis.store[day_keys[0]] == 100
    # Each window fixed its expiry to its own length on first write.
    assert redis.expiries[hour_keys[0]] == HOUR_WINDOW_SECONDS
    assert redis.expiries[day_keys[0]] == DAY_WINDOW_SECONDS


@pytest.mark.asyncio
async def test_add_usage_accumulates_across_calls():
    redis = _FakeRedis()
    await add_usage(redis, "chan", 100)
    await add_usage(redis, "chan", 50)
    hour_keys = [k for k in redis.store if ":hour:" in k]
    assert redis.store[hour_keys[0]] == 150


@pytest.mark.asyncio
async def test_add_usage_zero_or_negative_is_noop():
    redis = _FakeRedis()
    await add_usage(redis, "chan", 0)
    await add_usage(redis, "chan", -5)
    assert redis.store == {}


@pytest.mark.asyncio
async def test_zero_budgets_never_block():
    redis = _FakeRedis()
    await add_usage(redis, "chan", 10_000_000)
    assert await over_budget_reset_epoch(redis, "chan", 0, 0) is None


@pytest.mark.asyncio
async def test_hourly_budget_blocks_until_the_next_hour_boundary():
    redis = _FakeRedis()
    await add_usage(redis, "chan", 500)
    # Hourly cap of 500 is met; daily cap of 1_000_000 is far off.
    reset_epoch = await over_budget_reset_epoch(redis, "chan", 1_000_000, 500)
    assert reset_epoch is not None
    _assert_is_next_boundary(reset_epoch, HOUR_WINDOW_SECONDS)


@pytest.mark.asyncio
async def test_day_budget_blocks_until_the_next_day_boundary():
    redis = _FakeRedis()
    await add_usage(redis, "chan", 500)
    # Hourly unlimited (0); daily cap of 500 is met.
    reset_epoch = await over_budget_reset_epoch(redis, "chan", 500, 0)
    assert reset_epoch is not None
    _assert_is_next_boundary(reset_epoch, DAY_WINDOW_SECONDS)


@pytest.mark.asyncio
async def test_both_budgets_spent_blocks_until_the_day_boundary():
    redis = _FakeRedis()
    await add_usage(redis, "chan", 500)
    # Both caps met — the unblock moment is the later (day) boundary.
    reset_epoch = await over_budget_reset_epoch(redis, "chan", 500, 500)
    assert reset_epoch is not None
    _assert_is_next_boundary(reset_epoch, DAY_WINDOW_SECONDS)


@pytest.mark.asyncio
async def test_under_budget_does_not_block():
    redis = _FakeRedis()
    await add_usage(redis, "chan", 100)
    assert await over_budget_reset_epoch(redis, "chan", 1000, 1000) is None


@pytest.mark.asyncio
async def test_channels_do_not_share_budget():
    redis = _FakeRedis()
    await add_usage(redis, "chan-a", 500)
    assert await over_budget_reset_epoch(redis, "chan-b", 0, 500) is None


def test_budget_key_shape():
    assert budget_key("42", "hour", 7) == "modelbudget:42:hour:7"
