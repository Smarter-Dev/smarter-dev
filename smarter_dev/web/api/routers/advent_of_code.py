"""Advent of Code API router for Discord bot integration.

Provides endpoints for Advent of Code configuration and thread tracking.
Used by the Discord bot to check configurations and record posted threads.
"""

from __future__ import annotations

import logging
from typing import List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from smarter_dev.shared.database import get_db_session
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.crud import AdventOfCodeConfigOperations, DatabaseOperationError, ConflictError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/advent-of-code", tags=["advent-of-code"])


class RecordThreadRequest(BaseModel):
    """Request model for recording a posted thread."""
    year: int = Field(..., description="Advent of Code year")
    day: int = Field(..., ge=1, le=25, description="Day of the challenge (1-25)")
    thread_id: str = Field(..., description="Discord thread ID")
    thread_title: str = Field(..., description="Thread title")


class ThreadResponse(BaseModel):
    """Response model for thread data."""
    id: str
    guild_id: str
    year: int
    day: int
    thread_id: str
    thread_title: str
    created_at: str


class ConfigResponse(BaseModel):
    """Response model for AoC configuration data."""
    guild_id: str
    forum_channel_id: str | None
    is_active: bool


@router.get("/active-configs")
async def get_active_configs(
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, List[Dict[str, Any]]]:
    """Get all active Advent of Code configurations.

    Used by the Discord bot to retrieve guilds that have AoC threads enabled.

    Returns:
        Dictionary with list of active configuration data
    """
    try:
        aoc_ops = AdventOfCodeConfigOperations()
        configs = await aoc_ops.get_active_configs(session)

        # Format configs for bot consumption
        config_list = []
        for config in configs:
            config_data = {
                "guild_id": config.guild_id,
                "forum_channel_id": config.forum_channel_id,
                "is_active": config.is_active,
            }
            config_list.append(config_data)

        logger.debug(f"Retrieved {len(config_list)} active AoC configurations")

        return {"configs": config_list}

    except DatabaseOperationError as e:
        logger.error(f"Database error getting active AoC configs: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve active configurations"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting active AoC configs: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/{guild_id}/config")
async def get_guild_config(
    guild_id: str,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> ConfigResponse:
    """Get Advent of Code configuration for a guild.

    Args:
        guild_id: Discord guild ID

    Returns:
        Configuration data
    """
    try:
        aoc_ops = AdventOfCodeConfigOperations()
        config = await aoc_ops.get_or_create_config(session, guild_id)

        return ConfigResponse(
            guild_id=config.guild_id,
            forum_channel_id=config.forum_channel_id,
            is_active=config.is_active,
        )

    except DatabaseOperationError as e:
        logger.error(f"Database error getting guild AoC config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve configuration"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting guild AoC config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/{guild_id}/threads/{year}/{day}")
async def get_posted_thread(
    guild_id: str,
    year: int,
    day: int,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, Any]:
    """Check if a thread has been posted for a specific day.

    Args:
        guild_id: Discord guild ID
        year: Advent of Code year
        day: Day of the challenge (1-25)

    Returns:
        Thread data if exists, null otherwise
    """
    try:
        aoc_ops = AdventOfCodeConfigOperations()
        thread = await aoc_ops.get_posted_thread(session, guild_id, year, day)

        if thread is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Thread not found"
            )

        return {
            "thread": {
                "id": str(thread.id),
                "guild_id": thread.guild_id,
                "year": thread.year,
                "day": thread.day,
                "thread_id": thread.thread_id,
                "thread_title": thread.thread_title,
                "created_at": thread.created_at.isoformat()
            }
        }

    except HTTPException:
        raise
    except DatabaseOperationError as e:
        logger.error(f"Database error checking posted thread: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to check posted thread"
        )
    except Exception as e:
        logger.error(f"Unexpected error checking posted thread: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.post("/{guild_id}/threads")
async def record_posted_thread(
    guild_id: str,
    request: RecordThreadRequest,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, Any]:
    """Record that a thread has been posted for a specific day.

    Args:
        guild_id: Discord guild ID
        request: Thread recording data

    Returns:
        Created thread record
    """
    try:
        aoc_ops = AdventOfCodeConfigOperations()
        thread = await aoc_ops.record_posted_thread(
            session,
            guild_id=guild_id,
            year=request.year,
            day=request.day,
            thread_id=request.thread_id,
            thread_title=request.thread_title
        )
        await session.commit()

        logger.info(f"Recorded AoC thread for guild {guild_id}, year {request.year}, day {request.day}")

        return {
            "success": True,
            "thread": {
                "id": str(thread.id),
                "guild_id": thread.guild_id,
                "year": thread.year,
                "day": thread.day,
                "thread_id": thread.thread_id,
                "thread_title": thread.thread_title,
                "created_at": thread.created_at.isoformat()
            }
        }

    except ConflictError as e:
        logger.warning(f"Thread already recorded: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Thread already recorded for this day"
        )
    except DatabaseOperationError as e:
        logger.error(f"Database error recording posted thread: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to record posted thread"
        )
    except Exception as e:
        logger.error(f"Unexpected error recording posted thread: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/{guild_id}/threads")
async def get_guild_threads(
    guild_id: str,
    year: int | None = None,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, List[ThreadResponse]]:
    """Get all posted threads for a guild.

    Args:
        guild_id: Discord guild ID
        year: Optional year filter

    Returns:
        List of thread records
    """
    try:
        aoc_ops = AdventOfCodeConfigOperations()
        threads = await aoc_ops.get_guild_threads(session, guild_id, year)

        thread_responses = [
            ThreadResponse(
                id=str(thread.id),
                guild_id=thread.guild_id,
                year=thread.year,
                day=thread.day,
                thread_id=thread.thread_id,
                thread_title=thread.thread_title,
                created_at=thread.created_at.isoformat()
            )
            for thread in threads
        ]

        return {"threads": thread_responses}

    except DatabaseOperationError as e:
        logger.error(f"Database error getting guild threads: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve guild threads"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting guild threads: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )
