"""Squad management endpoints for the Smarter Dev API.

This module provides REST API endpoints for managing squads including
creation, membership, and squad operations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api.dependencies import (
    get_database_session,
    verify_bot_token,
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
    SquadResponse,
    SquadCreate,
    SquadUpdate,
    SquadMembershipResponse,
    SquadJoinRequest,
    SquadLeaveRequest,
    SquadMembersResponse,
    UserSquadResponse,
    SuccessResponse
)
from smarter_dev.web.crud import (
    SquadOperations,
    DatabaseOperationError,
    NotFoundError,
    ConflictError
)

router = APIRouter()


@router.get("/", response_model=List[SquadResponse])
async def list_squads(
    include_inactive: bool = Query(False, description="Include inactive squads"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> List[SquadResponse]:
    """List all squads in a guild.
    
    Returns all squads for the guild, optionally including inactive ones.
    Each squad includes current member count and configuration details.
    """
    squad_ops = SquadOperations()
    squads = await squad_ops.get_guild_squads(db, guild_id, active_only=not include_inactive)
    
    # Convert to response models and add member counts
    squad_responses = []
    for squad in squads:
        member_count = await squad_ops._get_squad_member_count(db, squad.id)
        squad_data = squad.__dict__.copy()
        squad_data['member_count'] = member_count
        squad_responses.append(SquadResponse.model_validate(squad_data))
    
    return squad_responses


@router.post("/", response_model=SquadResponse)
async def create_squad(
    squad: SquadCreate,
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> SquadResponse:
    """Create a new squad."""
    try:
        squad_ops = SquadOperations()
        created_squad = await squad_ops.create_squad(
            db,
            guild_id,
            squad.role_id,
            squad.name,
            description=squad.description,
            max_members=squad.max_members,
            switch_cost=squad.switch_cost
        )
        
        await db.commit()
        
        # Add member count (will be 0 for new squad)
        squad_data = created_squad.__dict__.copy()
        squad_data['member_count'] = 0
        
        return SquadResponse.model_validate(squad_data)
    except ConflictError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DatabaseOperationError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.get("/{squad_id}", response_model=SquadResponse)
async def get_squad(
    squad_id: UUID = Path(..., description="Squad UUID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> SquadResponse:
    """Get squad by ID."""
    try:
        squad_ops = SquadOperations()
        squad = await squad_ops.get_squad(db, squad_id)
        
        # Verify squad belongs to the guild
        if squad.guild_id != guild_id:
            raise HTTPException(status_code=404, detail="Squad not found in this guild")
        
        # Get member count
        member_count = await squad_ops._get_squad_member_count(db, squad_id)
        squad_data = squad.__dict__.copy()
        squad_data['member_count'] = member_count
        
        return SquadResponse.model_validate(squad_data)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DatabaseOperationError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.put("/{squad_id}", response_model=SquadResponse)
async def update_squad(
    squad_update: SquadUpdate,
    squad_id: UUID = Path(..., description="Squad UUID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> SquadResponse:
    """Update squad configuration."""
    try:
        squad_ops = SquadOperations()
        squad = await squad_ops.get_squad(db, squad_id)
        
        if squad.guild_id != guild_id:
            raise HTTPException(status_code=404, detail="Squad not found in this guild")
        
        # Get update data as dict, excluding None values
        update_data = squad_update.model_dump(exclude_unset=True, exclude_none=True)
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No valid squad updates provided")
        
        # Apply updates
        for field, value in update_data.items():
            if hasattr(squad, field):
                setattr(squad, field, value)
        
        await db.commit()
        
        # Get member count
        member_count = await squad_ops._get_squad_member_count(db, squad_id)
        squad_data = squad.__dict__.copy()
        squad_data['member_count'] = member_count
        
        return SquadResponse.model_validate(squad_data)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DatabaseOperationError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.post("/{squad_id}/join", response_model=SquadMembershipResponse)
async def join_squad(
    join_request: SquadJoinRequest,
    squad_id: UUID = Path(..., description="Squad UUID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> SquadMembershipResponse:
    """Join a squad."""
    try:
        squad_ops = SquadOperations()
        membership = await squad_ops.join_squad(db, guild_id, join_request.user_id, squad_id)
        await db.commit()
        
        # Get squad information for response
        squad = await squad_ops.get_squad(db, squad_id)
        member_count = await squad_ops._get_squad_member_count(db, squad_id)
        
        squad_data = squad.__dict__.copy()
        squad_data['member_count'] = member_count
        
        return SquadMembershipResponse(
            squad_id=membership.squad_id,
            user_id=membership.user_id,
            guild_id=membership.guild_id,
            joined_at=membership.joined_at,
            squad=SquadResponse.model_validate(squad_data)
        )
    except ConflictError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DatabaseOperationError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.delete("/leave", response_model=SuccessResponse)
async def leave_squad(
    leave_request: SquadLeaveRequest,
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> SuccessResponse:
    """Leave current squad."""
    try:
        squad_ops = SquadOperations()
        await squad_ops.leave_squad(db, guild_id, leave_request.user_id)
        await db.commit()
        
        return SuccessResponse(
            message=f"User {leave_request.user_id} left their squad",
            timestamp=datetime.now(timezone.utc)
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DatabaseOperationError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.get("/members/{user_id}", response_model=UserSquadResponse)
async def get_user_squad(
    user_id: str = Path(..., description="Discord user ID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> UserSquadResponse:
    """Get user's current squad."""
    try:
        # Validate user ID format
        validate_discord_id(user_id, "user ID")
        
        squad_ops = SquadOperations()
        squad = await squad_ops.get_user_squad(db, guild_id, user_id)
        
        if squad is None:
            return UserSquadResponse(
                user_id=user_id,
                guild_id=guild_id,
                squad=None,
                membership=None
            )
        
        # Get membership details
        from smarter_dev.web.models import SquadMembership
        from sqlalchemy import select
        
        stmt = select(SquadMembership).where(
            SquadMembership.guild_id == guild_id,
            SquadMembership.user_id == user_id,
            SquadMembership.squad_id == squad.id
        )
        result = await db.execute(stmt)
        membership = result.scalar_one()
        
        # Get member count
        member_count = await squad_ops._get_squad_member_count(db, squad.id)
        squad_data = squad.__dict__.copy()
        squad_data['member_count'] = member_count
        
        return UserSquadResponse(
            user_id=user_id,
            guild_id=guild_id,
            squad=SquadResponse.model_validate(squad_data),
            membership=SquadMembershipResponse(
                squad_id=membership.squad_id,
                user_id=membership.user_id,
                guild_id=membership.guild_id,
                joined_at=membership.joined_at,
                squad=SquadResponse.model_validate(squad_data)
            )
        )
    except DatabaseOperationError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.get("/{squad_id}/members", response_model=SquadMembersResponse)
async def get_squad_members(
    squad_id: UUID = Path(..., description="Squad UUID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> SquadMembersResponse:
    """Get all members of a squad."""
    try:
        squad_ops = SquadOperations()
        squad = await squad_ops.get_squad(db, squad_id)
        
        if squad.guild_id != guild_id:
            raise HTTPException(status_code=404, detail="Squad not found in this guild")
        
        # Get squad members
        memberships = await squad_ops.get_squad_members(db, squad_id)
        member_count = len(memberships)
        
        squad_data = squad.__dict__.copy()
        squad_data['member_count'] = member_count
        squad_response = SquadResponse.model_validate(squad_data)
        
        # Convert memberships to response models
        member_responses = [
            SquadMembershipResponse(
                squad_id=membership.squad_id,
                user_id=membership.user_id,
                guild_id=membership.guild_id,
                joined_at=membership.joined_at,
                squad=squad_response
            )
            for membership in memberships
        ]
        
        return SquadMembersResponse(
            squad=squad_response,
            members=member_responses,
            total_members=member_count
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DatabaseOperationError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")