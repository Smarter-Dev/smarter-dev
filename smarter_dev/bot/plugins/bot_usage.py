"""``/bot-usage`` and ``/bot-usage-info`` slash commands.

Both available to everyone. ``/bot-usage`` responds ephemerally with the
invoker's personal numbers; ``/bot-usage-info`` posts its leaderboard
publicly so the channel can discuss it. ``/bot-usage`` shows two things:

- The invoker's rolling per-user message counter (see
  :mod:`~smarter_dev.bot.services.user_message_limit`), framed over the hours
  the counted messages actually span ("32/60 messages in the last hour").
- The channel's chat-token consumption for the current hour and day windows
  against its configured budgets ("76k/100k this hour · 512k/1m today").
  Every channel is metered; without a ``/setmodel`` budget the allowance
  reads "unlimited", and a maxed-out budget window shows a live countdown
  to its wall-clock reset.

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

from smarter_dev.bot.services.channel_token_budget import DAY_WINDOW_SECONDS
from smarter_dev.bot.services.channel_token_budget import HOUR_WINDOW_SECONDS
from smarter_dev.bot.services.channel_token_budget import current_window_usage
from smarter_dev.bot.services.channel_token_budget import next_window_reset_epoch
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


def _render_window_usage(
    used: int, budget: int, label: str, now_epoch: float, window_seconds: int
) -> str:
    """One window's "76k/100k this hour" — plus, once a budgeted window is
    maxed, a live countdown to its wall-clock reset."""
    rendered = f"{format_compact_tokens(used)}/{_render_budget_side(budget)} {label}"
    if budget > 0 and used >= budget:
        reset_epoch = next_window_reset_epoch(now_epoch, window_seconds)
        rendered += f" (resets <t:{reset_epoch}:R>)"
    return rendered


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
    # No override → no budgets to enforce; usage still shows, over "unlimited".
    hourly_budget = override.hourly_token_budget if override is not None else 0
    daily_budget = override.daily_token_budget if override is not None else 0
    now_epoch = datetime.now(UTC).timestamp()
    hour = _render_window_usage(
        hour_used, hourly_budget, "this hour", now_epoch, HOUR_WINDOW_SECONDS
    )
    day = _render_window_usage(
        day_used, daily_budget, "today", now_epoch, DAY_WINDOW_SECONDS
    )
    return f"**Bot usage here:** {hour} · {day}"


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


# ---------------------------------------------------------------------------
# /bot-usage-info — per-channel token leaderboard

LEADERBOARD_LIMIT = 20

# Command option value → how many days back the leaderboard window reaches.
LEADERBOARD_RANGES = {"day": 1, "week": 7, "month": 30, "year": 365}


def _api_client(bot: Any) -> Any | None:
    """The bot's shared APIClient, or None when unavailable (fail-soft)."""
    data = getattr(bot, "d", None)
    if not isinstance(data, dict):
        return None
    return data.get("api_client")


async def build_usage_leaderboard(bot: Any, guild_id: str, range_key: str) -> str:
    """The `/bot-usage-info` response: top channels by chat tokens.

    Reads the persisted turn data via the web API — the Redis meters only
    hold the current hour/day windows, so anything longer comes from the
    database. Failures degrade to a friendly one-liner.
    """
    days = LEADERBOARD_RANGES.get(range_key, 1)
    api = _api_client(bot)
    if api is None:
        return f"Usage data is {_UNAVAILABLE}."
    try:
        resp = await api.get(
            "/chat-conversations/usage-leaderboard",
            params={
                "guild_id": guild_id,
                "days": days,
                "limit": LEADERBOARD_LIMIT,
            },
        )
        if resp.status_code >= 400:
            raise APIError(f"usage-leaderboard returned {resp.status_code}")
        payload = resp.json()
    except Exception:
        logger.warning(
            "Failed to fetch usage leaderboard for guild %s", guild_id,
            exc_info=True,
        )
        return f"Usage data is {_UNAVAILABLE}."

    entries = payload.get("entries", [])
    window_total = format_compact_tokens(int(payload.get("total_tokens_in_window", 0)))
    all_time_total = format_compact_tokens(
        int(payload.get("total_tokens_all_time", 0))
    )
    if not entries:
        return (
            f"No bot token usage recorded in the last {range_key} "
            f"({all_time_total} tokens all time)."
        )

    lines = [
        f"**Bot token usage — last {range_key}** "
        f"(top {len(entries)} channels)",
        f"-# {window_total} tokens in the last {range_key} · "
        f"{all_time_total} all time",
    ]
    for position, entry in enumerate(entries, start=1):
        tokens = format_compact_tokens(int(entry["total_tokens"]))
        lines.append(f"{position}. <#{entry['channel_id']}> — {tokens}")
    return "\n".join(lines)


@plugin.command
@lightbulb.option(
    "range",
    "Time range for the leaderboard",
    type=hikari.OptionType.STRING,
    choices=list(LEADERBOARD_RANGES),
    required=False,
    default="day",
)
@lightbulb.command(
    "bot-usage-info",
    "Top channels/threads by bot token usage",
)
@lightbulb.implements(lightbulb.SlashCommand)
async def bot_usage_info(ctx: lightbulb.Context) -> None:
    """Show the per-channel token leaderboard for the chosen range.

    Posts publicly (unlike ``/bot-usage``) so the channel can see and
    discuss the leaderboard.
    """
    report = await build_usage_leaderboard(
        ctx.bot, str(ctx.guild_id), ctx.options.range
    )
    await ctx.respond(report)


def load(bot: lightbulb.BotApp) -> None:
    """Load the bot-usage plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the bot-usage plugin."""
    bot.remove_plugin(plugin)
