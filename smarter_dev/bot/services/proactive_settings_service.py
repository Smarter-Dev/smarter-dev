"""Read/write per-channel proactive-bot settings via the web API.

Mirrors ModelOverrideService: in-process TTL cache, 404 == the default
(disabled, no addendum). The enabled flag is checked on every message event
in watched guilds, so the cache keeps that hot path off the wire.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.cache_manager import CacheManager
from smarter_dev.bot.services.exceptions import APIError

_SETTINGS_CACHE_TTL = 60.0


@dataclass(frozen=True)
class ProactiveChannelSettings:
    """Bot-side view of one channel's proactive settings."""

    guild_id: str
    channel_id: str
    enabled: bool
    watch_addendum: str
    # False for the 404 default: the channel has no row of its own, so a
    # forum post may inherit its forum's settings (#248).
    exists: bool = True
    # The row switches on a whole forum, whose posts are the channels.
    is_forum: bool = False
    # A forum's new posts wake the agent without the watcher (#297).
    new_posts_to_agent: bool = False

    @classmethod
    def from_api_response(cls, payload: dict) -> ProactiveChannelSettings:
        return cls(
            guild_id=payload["guild_id"],
            channel_id=payload["channel_id"],
            enabled=payload["enabled"],
            watch_addendum=payload["watch_addendum"],
            is_forum=bool(payload.get("is_forum", False)),
            new_posts_to_agent=bool(payload.get("new_posts_to_agent", False)),
        )

    @classmethod
    def default(cls, guild_id: str, channel_id: str) -> ProactiveChannelSettings:
        return cls(
            guild_id=guild_id, channel_id=channel_id, enabled=False,
            watch_addendum="", exists=False,
        )


@dataclass(frozen=True)
class EnabledProactiveChannel:
    """Bot-side view of an enabled proactive channel."""

    channel_id: str
    watch_addendum: str
    is_forum: bool = False

    @classmethod
    def from_api_response(cls, payload: dict) -> EnabledProactiveChannel:
        return cls(
            channel_id=payload["channel_id"],
            watch_addendum=payload["watch_addendum"],
            is_forum=bool(payload.get("is_forum", False)),
        )


class ProactiveSettingsService(BaseService):
    """Per-channel proactive settings over the bot API."""

    def __init__(
        self, api_client: APIClient, cache_manager: CacheManager | None = None
    ):
        super().__init__(
            api_client, cache_manager, service_name="ProactiveSettingsService"
        )
        self._settings_cache: dict[
            tuple[str, str], tuple[float, ProactiveChannelSettings]
        ] = {}

    @staticmethod
    def _path(guild_id: str, channel_id: str) -> str:
        return f"/guilds/{guild_id}/channels/{channel_id}/proactive-settings"

    def invalidate(self, guild_id: str, channel_id: str) -> None:
        """Drop the cached settings, e.g. after another process changed them."""
        self._settings_cache.pop((guild_id, channel_id), None)

    async def list_enabled_channels(
        self, guild_id: str
    ) -> list[EnabledProactiveChannel]:
        """Return the guild's enabled proactive channels, whole forums
        included (the bot runs their posts)."""
        response = await self._api_client.get(
            f"/guilds/{guild_id}/proactive-settings", params={"forums": "true"}
        )
        return [
            EnabledProactiveChannel.from_api_response(payload)
            for payload in response.json()
        ]

    async def get_settings(
        self, guild_id: str, channel_id: str, *, fresh: bool = False
    ) -> ProactiveChannelSettings:
        """The channel's settings; 404 means the default (disabled).

        ``fresh`` asks the application even when a cached answer is still
        good (and caches the new one), for a check that must see a change
        made elsewhere, such as the web form, moments ago."""
        cache_key = (guild_id, channel_id)
        cached = None if fresh else self._settings_cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]

        try:
            response = await self._api_client.get(self._path(guild_id, channel_id))
        except APIError as error:
            if error.status_code == 404:
                settings = ProactiveChannelSettings.default(guild_id, channel_id)
            else:
                raise
        else:
            settings = ProactiveChannelSettings.from_api_response(response.json())

        self._settings_cache[cache_key] = (
            time.monotonic() + _SETTINGS_CACHE_TTL,
            settings,
        )
        return settings

    async def set_enabled(
        self,
        guild_id: str,
        channel_id: str,
        enabled: bool,
        *,
        is_forum: bool | None = None,
    ) -> ProactiveChannelSettings:
        """Flip the channel switch, preserving the stored watch addendum.

        ``is_forum`` marks a whole forum's row; None keeps the mark."""
        current = await self.get_settings(guild_id, channel_id)
        return await self._put(
            guild_id, channel_id, enabled=enabled,
            watch_addendum=current.watch_addendum, is_forum=is_forum,
        )

    async def set_watch_addendum(
        self, guild_id: str, channel_id: str, watch_addendum: str
    ) -> ProactiveChannelSettings:
        """Persist the agent's updated wake criteria, preserving the switch."""
        current = await self.get_settings(guild_id, channel_id)
        return await self._put(
            guild_id, channel_id, enabled=current.enabled,
            watch_addendum=watch_addendum,
        )

    async def set_new_posts_to_agent(
        self, guild_id: str, forum_id: str, new_posts_to_agent: bool
    ) -> ProactiveChannelSettings:
        """Send a forum's new posts straight to the agent, or back through
        the watcher; the switch and watch addendum are kept."""
        current = await self.get_settings(guild_id, forum_id)
        return await self._put(
            guild_id, forum_id, enabled=current.enabled,
            watch_addendum=current.watch_addendum,
            new_posts_to_agent=new_posts_to_agent,
        )

    async def record_wake_usage(
        self,
        guild_id: str,
        channel_id: str,
        *,
        wake_id: str,
        metered_at: datetime,
        passive: bool,
        responses: int,
        entries: list[dict],
    ) -> None:
        """Report one wake's per-model token spend to the usage ledger."""
        await self._api_client.post(
            f"{self._path(guild_id, channel_id)}/usage",
            json_data={
                "wake_id": wake_id,
                "metered_at": metered_at.isoformat(),
                "passive": passive,
                "responses": responses,
                "entries": entries,
            },
        )

    async def _put(
        self,
        guild_id: str,
        channel_id: str,
        *,
        enabled: bool,
        watch_addendum: str,
        is_forum: bool | None = None,
        new_posts_to_agent: bool | None = None,
    ) -> ProactiveChannelSettings:
        payload = {"enabled": enabled, "watch_addendum": watch_addendum}
        if is_forum is not None:
            payload["is_forum"] = is_forum
        if new_posts_to_agent is not None:
            payload["new_posts_to_agent"] = new_posts_to_agent
        response = await self._api_client.put(
            self._path(guild_id, channel_id), json_data=payload
        )
        self.invalidate(guild_id, channel_id)
        settings = ProactiveChannelSettings.from_api_response(response.json())
        self._settings_cache[(guild_id, channel_id)] = (
            time.monotonic() + _SETTINGS_CACHE_TTL,
            settings,
        )
        return settings
