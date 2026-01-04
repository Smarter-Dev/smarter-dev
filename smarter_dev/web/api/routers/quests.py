from __future__ import annotations

import logging
from typing import Dict, Any, Optional
from uuid import UUID
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared import date_provider
from smarter_dev.shared.date_provider import get_date_provider

from smarter_dev.shared.database import get_db_session
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.crud import (
    QuestOperations,
    DatabaseOperationError, SquadOperations, QuestInputOperations, ScriptExecutionError,
)
from smarter_dev.web.models import Campaign, DailyQuest, Quest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quests", tags=["quests"])


@router.get("/daily/current")
async def get_current_daily_quest(
    guild_id: str = Query(..., description="Discord guild ID"),
    session: AsyncSession = Depends(get_db_session),
    api_key=Depends(verify_api_key),
) -> Dict[str, Dict[str, Any] | str | None]:
    date_provider = get_date_provider()
    today = date_provider.today()

    logger.info("Quests router hit")

    try:
        quest_ops = QuestOperations(session)

        daily = await quest_ops.get_daily_quest(
            active_date=today,
            guild_id=guild_id,
        )

        logger.info("Awaited daily quest")

        if not daily or not daily.quest or not daily.is_active:
            return {"quest": None, "message": "No daily quest available yet"}

        quest_data = {
            "id": str(daily.id),  # daily quest ID
            "title": daily.quest.title,
            "prompt": daily.quest.prompt,
            "quest_type": daily.quest.quest_type,
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

@router.get("/{daily_quest_id}/input")
async def get_daily_quest_input(
    daily_quest_id: UUID,
    guild_id: str = Query(..., description="Discord guild ID"),
    user_id: str = Query(..., description="Discord user ID"),
    session: AsyncSession = Depends(get_db_session),
    api_key=Depends(verify_api_key),
) -> Dict[str, Any]:
    """Get daily quest input data.

    Gets existing input data if available, or generates new input by executing
    the quest's input generator script. All users receive the same input.
    """
    try:
        # Get user's squad (still required for participation context)
        squad_ops = SquadOperations()
        user_squad = await squad_ops.get_user_squad(session, guild_id, user_id)

        if not user_squad:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User is not a member of any squad",
            )

        # Get the daily quest (joined with Quest)
        quest_ops = QuestOperations(session)
        daily_quest = await quest_ops.get_daily_quest_by_id(daily_quest_id, guild_id)

        if not daily_quest:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Daily quest not found",
            )

        # Verify guild ownership
        if daily_quest.guild_id != guild_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Quest does not belong to the specified guild",
            )

        # Check quest activity
        if not daily_quest.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This quest is no longer / not yet active",
            )

        quest = daily_quest.quest

        # Check if the quest has an input generator script
        if not quest.input_generator_script:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This quest does not have input generation configured yet. Please contact an administrator.",
            )

        # Get or generate shared input for the daily quest
        input_ops = QuestInputOperations(session)

        try:
            input_data, _ = await input_ops.get_or_create_input(
                daily_quest_id=daily_quest.id,
                script=quest.input_generator_script,
            )
        except ScriptExecutionError as e:
            logger.error(f"Script execution error for daily quest {daily_quest_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate quest input due to script execution error",
            )

        logger.info(
            f"Provided daily quest input for user {user_id} (quest={daily_quest_id})"
        )

        return {
            "input_data": input_data,
            "quest": {
                "id": str(quest.id),
                "title": quest.title,
                "prompt": quest.prompt,
                "type": quest.quest_type,
            },
            "daily_quest": {
                "id": str(daily_quest.id),
                "active_date": str(daily_quest.active_date),
                "expires_at": daily_quest.expires_at.isoformat(),
            },
            "squad": {
                "id": str(user_squad.id),
                "name": user_squad.name,
            },
            "metadata": {
                "has_existing_input": True,
            },
        }

    except DatabaseOperationError as e:
        logger.error(f"Database error getting daily quest input: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve quest input",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting daily quest input: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )






@router.get("/detailed-scoreboard")
async def get_detailed_scoreboard(
        guild_id: str = Query(..., description="Discord guild ID"),
        session: AsyncSession = Depends(get_db_session),
        api_key=Depends(get_db_session)
) -> Dict[str, Any]:
    try:
        quest_ops : QuestOperations = QuestOperations(session)
        today = date_provider.today()
        daily = await quest_ops.get_daily_quest(today, guild_id)

        if not daily :
            return {
                "quest": None,
                "detailed_scoreboard": [],
                "total_submissions": 0,
                "total_challenges": 0
            }

    except DatabaseOperationError as e:
        logger.error(f"Database error getting detailed scoreboard: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve detailed scoreboard data",
        )
    except Exception as e:
        logger.error(f"Unexpected error getting detailed scoreboard: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
