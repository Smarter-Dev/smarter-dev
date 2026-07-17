"""Native Litestar port of the image-generation quota bot API.

Ports the legacy FastAPI ``routers/image_quota.py`` (prefix
``/image-generations``) — part of unit U8 in
docs/v2/legacy-sunset/04-api-rewrite.md. Preserves the exact paths, verbs,
status codes, and request/response shapes of the FastAPI implementation so the
bot's chat agent (``smarter_dev/bot/agents/chat_tools.py`` and
``services/chat_engine.py``) needs zero changes:

- ``GET  /api/image-generations/quota?guild_id=...`` → remaining budget (no spend).
- ``POST /api/image-generations/reserve`` (body ``{"guild_id"}``) → spend one slot.
- ``POST /api/image-generations/release`` (body ``{"guild_id"}``) → refund a slot.

The quota lives in Redis (see :mod:`smarter_dev.web.image_quota`); these
endpoints take no DB session. 

Status-code parity note: FastAPI defaults every verb (including ``POST``) to
200, so the reserve/release handlers declare ``HTTP_200_OK`` rather than
Litestar's default 201. The ``release`` response is the literal
``{"released": guild_id}`` shape the legacy router returned.
"""

from __future__ import annotations

from litestar import Controller, get, post
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.image_quota import ImageQuotaLimiter, ImageQuotaStatus

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


class QuotaStatusResponse(BaseModel):
    """Response for a guild's image budget (parity with the FastAPI router)."""

    guild_id: str
    limit: int
    remaining: int
    resets_at: str | None = None
    retry_after_seconds: int | None = None
    granted: bool = True


class GuildBody(BaseModel):
    """Request body carrying the target guild id."""

    guild_id: str


def _to_response(guild_id: str, status: ImageQuotaStatus) -> QuotaStatusResponse:
    return QuotaStatusResponse(
        guild_id=guild_id,
        limit=status.limit,
        remaining=status.remaining,
        # Minute-precision UTC, matching the bot's other timestamp rendering.
        resets_at=(
            status.resets_at.strftime("%Y-%m-%dT%H:%MZ")
            if status.resets_at is not None
            else None
        ),
        retry_after_seconds=status.retry_after_seconds,
        granted=status.granted,
    )


class ImageQuotaController(Controller):
    """Per-guild hourly image-generation quota — peek, reserve, release."""

    path = "/api/image-generations"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("/quota", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_quota(self, guild_id: str) -> QuotaStatusResponse:
        """Read the remaining budget without spending any of it."""
        limiter = ImageQuotaLimiter(redis=get_redis_client())
        return _to_response(guild_id, await limiter.peek(guild_id))

    @post("/reserve", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def reserve_slot(self, data: GuildBody) -> QuotaStatusResponse:
        """Spend one slot before generating an image."""
        limiter = ImageQuotaLimiter(redis=get_redis_client())
        return _to_response(data.guild_id, await limiter.reserve(data.guild_id))

    @post("/release", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def release_slot(self, data: GuildBody) -> dict[str, str]:
        """Refund a reserved slot when a generation then fails."""
        limiter = ImageQuotaLimiter(redis=get_redis_client())
        await limiter.release(data.guild_id)
        return {"released": data.guild_id}
