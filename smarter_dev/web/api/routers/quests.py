"""Quests API router for Discord bot integration."""

"""Quests API router for Discord bot integration."""

from __future__ import annotations

import logging
from typing import Dict, Any
from uuid import UUID
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from smarter_dev.shared.date_provider import get_date_provider

from smarter_dev.shared.database import get_db_session
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.crud import (
    QuestOperations,
    DatabaseOperationError,
)
from smarter_dev.web.models import Campaign, DailyQuest, Quest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quests", tags=["quests"])

# TODO THIS IS JSUT A ROUTER


@router.get("/daily/current")
async def get_current_daily_quest(
    guild_id: str = Query(..., description="Discord guild ID"),
    session: AsyncSession = Depends(get_db_session),
    api_key=Depends(verify_api_key),
) -> Dict[str, Dict[str, Any]]:
    date_provider = get_date_provider()
    today = date_provider.today()

    try:
        quest_ops = QuestOperations(session)

        daily = await quest_ops.get_daily_quest(
            active_date=today,
            guild_id=guild_id,
        )

        if not daily or not daily.quest or not daily.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No active daily quest",
            )

        quest_data = {
            "id": str(daily.id),  # daily quest ID
            "title": quest.title,
            "prompt": quest.prompt,
            "quest_type": quest.quest_type,
            "active_date": daily.active_date.isoformat(),
            "expires_at": daily.expires_at.isoformat(),
            "hint": "Once you're ready, submit with /daily submit",
        }

        return {"quest": quest_data}

    except DatabaseOperationError as e:
        logger.error(f"Database error getting daily quest: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve daily quest",
        ) from e


from __future__ import annotations

import logging
from typing import Dict, Any
from uuid import UUID
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from smarter_dev.shared.date_provider import get_date_provider

from smarter_dev.shared.database import get_db_session
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.crud import (
    QuestOperations,
    DatabaseOperationError,
)
from smarter_dev.web.models import Campaign, DailyQuest, Quest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quests", tags=["quests"])


@router.get("/daily/current")
async def get_current_daily_quest(
    guild_id: str = Query(..., description="Discord guild ID"),
    session: AsyncSession = Depends(get_db_session),
    api_key=Depends(verify_api_key),
) -> Dict[str, Dict[str, Any]]:
    date_provider = get_date_provider()
    today = date_provider.today()

    try:
        quest_ops = QuestOperations(session)

        daily = await quest_ops.get_daily_quest(
            active_date=today,
            guild_id=guild_id,
        )

        if not daily or not daily.quest or not daily.is_active:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="No active daily quest",
            )

        quest = daily.quest

        quest_data = {
            "id": str(daily.id),  # daily quest ID
            "title": quest.title,
            "prompt": quest.prompt,
            "quest_type": quest.quest_type,
            "active_date": daily.active_date.isoformat(),
            "expires_at": daily.expires_at.isoformat(),
            "hint": "Once you're ready, submit with /daily submit",
        }

        return {"quest": quest_data}

    except DatabaseOperationError as e:
        logger.error(f"Database error getting daily quest: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve daily quest",
        ) from e
