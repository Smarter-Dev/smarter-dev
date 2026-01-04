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
    DatabaseOperationError, SquadOperations, QuestInputOperations, ScriptExecutionError, QuestSubmissionOperations,
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

class DailyQuestSubmitBody(BaseModel):
    guild_id: str
    user_id: str
    submitted_solution: str


@router.post("/{daily_quest_id}/submit")
async def submit_daily_quest(
    daily_quest_id: UUID,
    body: DailyQuestSubmitBody,
    session: AsyncSession = Depends(get_db_session),
    api_key=Depends(verify_api_key),
) -> dict[str, Any]:
    guild_id = body.guild_id

    user_id = body.user_id
    submitted_solution = body.submitted_solution

    squad_ops = SquadOperations()
    user_squad = await squad_ops.get_user_squad(session, guild_id, user_id)
    if not user_squad:
        raise HTTPException(404, "User is not a member of any squad")

    quest_ops = QuestOperations(session)
    daily_quest = await quest_ops.get_daily_quest_by_id(daily_quest_id, guild_id)
    if not daily_quest:
        raise HTTPException(404, "Daily quest not found")
    if not daily_quest.is_active:
        raise HTTPException(403, "Daily quest is not active")

    submission_ops = QuestSubmissionOperations(session)
    is_correct, is_first_success, points = await submission_ops.submit_solution(
        daily_quest_id=daily_quest_id,
        guild_id=guild_id,
        squad_id=user_squad.id,
        user_id=user_id,
        username=user_id,  # replace later if you want
        submitted_solution=submitted_solution,
    )

    return {
        "is_correct": is_correct,
        "is_first_success": is_first_success,
        "points_earned": points,
    }

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

        # Get or generate shared input for the daily quest
        input_ops = QuestInputOperations(session)

        # If no generator script, return empty/static input instead of error
        if not quest.input_generator_script:
            input_data = "No input required for this quest."
            result_data = ""
        else:
            try:
                input_data, result_data = await input_ops.get_or_create_input(
                    daily_quest_id=daily_quest.id,
                    script=quest.input_generator_script,
                )
            except ScriptExecutionError as e:
                raise HTTPException(
                    status_code=500,
                    detail="Failed to generate quest input",
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
        api_key=Depends(verify_api_key)
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

@router.get("/upcoming-announcements")
async def get_upcoming_quest_announcements(
    seconds: int = Query(default=45),
    session: AsyncSession = Depends(get_db_session),
    api_key=Depends(verify_api_key),
) -> Dict[str, list[dict[str, Any]]]:
    try:
        quest_ops = QuestOperations(session)

        now = datetime.now(timezone.utc)
        window_end = now + timedelta(seconds=seconds)

        quests = await quest_ops.get_upcoming_daily_quests(
            window_end=window_end
        )

        quest_list = []
        for dq in quests:
            quest_list.append({
                "id": str(dq.id),                 # daily_quest_id
                "quest_id": str(dq.quest.id),
                "title": dq.quest.title,
                "description": dq.quest.prompt,
                "guild_id": dq.guild_id,
                "release_time": dq.active_date.isoformat(),
            })

        return {"quests": quest_list}

    except DatabaseOperationError as e:
        logger.error(f"Failed to get upcoming quest announcements: {e}")
        raise HTTPException(500, "Failed to retrieve upcoming quests")

@router.post("/{daily_quest_id}/mark-announced")
async def mark_daily_quest_announced(
    daily_quest_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    api_key=Depends(verify_api_key),
) -> Dict[str, bool]:
    try:
        quest_ops = QuestOperations(session)
        success = await quest_ops.mark_daily_quest_announced(daily_quest_id)

        if not success:
            raise HTTPException(404, "Daily quest not found")

        return {"success": True}

    except DatabaseOperationError as e:
        logger.error(f"Failed to mark quest announced: {e}")
        raise HTTPException(500, "Failed to mark quest announced")

@router.post("/{daily_quest_id}/mark-active")
async def mark_daily_quest_active(
    daily_quest_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    api_key=Depends(verify_api_key),
) -> Dict[str, bool]:
    try:
        quest_ops = QuestOperations(session)
        success = await quest_ops.mark_daily_quest_active(daily_quest_id)

        if not success:
            raise HTTPException(404, "Daily quest not found")

        return {"success": True}

    except DatabaseOperationError as e:
        logger.error(f"Failed to activate daily quest: {e}")
        raise HTTPException(500, "Failed to activate daily quest")

