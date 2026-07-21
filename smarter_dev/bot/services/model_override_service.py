"""Bot-side service for per-channel LLM model overrides.

Wraps the web API's ``/guilds/{guild_id}/channels/{channel_id}/model-override``
endpoints so the admin ``/chat-bot-settings`` slash command (stage 03) and the chat runtime
(stage 04) can read/write a channel's model + token budgets without touching the
DB directly.

A short-TTL in-process cache fronts reads so the hot chat path (usually a
"no override" lookup on every turn) does not pay an HTTP round trip each time.
The common no-override result is cached too, and every write invalidates the
channel's entry so reopening the modal reflects the new value immediately.
"""

from __future__ import annotations

import logging
import time

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
        # In-process TTL cache keyed by (guild_id, channel_id). The bot constructs
        # this service without a shared cache manager, so BaseService caching would
        # no-op; this local cache keeps the per-turn override lookup off the wire.
        # Value is the override or ``None`` (the common no-override case).
        self._override_cache: dict[
            tuple[str, str], tuple[float, ChannelModelOverride | None]
        ] = {}

    @staticmethod
    def _path(guild_id: str, channel_id: str) -> str:
        return f"/guilds/{guild_id}/channels/{channel_id}/model-override"

    def _invalidate_override(self, guild_id: str, channel_id: str) -> None:
        self._override_cache.pop((guild_id, channel_id), None)

    async def get_override(
        self, guild_id: str, channel_id: str
    ) -> ChannelModelOverride | None:
        """Return the channel's override, or ``None`` if none is set."""
        cache_key = (guild_id, channel_id)
        cached = self._override_cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]

        try:
            response = await self._api_client.get(self._path(guild_id, channel_id))
        except APIError as error:
            # The API client raises for any status >= 400; a 404 simply means the
            # channel has no override configured. Anything else is a real failure.
            if error.status_code == 404:
                override = None
            else:
                raise
        else:
            override = ChannelModelOverride.from_api_response(response.json())

        self._override_cache[cache_key] = (
            time.monotonic() + _OVERRIDE_CACHE_TTL,
            override,
        )
        return override

    async def set_override(
        self,
        guild_id: str,
        channel_id: str,
        model_key: str | None,
        daily_token_budget: int,
        hourly_token_budget: int,
        reasoning_level: str | None = None,
        auto_respond: bool = False,
        fallback_model_key: str | None = None,
        response_filter: str | None = None,
    ) -> ChannelModelOverride:
        """Upsert the channel's override and return the stored value.

        ``model_key`` of ``None`` keeps the server default model while the
        budgets and behaviour settings still apply. ``reasoning_level`` of
        ``None`` means "use the model's default level". ``auto_respond`` makes
        the bot reply to any message, not just @mentions;
        ``fallback_model_key`` and ``response_filter`` default to "unset".
        """
        response = await self._api_client.put(
            self._path(guild_id, channel_id),
            json_data={
                "model_key": model_key,
                "reasoning_level": reasoning_level,
                "daily_token_budget": daily_token_budget,
                "hourly_token_budget": hourly_token_budget,
                "auto_respond": auto_respond,
                "fallback_model_key": fallback_model_key,
                "response_filter": response_filter,
            },
        )
        self._invalidate_override(guild_id, channel_id)
        return ChannelModelOverride.from_api_response(response.json())

    async def clear_override(self, guild_id: str, channel_id: str) -> None:
        """Remove the channel's override (idempotent)."""
        await self._api_client.delete(self._path(guild_id, channel_id))
        self._invalidate_override(guild_id, channel_id)
