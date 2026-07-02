"""API router for member message-activity ingestion (bot integration).

The bot reports every human guild message here in batches (one call per flush
interval, not per message). The data feeds the activity facts injected into
handler trigger contexts — see :mod:`smarter_dev.web.member_activity`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_skrift_db_session
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.member_activity import record_activity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/activity", tags=["activity"])


class ActivityEvent(BaseModel):
    guild_id: str
    user_id: str
    message_at: datetime


class ActivityBatchRequest(BaseModel):
    events: list[ActivityEvent] = Field(default_factory=list)


@router.post("/batch", status_code=status.HTTP_200_OK)
async def ingest_activity_batch(
    body: ActivityBatchRequest,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> dict:
    for event in body.events:
        await record_activity(
            session, event.guild_id, event.user_id, event.message_at
        )
    await session.commit()
    return {"recorded": len(body.events)}
