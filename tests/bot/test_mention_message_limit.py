"""Tests for the per-user message-limit gate in the mention plugin."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from redis.exceptions import RedisError

from smarter_dev.bot.plugins.mention import _record_engaged_message
from smarter_dev.bot.plugins.mention import _reject_when_over_limit
from smarter_dev.bot.services.user_message_limit import LIMIT_WINDOW_SECONDS
from smarter_dev.bot.services.user_message_limit import OverLimitStatus
from smarter_dev.bot.services.user_message_limit import USER_MESSAGE_LIMIT
from smarter_dev.bot.services.user_message_limit import UsageWarning


def _bot(redis) -> SimpleNamespace:
    rest = MagicMock()
    rest.create_message = AsyncMock()
    return SimpleNamespace(rest=rest, d={"chat_memory_redis": redis})


def _event(user_id: int = 200, message_id: int = 555) -> SimpleNamespace:
    message = SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=user_id, is_bot=False),
        created_at=datetime.now(UTC),
    )
    return SimpleNamespace(message=message, channel_id=42)


def _over_limit_status() -> OverLimitStatus:
    now_epoch = datetime.now(UTC).timestamp()
    return OverLimitStatus(
        window_started_epoch=now_epoch - 600,
        retry_epoch=int(now_epoch - 600) + LIMIT_WINDOW_SECONDS,
    )


@pytest.mark.asyncio
async def test_under_limit_allows_the_message():
    bot = _bot(MagicMock())
    with patch(
        "smarter_dev.bot.plugins.mention.over_limit_status",
        new=AsyncMock(return_value=None),
    ):
        assert await _reject_when_over_limit(bot, _event()) is False
    bot.rest.create_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_over_limit_drops_and_pings_once():
    bot = _bot(MagicMock())
    status = _over_limit_status()
    event = _event(user_id=200)
    with patch(
        "smarter_dev.bot.plugins.mention.over_limit_status",
        new=AsyncMock(return_value=status),
    ), patch(
        "smarter_dev.bot.plugins.mention.claim_notice_throttle",
        new=AsyncMock(return_value=True),
    ):
        assert await _reject_when_over_limit(bot, event) is True

    bot.rest.create_message.assert_awaited_once()
    args, kwargs = bot.rest.create_message.await_args
    notice = args[1]
    assert notice.startswith("> -# <@200> ")
    assert f"you've sent {USER_MESSAGE_LIMIT} messages to the bot" in notice
    assert f"<t:{status.retry_epoch}:R>" in notice
    # The reply carries an explicit user mention so the ping always lands.
    assert kwargs["user_mentions"] == [200]
    assert kwargs["reply"] is event.message


@pytest.mark.asyncio
async def test_over_limit_stays_silent_once_the_episode_is_claimed():
    bot = _bot(MagicMock())
    with patch(
        "smarter_dev.bot.plugins.mention.over_limit_status",
        new=AsyncMock(return_value=_over_limit_status()),
    ), patch(
        "smarter_dev.bot.plugins.mention.claim_notice_throttle",
        new=AsyncMock(return_value=False),
    ):
        assert await _reject_when_over_limit(bot, _event()) is True
    bot.rest.create_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_redis_never_blocks():
    bot = SimpleNamespace(rest=MagicMock(), d={})
    assert await _reject_when_over_limit(bot, _event()) is False


@pytest.mark.asyncio
async def test_redis_error_never_blocks():
    bot = _bot(MagicMock())
    with patch(
        "smarter_dev.bot.plugins.mention.over_limit_status",
        new=AsyncMock(side_effect=RedisError("down")),
    ):
        assert await _reject_when_over_limit(bot, _event()) is False


@pytest.mark.asyncio
async def test_notice_send_failure_still_drops_the_message():
    bot = _bot(MagicMock())
    bot.rest.create_message = AsyncMock(side_effect=RuntimeError("boom"))
    with patch(
        "smarter_dev.bot.plugins.mention.over_limit_status",
        new=AsyncMock(return_value=_over_limit_status()),
    ), patch(
        "smarter_dev.bot.plugins.mention.claim_notice_throttle",
        new=AsyncMock(return_value=True),
    ):
        assert await _reject_when_over_limit(bot, _event()) is True


@pytest.mark.asyncio
async def test_engaged_message_is_charged_to_its_author():
    bot = _bot(MagicMock())
    event = _event(user_id=200, message_id=555)
    with patch(
        "smarter_dev.bot.plugins.mention.record_directed_messages",
        new=AsyncMock(),
    ) as record_mock:
        await _record_engaged_message(bot, event)

    record_mock.assert_awaited_once()
    _, user_id, message_epochs = record_mock.await_args.args
    assert user_id == "200"
    assert list(message_epochs) == ["555"]
    assert message_epochs["555"] == pytest.approx(
        event.message.created_at.timestamp()
    )


@pytest.mark.asyncio
async def test_engaged_message_pings_for_a_new_usage_warning():
    bot = _bot(MagicMock())
    event = _event(user_id=200, message_id=555)
    warning = UsageWarning(percentage=80, reset_epoch=1_800_000_000)
    with patch(
        "smarter_dev.bot.plugins.mention.record_directed_messages",
        new=AsyncMock(return_value=[warning]),
    ):
        await _record_engaged_message(bot, event)

    bot.rest.create_message.assert_awaited_once_with(
        42,
        "-# <@200> you've used 80% of your 4hr chat bot limit, "
        "resets <t:1800000000:R>",
        user_mentions=[200],
    )


@pytest.mark.asyncio
async def test_engaged_charge_survives_redis_error():
    bot = _bot(MagicMock())
    with patch(
        "smarter_dev.bot.plugins.mention.record_directed_messages",
        new=AsyncMock(side_effect=RedisError("down")),
    ):
        await _record_engaged_message(bot, _event())
