"""``/bot-usage`` slash command — personal message allowance + channel token usage.

Available to everyone; responds ephemerally. Shows two things:

- The invoker's rolling per-user message counter (see
  :mod:`~smarter_dev.bot.services.user_message_limit`), framed over the hours
  the counted messages actually span ("32/60 messages in the last hour").
- The channel's chat-token consumption for the current hour and day windows
  against its configured budgets ("76k/100k this hour · 512k/1m today").
  Token usage is only metered on channels with a model override, so channels
  without one say so instead of showing numbers.

Every data source is fail-soft: a Redis or API hiccup degrades that line to
"unavailable" rather than failing the command.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

import hikari
import lightbulb
from redis.exceptions import RedisError

from smarter_dev.bot.services.channel_token_budget import current_window_usage
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.user_message_limit import USER_MESSAGE_LIMIT
from smarter_dev.bot.services.user_message_limit import counted_messages
from smarter_dev.bot.services.user_message_limit import format_counted_window

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("bot_usage")

_UNAVAILABLE = "unavailable right now"


def _chat_memory_redis(bot: Any) -> Any | None:
    """The shared chat-memory Redis, or None when unavailable."""
    data = getattr(bot, "d", None)
    if not isinstance(data, dict):
        return None
    return data.get("chat_memory_redis")


def _override_service(bot: Any) -> Any | None:
    """The bot's ModelOverrideService, or None when unavailable (fail-soft)."""
    data = getattr(bot, "d", None)
    if not isinstance(data, dict):
        return None
    service = data.get("model_override_service")
    if service is None:
        service = data.get("_services", {}).get("model_override_service")
    return service


def format_compact_tokens(count: int) -> str:
    """Render a token count the way budgets read in chat: 76k, 512k, 1.5m."""
    for threshold, suffix in ((1_000_000, "m"), (1_000, "k")):
        if count >= threshold:
            rendered = f"{count / threshold:.1f}".rstrip("0").rstrip(".")
            return f"{rendered}{suffix}"
    return str(count)


def _render_budget_side(budget: int) -> str:
    """The allowance half of "used/allowed"; a 0 budget is unlimited."""
    return format_compact_tokens(budget) if budget > 0 else "unlimited"


async def _messages_line(bot: Any, user_id: str) -> str:
    """The invoker's "32/60 messages in the last hour" line."""
    redis = _chat_memory_redis(bot)
    if redis is None:
        return f"**Your messages:** {_UNAVAILABLE}"
    try:
        counted, oldest_epoch = await counted_messages(redis, user_id)
    except RedisError:
        logger.warning(
            "Failed to read message counter for user %s", user_id, exc_info=True
        )
        return f"**Your messages:** {_UNAVAILABLE}"
    span = format_counted_window(oldest_epoch, datetime.now(UTC).timestamp())
    return f"**Your messages:** {counted}/{USER_MESSAGE_LIMIT} in {span}"


async def _channel_tokens_line(bot: Any, guild_id: str, channel_id: str) -> str:
    """The channel's "76k/100k this hour · 512k/1m today" line."""
    override = None
    service = _override_service(bot)
    if service is not None:
        try:
            override = await service.get_override(guild_id, channel_id)
        except APIError:
            logger.warning(
                "Failed to read model override for channel %s", channel_id,
                exc_info=True,
            )
            return f"**Bot usage here:** {_UNAVAILABLE}"
    if override is None:
        return (
            "**Bot usage here:** not metered — this channel has no token budget"
        )
    redis = _chat_memory_redis(bot)
    if redis is None:
        return f"**Bot usage here:** {_UNAVAILABLE}"
    try:
        hour_used, day_used = await current_window_usage(redis, channel_id)
    except RedisError:
        logger.warning(
            "Failed to read token usage for channel %s", channel_id, exc_info=True
        )
        return f"**Bot usage here:** {_UNAVAILABLE}"
    hour = (
        f"{format_compact_tokens(hour_used)}"
        f"/{_render_budget_side(override.hourly_token_budget)}"
    )
    day = (
        f"{format_compact_tokens(day_used)}"
        f"/{_render_budget_side(override.daily_token_budget)}"
    )
    return f"**Bot usage here:** {hour} this hour · {day} today"


async def build_usage_report(
    bot: Any, user_id: str, guild_id: str, channel_id: str
) -> str:
    """The full ``/bot-usage`` response body."""
    return "\n".join(
        (
            await _messages_line(bot, user_id),
            await _channel_tokens_line(bot, guild_id, channel_id),
        )
    )


@plugin.command
@lightbulb.command(
    "bot-usage",
    "Your chat-bot message allowance and this channel's token usage",
)
@lightbulb.implements(lightbulb.SlashCommand)
async def bot_usage(ctx: lightbulb.Context) -> None:
    """Show the invoker their message counter and the channel's token meter."""
    report = await build_usage_report(
        ctx.bot,
        str(ctx.author.id),
        str(ctx.guild_id),
        str(ctx.channel_id),
    )
    await ctx.respond(report, flags=hikari.MessageFlag.EPHEMERAL)


def load(bot: lightbulb.BotApp) -> None:
    """Load the bot-usage plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the bot-usage plugin."""
    bot.remove_plugin(plugin)
