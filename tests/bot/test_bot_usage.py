"""Tests for the ``/bot-usage`` slash command."""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import hikari
import pytest
from redis.exceptions import RedisError

from smarter_dev.bot.plugins import bot_usage as bot_usage_plugin
from smarter_dev.bot.plugins.bot_usage import LEADERBOARD_LIMIT
from smarter_dev.bot.plugins.bot_usage import build_usage_leaderboard
from smarter_dev.bot.services.channel_token_budget import HOUR_WINDOW_SECONDS
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
async def test_report_without_override_shows_usage_over_unlimited():
    bot = _bot(redis=MagicMock(), override=None)
    counters = _patch_counters(window_usage=(76_000, 512_000))
    with counters[0], counters[1]:
        report = await build_usage_report(bot, "200", "G", "C")

    assert "**Bot usage here:** 76k/unlimited this hour · 512k/unlimited today" in report
    # The personal counter still renders.
    assert f"**Your messages:** 0/{USER_MESSAGE_LIMIT}" in report
    assert "in the last 4 hours" in report


@pytest.mark.asyncio
async def test_report_maxed_budget_window_shows_its_reset_countdown():
    bot = _bot(redis=MagicMock(), override=_override(daily=1_000_000, hourly=100_000))
    counters = _patch_counters(window_usage=(100_000, 512_000))
    with counters[0], counters[1]:
        report = await build_usage_report(bot, "200", "G", "C")

    match = re.search(r"100k/100k this hour \(resets <t:(\d+):R>\)", report)
    assert match, report
    reset_epoch = int(match.group(1))
    # The countdown targets the next wall-clock hour boundary.
    assert reset_epoch % HOUR_WINDOW_SECONDS == 0
    assert time.time() < reset_epoch <= time.time() + HOUR_WINDOW_SECONDS
    # The unmaxed day window carries no countdown.
    assert "512k/1m today" in report
    assert report.count("resets") == 1


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


# --------------------------------------------------------------------------- #
# /bot-usage-info leaderboard
# --------------------------------------------------------------------------- #


def _leaderboard_bot(payload=None, status_code=200):
    api = MagicMock()
    response = SimpleNamespace(
        status_code=status_code, json=lambda: payload or {"entries": []}
    )
    api.get = AsyncMock(return_value=response)
    return SimpleNamespace(d={"api_client": api}), api


@pytest.mark.asyncio
async def test_leaderboard_renders_top_channels_for_the_range():
    bot, api = _leaderboard_bot(
        {
            "entries": [
                {"channel_id": "111", "channel_name": "general", "total_tokens": 512_000},
                {"channel_id": "222", "channel_name": None, "total_tokens": 76_000},
            ]
        }
    )

    report = await build_usage_leaderboard(bot, "G", "week")

    assert "**Bot token usage — last week** (top 2 channels)" in report
    assert "1. <#111> — 512k" in report
    assert "2. <#222> — 76k" in report
    api.get.assert_awaited_once_with(
        "/chat-conversations/usage-leaderboard",
        params={"guild_id": "G", "days": 7, "limit": LEADERBOARD_LIMIT},
    )


@pytest.mark.asyncio
async def test_leaderboard_ranges_map_to_days():
    for range_key, days in (("day", 1), ("month", 30), ("year", 365)):
        bot, api = _leaderboard_bot()
        await build_usage_leaderboard(bot, "G", range_key)
        assert api.get.await_args.kwargs["params"]["days"] == days


@pytest.mark.asyncio
async def test_leaderboard_empty_window_says_so():
    bot, _ = _leaderboard_bot({"entries": []})
    report = await build_usage_leaderboard(bot, "G", "day")
    assert report == "No bot token usage recorded in the last day."


@pytest.mark.asyncio
async def test_leaderboard_degrades_on_api_failure():
    bot, _ = _leaderboard_bot(status_code=500)
    assert "unavailable right now" in await build_usage_leaderboard(bot, "G", "day")

    no_api_bot = SimpleNamespace(d={})
    assert "unavailable right now" in await build_usage_leaderboard(
        no_api_bot, "G", "day"
    )


@pytest.mark.asyncio
async def test_leaderboard_command_responds_ephemerally():
    ctx = Mock()
    ctx.guild_id = "G"
    ctx.respond = AsyncMock()
    ctx.options = SimpleNamespace(range="month")
    ctx.bot, _ = _leaderboard_bot(
        {"entries": [{"channel_id": "111", "channel_name": None, "total_tokens": 10}]}
    )

    await bot_usage_plugin.bot_usage_info(ctx)

    ctx.respond.assert_awaited_once()
    args, kwargs = ctx.respond.await_args
    assert "last month" in args[0]
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL


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
