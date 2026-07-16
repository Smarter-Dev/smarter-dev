"""API router for per-channel LLM model overrides (bot integration).

The admin ``/model`` slash command sets a model + token budgets for a single
channel; the bot has no DB access, so it reads/writes the override through these
endpoints (one row per channel, PUT is an upsert):

- ``GET    /guilds/{guild_id}/channels/{channel_id}/model-override`` → the row or 404.
- ``PUT    /guilds/{guild_id}/channels/{channel_id}/model-override`` → upsert, 200.
- ``DELETE /guilds/{guild_id}/channels/{channel_id}/model-override`` → 204 (idempotent).

All endpoints sit behind ``verify_api_key`` (bot bearer token). Budgets are stored
verbatim (``0`` = unlimited); enforcement happens in the chat runtime, not here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api.dependencies import APIKey, get_database_session
from smarter_dev.web.api.exceptions import create_not_found_error
from smarter_dev.web.api.schemas import (
    ChannelModelOverrideRead,
    ChannelModelOverrideWrite,
)
from smarter_dev.web.crud import (
    delete_channel_model_override,
    get_channel_model_override,
    upsert_channel_model_override,
)

router = APIRouter()


@router.get(
    "/guilds/{guild_id}/channels/{channel_id}/model-override",
    response_model=ChannelModelOverrideRead,
)
async def get_model_override(
    guild_id: str,
    channel_id: str,
    api_key: APIKey,
    db: AsyncSession = Depends(get_database_session),
) -> ChannelModelOverrideRead:
    """Return the channel's model override, or 404 if none is set."""
    record = await get_channel_model_override(db, guild_id, channel_id)
    if record is None:
        raise create_not_found_error("Model override", channel_id)
    return ChannelModelOverrideRead.model_validate(record)


@router.put(
    "/guilds/{guild_id}/channels/{channel_id}/model-override",
    response_model=ChannelModelOverrideRead,
)
async def put_model_override(
    guild_id: str,
    channel_id: str,
    body: ChannelModelOverrideWrite,
    api_key: APIKey,
    db: AsyncSession = Depends(get_database_session),
) -> ChannelModelOverrideRead:
    """Upsert the channel's model override and return the stored row."""
    record = await upsert_channel_model_override(
        db,
        guild_id=guild_id,
        channel_id=channel_id,
        model_key=body.model_key,
        reasoning_level=body.reasoning_level,
        daily_token_budget=body.daily_token_budget,
        hourly_token_budget=body.hourly_token_budget,
    )
    response = ChannelModelOverrideRead.model_validate(record)
    await db.commit()
    return response


@router.delete(
    "/guilds/{guild_id}/channels/{channel_id}/model-override",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_model_override(
    guild_id: str,
    channel_id: str,
    api_key: APIKey,
    db: AsyncSession = Depends(get_database_session),
) -> Response:
    """Remove the channel's model override; idempotent (204 whether or not it existed)."""
    await delete_channel_model_override(db, guild_id, channel_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
