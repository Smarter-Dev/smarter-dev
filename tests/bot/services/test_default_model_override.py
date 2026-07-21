"""Tests for the temporary bot-wide default chat model override store."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from redis.exceptions import RedisError

from smarter_dev.bot.services.default_model_override import (
    DEFAULT_MODEL_OVERRIDE_KEY,
    DefaultModelOverride,
    parse_end_date_utc,
    read_default_model_override,
    set_default_model_override,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# End-date parsing
# --------------------------------------------------------------------------- #


def test_parse_end_date_accepts_utc_minute():
    parsed = parse_end_date_utc("2026-07-25 18:30", NOW)
    assert parsed == datetime(2026, 7, 25, 18, 30, tzinfo=UTC)


def test_parse_end_date_bare_date_lasts_through_that_day():
    parsed = parse_end_date_utc("2026-07-25", NOW)
    assert parsed == datetime(2026, 7, 26, 0, 0, tzinfo=UTC)


def test_parse_end_date_bare_today_still_ends_in_the_future():
    # "Today" as a bare date runs through the end of the current UTC day.
    parsed = parse_end_date_utc("2026-07-21", NOW)
    assert parsed == datetime(2026, 7, 22, 0, 0, tzinfo=UTC)


def test_parse_end_date_strips_whitespace():
    parsed = parse_end_date_utc("  2026-07-25 06:00  ", NOW)
    assert parsed == datetime(2026, 7, 25, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["soon", "07/25/2026", "2026-13-01", "2026-07-25 24:99", ""])
def test_parse_end_date_rejects_malformed_input(raw):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_end_date_utc(raw, NOW)


def test_parse_end_date_rejects_past_end():
    with pytest.raises(ValueError, match="not in the future"):
        parse_end_date_utc("2026-07-21 11:59", NOW)


def test_parse_end_date_rejects_yesterday():
    with pytest.raises(ValueError, match="not in the future"):
        parse_end_date_utc("2026-07-20", NOW)


# --------------------------------------------------------------------------- #
# Redis round trip
# --------------------------------------------------------------------------- #


def _redis_with(raw):
    redis = MagicMock()
    redis.get = AsyncMock(return_value=raw)
    redis.set = AsyncMock()
    return redis


@pytest.mark.asyncio
async def test_set_stores_payload_expiring_at_end_epoch():
    redis = _redis_with(None)
    override = DefaultModelOverride(
        model_key="gemini-3-6-flash",
        reasoning_level="high",
        expires_at_epoch=1_790_000_000,
    )

    await set_default_model_override(redis, override)

    redis.set.assert_awaited_once()
    args, kwargs = redis.set.await_args
    assert args[0] == DEFAULT_MODEL_OVERRIDE_KEY
    assert kwargs["exat"] == 1_790_000_000
    assert json.loads(args[1]) == {
        "model_key": "gemini-3-6-flash",
        "reasoning_level": "high",
        "expires_at_epoch": 1_790_000_000,
    }


@pytest.mark.asyncio
async def test_read_round_trips_a_stored_override():
    redis = _redis_with(None)
    override = DefaultModelOverride(
        model_key="gemini-3-5-flash-lite",
        reasoning_level=None,
        expires_at_epoch=1_790_000_000,
    )
    await set_default_model_override(redis, override)
    stored = redis.set.await_args.args[1]

    assert await read_default_model_override(_redis_with(stored)) == override


@pytest.mark.asyncio
async def test_read_absent_key_returns_none():
    assert await read_default_model_override(_redis_with(None)) is None


@pytest.mark.asyncio
async def test_read_corrupt_payload_returns_none():
    assert await read_default_model_override(_redis_with("not json")) is None
    assert await read_default_model_override(_redis_with('{"nope": 1}')) is None


@pytest.mark.asyncio
async def test_read_redis_error_degrades_to_none():
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=RedisError("down"))
    assert await read_default_model_override(redis) is None
