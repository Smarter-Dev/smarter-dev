"""Read/write per-channel proactive-bot settings via the web API.

Mirrors ModelOverrideService: in-process TTL cache, 404 == the default
(disabled, no addendum). The enabled flag is checked on every message event
in watched guilds, so the cache keeps that hot path off the wire.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

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

    @classmethod
    def from_api_response(cls, payload: dict) -> ProactiveChannelSettings:
        return cls(
            guild_id=payload["guild_id"],
            channel_id=payload["channel_id"],
            enabled=payload["enabled"],
            watch_addendum=payload["watch_addendum"],
        )

    @classmethod
    def default(cls, guild_id: str, channel_id: str) -> ProactiveChannelSettings:
        return cls(
            guild_id=guild_id, channel_id=channel_id, enabled=False,
            watch_addendum="",
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

    def _invalidate(self, guild_id: str, channel_id: str) -> None:
        self._settings_cache.pop((guild_id, channel_id), None)

    async def get_settings(
        self, guild_id: str, channel_id: str
    ) -> ProactiveChannelSettings:
        """The channel's settings; 404 means the default (disabled)."""
        cache_key = (guild_id, channel_id)
        cached = self._settings_cache.get(cache_key)
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
        self, guild_id: str, channel_id: str, enabled: bool
    ) -> ProactiveChannelSettings:
        """Flip the channel switch, preserving the stored watch addendum."""
        current = await self.get_settings(guild_id, channel_id)
        return await self._put(
            guild_id, channel_id, enabled=enabled,
            watch_addendum=current.watch_addendum,
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

    async def _put(
        self, guild_id: str, channel_id: str, *, enabled: bool, watch_addendum: str
    ) -> ProactiveChannelSettings:
        response = await self._api_client.put(
            self._path(guild_id, channel_id),
            json_data={"enabled": enabled, "watch_addendum": watch_addendum},
        )
        self._invalidate(guild_id, channel_id)
        settings = ProactiveChannelSettings.from_api_response(response.json())
        self._settings_cache[(guild_id, channel_id)] = (
            time.monotonic() + _SETTINGS_CACHE_TTL,
            settings,
        )
        return settings
