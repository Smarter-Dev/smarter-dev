"""Bot-side event dispatch for the agentic handler system.

Listens for user messages and reactions and, when a channel has an event handler
installed, asks the web API to enqueue a fire. Two invariants enforced here:

- ONLY user actions fire triggers. The bot's own messages and reactions are
  ignored, which structurally prevents trigger loops (bot reacts -> fires ->
  reacts -> ...).
- The hot path stays cheap: a short-TTL in-memory cache of which (channel,
  trigger) pairs have a handler means we don't hit the API on every message —
  only when the channel actually has a handler.

Actual execution (the sandbox, the budget, emitting) happens in the worker; this
just decides whether to dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import hikari
import lightbulb

from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("handler_events")

ACTIVITY_FLUSH_SECONDS = 30.0


class ActivityBatcher:
    """Collects (guild, user) -> latest message time and flushes in one call.

    Every human guild message is recorded (not just handler channels) so the
    activity facts in handler contexts reflect real guild-wide activity. One
    API call per flush interval instead of one per message; a failed flush
    re-queues its events without regressing anything newer.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], datetime] = {}

    def record(self, guild_id: str, user_id: str, message_at: datetime) -> None:
        key = (guild_id, user_id)
        current = self._pending.get(key)
        if current is None or message_at > current:
            self._pending[key] = message_at

    async def flush(self, api: Any) -> None:
        if not self._pending:
            return
        taken, self._pending = self._pending, {}
        events = [
            {"guild_id": g, "user_id": u, "message_at": at.isoformat()}
            for (g, u), at in taken.items()
        ]
        try:
            await api.post("/activity/batch", json_data={"events": events})
        except Exception:  # noqa: BLE001 — activity is best-effort; keep for retry
            logger.debug("activity flush failed; re-queueing", exc_info=True)
            for (g, u), at in taken.items():
                self.record(g, u, at)

    async def run(self, api: Any) -> None:
        while True:
            await asyncio.sleep(ACTIVITY_FLUSH_SECONDS)
            await self.flush(api)


_activity = ActivityBatcher()


_DISCORD_EPOCH_MS = 1420070400000


def _snowflake_created_at(snowflake: int) -> str:
    """ISO-8601 UTC creation time encoded in a Discord snowflake id."""
    from datetime import datetime, timezone

    ms = (snowflake >> 22) + _DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


class ActiveChannelsCache:
    """Short-TTL cache of which (channel, trigger) and (guild, trigger) fire.

    ``_pairs`` covers standard + channel-scoped admin handlers; ``_guild_triggers``
    covers admin handlers scoped to all channels in a guild.
    """

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._pairs: set[tuple[str, str]] = set()
        self._guild_triggers: set[tuple[str, str]] = set()
        self._expires_at: float = 0.0

    def invalidate(self) -> None:
        self._expires_at = 0.0

    async def _refresh(self, api: Any) -> None:
        resp = await api.get("/handlers/active-channels")
        if resp.status_code < 400:
            data = resp.json()
            self._pairs = {(str(c), str(t)) for c, t in data.get("channels", [])}
            self._guild_triggers = {
                (str(g), str(t)) for g, t in data.get("guild_triggers", [])
            }
            self._expires_at = time.monotonic() + self._ttl

    async def has(
        self, api: Any, channel_id: str, guild_id: str, trigger_type: str
    ) -> bool:
        if time.monotonic() >= self._expires_at:
            try:
                await self._refresh(api)
            except Exception:  # noqa: BLE001 — never let dispatch crash on a cache miss
                logger.debug("active-channels refresh failed", exc_info=True)
                return False
        return (
            (str(channel_id), str(trigger_type)) in self._pairs
            or (str(guild_id), str(trigger_type)) in self._guild_triggers
        )


_cache = ActiveChannelsCache()
_api_client: Any = None


def _get_api_client() -> Any:
    global _api_client
    if _api_client is None:
        from smarter_dev.bot.services.api_client import APIClient

        settings = get_settings()
        _api_client = APIClient(
            base_url=settings.api_base_url, api_key=settings.bot_api_key
        )
    return _api_client


async def _dispatch(
    channel_id: str, guild_id: str, trigger_type: str, context: dict
) -> None:
    api = _get_api_client()
    if not await _cache.has(api, channel_id, guild_id, trigger_type):
        return
    try:
        await api.post(
            "/handlers/dispatch",
            json_data={
                "guild_id": guild_id,
                "channel_id": channel_id,
                "trigger_type": trigger_type,
                "trigger_context": context,
            },
        )
    except Exception:  # noqa: BLE001 — dispatch is best-effort for a toy
        logger.debug("handler dispatch failed", exc_info=True)


@plugin.listener(hikari.StartedEvent)
async def start_activity_flush(_: hikari.StartedEvent) -> None:
    asyncio.create_task(_activity.run(_get_api_client()))


@plugin.listener(hikari.GuildMessageCreateEvent)
async def on_message(event: hikari.GuildMessageCreateEvent) -> None:
    # Only user actions fire triggers.
    if not event.is_human:
        return
    _activity.record(
        str(event.guild_id),
        str(event.message.author.id),
        datetime.now(timezone.utc),
    )
    msg = event.message
    joined_at = None
    if event.member is not None and event.member.joined_at is not None:
        joined_at = event.member.joined_at.isoformat()
    attachments = [
        {
            "url": a.url,
            "content_type": a.media_type or "",
            "filename": a.filename or "",
        }
        for a in msg.attachments
    ]
    await _dispatch(
        str(event.channel_id),
        str(event.guild_id),
        "message",
        {
            "trigger_type": "message",
            "message_content": msg.content or "",
            "message_id": str(msg.id),
            "author_id": str(msg.author.id),
            "author_name": msg.author.username,
            # For admin handlers that gate on new accounts / recent joiners.
            "author_account_created_at": _snowflake_created_at(int(msg.author.id)),
            "author_joined_at": joined_at,
            # Files posted with the message — scripts can read these via the
            # gathering agent's web_read tool (it handles image/pdf/audio urls).
            "attachments": attachments,
        },
    )


@plugin.listener(hikari.GuildReactionAddEvent)
async def on_reaction(event: hikari.GuildReactionAddEvent) -> None:
    # Ignore the bot's own reactions — only user actions fire triggers.
    me = plugin.bot.get_me()
    if me is not None and event.user_id == me.id:
        return
    emoji = event.emoji_name or (str(event.emoji_id) if event.emoji_id else "")
    await _dispatch(
        str(event.channel_id),
        str(event.guild_id),
        "reaction",
        {
            "trigger_type": "reaction",
            "reaction_emoji": emoji,
            "reaction_message_id": str(event.message_id),
            "reaction_user_id": str(event.user_id),
        },
    )


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)
    logger.info("Handler events plugin loaded (handler dispatch)")


def unload(bot: lightbulb.BotApp) -> None:
    bot.remove_plugin(plugin)
