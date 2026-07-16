"""Tests for the ``/bot-usage`` slash command."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import hikari
import pytest
from redis.exceptions import RedisError

from smarter_dev.bot.plugins import bot_usage as bot_usage_plugin
from smarter_dev.bot.plugins.bot_usage import build_usage_report
from smarter_dev.bot.plugins.bot_usage import format_compact_tokens
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.user_message_limit import USER_MESSAGE_LIMIT


def _override(daily: int = 0, hourly: int = 0):
    return SimpleNamespace(
        model_key="gpt-5-4",
        daily_token_budget=daily,
        hourly_token_budget=hourly,
        reasoning_level=None,
    )


def _bot(redis=None, override=None, override_error=None):
    service = MagicMock()
    if override_error is not None:
        service.get_override = AsyncMock(side_effect=override_error)
    else:
        service.get_override = AsyncMock(return_value=override)
    return SimpleNamespace(
        d={
            "chat_memory_redis": redis,
            "model_override_service": service,
        }
    )


def _patch_counters(counted=(0, None), window_usage=(0, 0)):
    return (
        patch(
            "smarter_dev.bot.plugins.bot_usage.counted_messages",
            new=AsyncMock(return_value=counted),
        ),
        patch(
            "smarter_dev.bot.plugins.bot_usage.current_window_usage",
            new=AsyncMock(return_value=window_usage),
        ),
    )


# --------------------------------------------------------------------------- #
# Compact token rendering
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("count", "rendered"),
    [
        (0, "0"),
        (950, "950"),
        (1_000, "1k"),
        (76_000, "76k"),
        (76_500, "76.5k"),
        (100_000, "100k"),
        (512_000, "512k"),
        (1_000_000, "1m"),
        (1_500_000, "1.5m"),
    ],
)
def test_format_compact_tokens(count, rendered):
    assert format_compact_tokens(count) == rendered


# --------------------------------------------------------------------------- #
# Report body
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_report_shows_counter_over_its_actual_span():
    import time

    bot = _bot(redis=MagicMock(), override=_override(daily=1_000_000, hourly=100_000))
    counters = _patch_counters(
        counted=(32, time.time() - 30 * 60), window_usage=(76_000, 512_000)
    )
    with counters[0], counters[1]:
        report = await build_usage_report(bot, "200", "G", "C")

    assert (
        f"**Your messages:** 32/{USER_MESSAGE_LIMIT} in the last hour" in report
    )
    assert "**Bot usage here:** 76k/100k this hour · 512k/1m today" in report


@pytest.mark.asyncio
async def test_report_zero_budget_reads_unlimited():
    bot = _bot(redis=MagicMock(), override=_override(daily=0, hourly=100_000))
    counters = _patch_counters(window_usage=(500, 500))
    with counters[0], counters[1]:
        report = await build_usage_report(bot, "200", "G", "C")

    assert "500/100k this hour · 500/unlimited today" in report


@pytest.mark.asyncio
async def test_report_without_override_says_not_metered():
    bot = _bot(redis=MagicMock(), override=None)
    counters = _patch_counters()
    with counters[0], counters[1]:
        report = await build_usage_report(bot, "200", "G", "C")

    assert "not metered — this channel has no token budget" in report
    # The personal counter still renders.
    assert f"**Your messages:** 0/{USER_MESSAGE_LIMIT}" in report
    assert "in the last 4 hours" in report


@pytest.mark.asyncio
async def test_report_degrades_when_redis_is_missing():
    bot = _bot(redis=None, override=_override(hourly=100))
    report = await build_usage_report(bot, "200", "G", "C")

    assert "**Your messages:** unavailable right now" in report
    assert "**Bot usage here:** unavailable right now" in report


@pytest.mark.asyncio
async def test_report_degrades_on_redis_errors():
    bot = _bot(redis=MagicMock(), override=_override(hourly=100))
    with patch(
        "smarter_dev.bot.plugins.bot_usage.counted_messages",
        new=AsyncMock(side_effect=RedisError("down")),
    ), patch(
        "smarter_dev.bot.plugins.bot_usage.current_window_usage",
        new=AsyncMock(side_effect=RedisError("down")),
    ):
        report = await build_usage_report(bot, "200", "G", "C")

    assert "**Your messages:** unavailable right now" in report
    assert "**Bot usage here:** unavailable right now" in report


@pytest.mark.asyncio
async def test_report_degrades_on_override_api_error():
    bot = _bot(redis=MagicMock(), override_error=APIError("api down"))
    counters = _patch_counters(counted=(5, None))
    with counters[0], counters[1]:
        report = await build_usage_report(bot, "200", "G", "C")

    assert "**Bot usage here:** unavailable right now" in report


# --------------------------------------------------------------------------- #
# Slash command
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_command_responds_ephemerally_with_the_report():
    ctx = Mock()
    ctx.author = SimpleNamespace(id=200)
    ctx.guild_id = "G"
    ctx.channel_id = "C"
    ctx.bot = _bot(redis=MagicMock(), override=None)
    ctx.respond = AsyncMock()

    counters = _patch_counters(counted=(2, None))
    with counters[0], counters[1]:
        await bot_usage_plugin.bot_usage(ctx)

    ctx.respond.assert_awaited_once()
    args, kwargs = ctx.respond.await_args
    assert f"2/{USER_MESSAGE_LIMIT}" in args[0]
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
