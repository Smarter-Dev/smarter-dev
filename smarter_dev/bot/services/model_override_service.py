"""Bot-side service for per-channel LLM model overrides.

Wraps the web API's ``/guilds/{guild_id}/channels/{channel_id}/model-override``
endpoints so the admin ``/model`` slash command (stage 03) and the chat runtime
(stage 04) can read/write a channel's model + token budgets without touching the
DB directly.

A short-TTL cache fronts reads; every write invalidates it so reopening the modal
reflects the new value immediately. In production the bot runs without a cache
manager, so caching transparently no-ops.
"""

from __future__ import annotations

import logging

from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.cache_manager import CacheManager
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.models import ChannelModelOverride

logger = logging.getLogger(__name__)

# Short cache lifetime: overrides change rarely but a stale read right after the
# admin modal submit would be confusing, so keep it tight (and writes invalidate).
_OVERRIDE_CACHE_TTL = 60


class ModelOverrideService(BaseService):
    """Read/write per-channel model overrides via the web API."""

    def __init__(
        self, api_client: APIClient, cache_manager: CacheManager | None = None
    ):
        super().__init__(api_client, cache_manager, service_name="ModelOverrideService")

    def _cache_key(self, guild_id: str, channel_id: str) -> str:
        return self._build_cache_key("override", guild_id, channel_id)

    @staticmethod
    def _path(guild_id: str, channel_id: str) -> str:
        return f"/guilds/{guild_id}/channels/{channel_id}/model-override"

    async def get_override(
        self, guild_id: str, channel_id: str
    ) -> ChannelModelOverride | None:
        """Return the channel's override, or ``None`` if none is set."""
        cache_key = self._cache_key(guild_id, channel_id)
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return ChannelModelOverride.from_api_response(cached)

        response = await self._api_client.get(self._path(guild_id, channel_id))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise APIError(
                f"Failed to get model override: {response.status_code}",
                status_code=response.status_code,
            )

        data = response.json()
        await self._set_cached(cache_key, data, ttl=_OVERRIDE_CACHE_TTL)
        return ChannelModelOverride.from_api_response(data)

    async def set_override(
        self,
        guild_id: str,
        channel_id: str,
        model_key: str,
        daily_token_budget: int,
        hourly_token_budget: int,
    ) -> ChannelModelOverride:
        """Upsert the channel's override and return the stored value."""
        response = await self._api_client.put(
            self._path(guild_id, channel_id),
            json_data={
                "model_key": model_key,
                "daily_token_budget": daily_token_budget,
                "hourly_token_budget": hourly_token_budget,
            },
        )
        if response.status_code != 200:
            raise APIError(
                f"Failed to set model override: {response.status_code}",
                status_code=response.status_code,
            )

        await self._invalidate_cache(self._cache_key(guild_id, channel_id))
        return ChannelModelOverride.from_api_response(response.json())

    async def clear_override(self, guild_id: str, channel_id: str) -> None:
        """Remove the channel's override (idempotent)."""
        response = await self._api_client.delete(self._path(guild_id, channel_id))
        if response.status_code not in (200, 204):
            raise APIError(
                f"Failed to clear model override: {response.status_code}",
                status_code=response.status_code,
            )
        await self._invalidate_cache(self._cache_key(guild_id, channel_id))
