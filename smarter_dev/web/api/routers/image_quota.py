"""API router for the chat agent's per-guild image-generation quota.

The bot has no direct data store, so its image budget lives in Redis behind
these endpoints (see :mod:`smarter_dev.web.image_quota`):

- ``GET  /image-generations/quota`` read the remaining budget (no spend) — the
  bot injects this into the agent's per-turn prompt.
- ``POST /image-generations/reserve`` spend one slot before generating.
- ``POST /image-generations/release`` refund a reserved slot when generation
  then fails.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.image_quota import ImageQuotaLimiter, ImageQuotaStatus

router = APIRouter(prefix="/image-generations", tags=["image-generations"])


class QuotaStatusResponse(BaseModel):
    guild_id: str
    limit: int
    remaining: int
    resets_at: str | None = None
    retry_after_seconds: int | None = None
    granted: bool = True


class GuildBody(BaseModel):
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


@router.get("/quota", response_model=QuotaStatusResponse)
async def get_quota(
    guild_id: str,
    _: Any = Depends(verify_api_key),
) -> QuotaStatusResponse:
    limiter = ImageQuotaLimiter(redis=get_redis_client())
    return _to_response(guild_id, await limiter.peek(guild_id))


@router.post("/reserve", response_model=QuotaStatusResponse)
async def reserve_slot(
    body: GuildBody,
    _: Any = Depends(verify_api_key),
) -> QuotaStatusResponse:
    limiter = ImageQuotaLimiter(redis=get_redis_client())
    return _to_response(body.guild_id, await limiter.reserve(body.guild_id))


@router.post("/release", status_code=200)
async def release_slot(
    body: GuildBody,
    _: Any = Depends(verify_api_key),
) -> dict:
    limiter = ImageQuotaLimiter(redis=get_redis_client())
    await limiter.release(body.guild_id)
    return {"released": body.guild_id}
