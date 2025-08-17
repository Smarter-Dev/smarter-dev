"""Squad sale event management endpoints for the Smarter Dev API."""

from __future__ import annotations

import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api.dependencies import (
    get_database_session,
    APIKey,
    verify_guild_access,
    get_request_metadata
)
from smarter_dev.web.api.exceptions import (
    create_validation_error,
    create_not_found_error,
    create_conflict_error,
    validate_discord_id
)
from smarter_dev.web.api.schemas import (
    SquadSaleEventCreate,
    SquadSaleEventResponse,
    SquadSaleEventListResponse,
    SuccessResponse
)
from smarter_dev.web.crud import (
    SquadSaleEventOperations,
    DatabaseOperationError,
    NotFoundError,
    ConflictError
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/squad-sale-events", tags=["Squad Sale Events"])


@router.get("/", response_model=List[SquadSaleEventResponse])
async def list_sale_events(
    api_key: APIKey,
    guild_id: str = Depends(verify_guild_access),
    session: AsyncSession = Depends(get_database_session)
) -> List[SquadSaleEventResponse]:
    """List all sale events for a guild."""
    try:
        sale_ops = SquadSaleEventOperations(session)
        events, _ = await sale_ops.get_sale_events_by_guild(guild_id)
        
        # Convert to response format
        responses = []
        for event in events:
            response = SquadSaleEventResponse.model_validate(event)
            response.end_time = event.end_time
            response.is_currently_active = event.is_currently_active
            response.has_started = event.has_started
            response.has_ended = event.has_ended
            response.time_remaining_hours = event.time_remaining_hours
            response.days_until_start = event.days_until_start
            responses.append(response)
        
        return responses
        
    except DatabaseOperationError as e:
        logger.error(f"Database error listing sale events: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    except Exception as e:
        logger.error(f"Unexpected error listing sale events: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/{event_id}", response_model=SquadSaleEventResponse)
async def get_sale_event(
    api_key: APIKey,
    event_id: UUID = Path(..., description="Sale event ID"),
    guild_id: str = Depends(verify_guild_access),
    session: AsyncSession = Depends(get_database_session)
) -> SquadSaleEventResponse:
    """Get a specific sale event by ID."""
    try:
        sale_ops = SquadSaleEventOperations(session)
        event = await sale_ops.get_sale_event_by_id(event_id, guild_id)
        
        if not event:
            raise create_not_found_error("Sale event not found")
        
        response = SquadSaleEventResponse.model_validate(event)
        response.end_time = event.end_time
        response.is_currently_active = event.is_currently_active
        response.has_started = event.has_started
        response.has_ended = event.has_ended
        response.time_remaining_hours = event.time_remaining_hours
        response.days_until_start = event.days_until_start
        
        return response
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting sale event: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    except Exception as e:
        logger.error(f"Unexpected error getting sale event: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")