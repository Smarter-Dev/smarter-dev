"""Per-channel proactive-bot settings API.

Mirrors the model-override controller's shape (one row per channel, PUT
upserts) for the two-pass proactive bot:

- ``GET    /api/guilds/{guild_id}/channels/{channel_id}/proactive-settings`` → row or 404.
- ``PUT    /api/guilds/{guild_id}/channels/{channel_id}/proactive-settings`` → upsert, 200.

The bot treats a GET 404 as "disabled, no addendum" — the default for every
channel — so no DELETE is needed; disabling writes ``enabled=false``.
"""

from __future__ import annotations

from litestar import Controller, get, put
from litestar.status_codes import HTTP_200_OK
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    nested_not_found_error,
)
from smarter_dev.web.api_native.schemas import (
    ProactiveChannelSettingsRead,
    ProactiveChannelSettingsWrite,
)
from smarter_dev.web.crud import (
    get_proactive_channel_settings,
    upsert_proactive_channel_settings,
)

BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE — Skrift's auth_guard inspects
# route_handler.guards for the APIKeyOnly marker (see model_overrides.py).
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


class ProactiveChannelSettingsController(Controller):
    """Per-channel proactive-bot settings (one row per channel, PUT upserts)."""

    path = "/api/guilds/{guild_id:str}/channels/{channel_id:str}/proactive-settings"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get(status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_settings(
        self,
        db_session: AsyncSession,
        guild_id: str,
        channel_id: str,
    ) -> ProactiveChannelSettingsRead:
        """Return the channel's proactive settings, or 404 if never configured."""
        record = await get_proactive_channel_settings(
            db_session, guild_id, channel_id
        )
        if record is None:
            raise nested_not_found_error(
                f"Proactive settings with identifier '{channel_id}' not found"
            )
        return ProactiveChannelSettingsRead.model_validate(record)

    @put(status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def put_settings(
        self,
        db_session: AsyncSession,
        guild_id: str,
        channel_id: str,
        data: ProactiveChannelSettingsWrite,
    ) -> ProactiveChannelSettingsRead:
        """Upsert the channel's proactive settings and return the stored row."""
        record = await upsert_proactive_channel_settings(
            db_session,
            guild_id=guild_id,
            channel_id=channel_id,
            enabled=data.enabled,
            watch_addendum=data.watch_addendum,
        )
        # Serialize before commit (Skrift-injected session; see model_overrides).
        response = ProactiveChannelSettingsRead.model_validate(record)
        await db_session.commit()
        return response
