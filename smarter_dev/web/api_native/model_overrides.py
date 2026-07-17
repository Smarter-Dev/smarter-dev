"""Native Litestar port of the per-channel model-override bot API.

Ports the legacy FastAPI ``routers/model_overrides.py`` (no router prefix) —
part of unit U8 in docs/v2/legacy-sunset/04-api-rewrite.md. Preserves the exact
paths, verbs, status codes, and request/response shapes of the FastAPI
implementation so ``smarter_dev/bot/services/model_override_service.py`` (and any
external caller) needs zero changes:

- ``GET    /api/guilds/{guild_id}/channels/{channel_id}/model-override`` → row or 404.
- ``PUT    /api/guilds/{guild_id}/channels/{channel_id}/model-override`` → upsert, 200.
- ``DELETE /api/guilds/{guild_id}/channels/{channel_id}/model-override`` → 204 (idempotent).

Error-shape parity: the legacy GET answered a missing override with
``create_not_found_error("Model override", channel_id)`` — a nested
``{"detail": {ErrorResponse}}`` body with ``request_id=None`` (the router passed
no request). :func:`errors.nested_not_found_error` reproduces that byte-for-byte.

Status-code parity note: FastAPI declared the DELETE with ``204`` and the
GET/PUT default to 200; the upsert commits AFTER serializing the row (mirroring
the legacy ordering) to avoid touching the Skrift-injected session post-commit.
"""

from __future__ import annotations

from litestar import Controller, delete, get, put
from litestar.status_codes import HTTP_200_OK, HTTP_204_NO_CONTENT
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.web.api_native.schemas import (
    ChannelModelOverrideRead,
    ChannelModelOverrideWrite,
)
from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    nested_not_found_error,
)
from smarter_dev.web.crud import (
    delete_channel_model_override,
    get_channel_model_override,
    upsert_channel_model_override,
)

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


class ChannelModelOverrideController(Controller):
    """Per-channel LLM model-override endpoints (one row per channel, PUT upserts)."""

    path = "/api/guilds/{guild_id:str}/channels/{channel_id:str}/model-override"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get(status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_model_override(
        self,
        db_session: AsyncSession,
        guild_id: str,
        channel_id: str,
    ) -> ChannelModelOverrideRead:
        """Return the channel's model override, or 404 if none is set."""
        record = await get_channel_model_override(db_session, guild_id, channel_id)
        if record is None:
            raise nested_not_found_error(
                f"Model override with identifier '{channel_id}' not found"
            )
        return ChannelModelOverrideRead.model_validate(record)

    @put(status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def put_model_override(
        self,
        db_session: AsyncSession,
        guild_id: str,
        channel_id: str,
        data: ChannelModelOverrideWrite,
    ) -> ChannelModelOverrideRead:
        """Upsert the channel's model override and return the stored row."""
        record = await upsert_channel_model_override(
            db_session,
            guild_id=guild_id,
            channel_id=channel_id,
            model_key=data.model_key,
            reasoning_level=data.reasoning_level,
            daily_token_budget=data.daily_token_budget,
            hourly_token_budget=data.hourly_token_budget,
        )
        # Serialize before commit to avoid session-detachment issues with the
        # Skrift-injected session (mirrors the legacy router's ordering).
        response = ChannelModelOverrideRead.model_validate(record)
        await db_session.commit()
        return response

    @delete(status_code=HTTP_204_NO_CONTENT, guards=BOT_API_GUARDS)
    async def delete_model_override(
        self,
        db_session: AsyncSession,
        guild_id: str,
        channel_id: str,
    ) -> None:
        """Remove the channel's model override; idempotent (204 whether or not it existed)."""
        await delete_channel_model_override(db_session, guild_id, channel_id)
        await db_session.commit()
