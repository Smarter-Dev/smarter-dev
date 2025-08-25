"""Repeating Message API router for Discord bot integration.

Provides endpoints for repeating message management and sending automation.
Used by the Discord bot to check for due repeating messages and mark them as sent.
Also provides admin endpoints for web interface management.
"""

from __future__ import annotations

import logging
from typing import List, Dict, Any
from uuid import UUID
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from smarter_dev.shared.database import get_db_session
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.crud import RepeatingMessageOperations, DatabaseOperationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repeating-messages", tags=["repeating-messages"])


class CreateRepeatingMessageRequest(BaseModel):
    """Request model for creating a repeating message."""
    guild_id: str = Field(..., description="Discord guild ID")
    channel_id: str = Field(..., description="Discord channel ID")
    message_content: str = Field(..., description="Message content to send")
    role_id: str | None = Field(None, description="Optional role ID to mention")
    start_time: datetime = Field(..., description="UTC datetime when first message is sent")
    interval_minutes: int = Field(..., ge=1, description="Minutes between messages")
    created_by: str = Field(..., description="Username of creator")


class UpdateRepeatingMessageRequest(BaseModel):
    """Request model for updating a repeating message."""
    message_content: str | None = Field(None, description="Message content to send")
    role_id: str | None = Field(None, description="Role ID to mention (null to remove)")
    start_time: datetime | None = Field(None, description="UTC datetime when first message is sent")
    interval_minutes: int | None = Field(None, ge=1, description="Minutes between messages")
    is_active: bool | None = Field(None, description="Whether message is active")


class RepeatingMessageResponse(BaseModel):
    """Response model for repeating message data."""
    id: str
    guild_id: str
    channel_id: str
    message_content: str
    role_id: str | None
    start_time: datetime
    interval_minutes: int
    next_send_time: datetime
    is_active: bool
    total_sent: int
    last_sent_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime


@router.get("/due")
async def get_due_repeating_messages(
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, List[Dict[str, Any]]]:
    """Get repeating messages that are due to be sent.
    
    Used by the Discord bot to retrieve messages that need to be sent.
    
    Returns:
        Dictionary with list of repeating message data for bot sending
    """
    try:
        message_ops = RepeatingMessageOperations(session)
        due_messages = await message_ops.get_due_repeating_messages()
        
        # Format messages for bot consumption
        message_list = []
        for message in due_messages:
            message_data = {
                "id": str(message.id),
                "guild_id": message.guild_id,
                "channel_id": message.channel_id,
                "message_content": message.get_formatted_message(),
                "role_id": message.role_id,
                "interval_minutes": message.interval_minutes,
                "next_send_time": message.next_send_time.isoformat(),
                "total_sent": message.total_sent
            }
            message_list.append(message_data)
        
        logger.debug(f"Retrieved {len(message_list)} due repeating messages")
        
        return {"repeating_messages": message_list}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting due repeating messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve due repeating messages"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting due repeating messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.post("/{message_id}/mark-sent")
async def mark_repeating_message_sent(
    message_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, bool]:
    """Mark a repeating message as sent and update next send time.
    
    Args:
        message_id: UUID of the repeating message to mark as sent
        
    Returns:
        Success status
    """
    try:
        message_ops = RepeatingMessageOperations(session)
        success = await message_ops.mark_message_sent(message_id)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Repeating message not found"
            )
        
        logger.info(f"Marked repeating message {message_id} as sent")
        
        return {"success": True}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error marking repeating message as sent: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark repeating message as sent"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error marking repeating message as sent: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.post("/")
async def create_repeating_message(
    request: CreateRepeatingMessageRequest,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> RepeatingMessageResponse:
    """Create a new repeating message.
    
    Args:
        request: Repeating message creation data
        
    Returns:
        Created repeating message data
    """
    try:
        message_ops = RepeatingMessageOperations(session)
        message = await message_ops.create_repeating_message(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            message_content=request.message_content,
            start_time=request.start_time,
            interval_minutes=request.interval_minutes,
            created_by=request.created_by,
            role_id=request.role_id
        )
        
        return RepeatingMessageResponse(
            id=str(message.id),
            guild_id=message.guild_id,
            channel_id=message.channel_id,
            message_content=message.message_content,
            role_id=message.role_id,
            start_time=message.start_time,
            interval_minutes=message.interval_minutes,
            next_send_time=message.next_send_time,
            is_active=message.is_active,
            total_sent=message.total_sent,
            last_sent_at=message.last_sent_at,
            created_by=message.created_by,
            created_at=message.created_at,
            updated_at=message.updated_at
        )
        
    except DatabaseOperationError as e:
        logger.error(f"Database error creating repeating message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create repeating message"
        )
    except Exception as e:
        logger.error(f"Unexpected error creating repeating message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/guild/{guild_id}")
async def get_guild_repeating_messages(
    guild_id: str,
    active_only: bool = False,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, List[RepeatingMessageResponse]]:
    """Get all repeating messages for a guild.
    
    Args:
        guild_id: Discord guild ID
        active_only: If true, only return active messages
        
    Returns:
        List of repeating messages for the guild
    """
    try:
        message_ops = RepeatingMessageOperations(session)
        messages = await message_ops.get_guild_repeating_messages(
            guild_id=guild_id,
            active_only=active_only
        )
        
        message_responses = [
            RepeatingMessageResponse(
                id=str(message.id),
                guild_id=message.guild_id,
                channel_id=message.channel_id,
                message_content=message.message_content,
                role_id=message.role_id,
                start_time=message.start_time,
                interval_minutes=message.interval_minutes,
                next_send_time=message.next_send_time,
                is_active=message.is_active,
                total_sent=message.total_sent,
                last_sent_at=message.last_sent_at,
                created_by=message.created_by,
                created_at=message.created_at,
                updated_at=message.updated_at
            )
            for message in messages
        ]
        
        return {"repeating_messages": message_responses}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting guild repeating messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve guild repeating messages"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting guild repeating messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/{message_id}")
async def get_repeating_message(
    message_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> RepeatingMessageResponse:
    """Get a repeating message by ID.
    
    Args:
        message_id: UUID of the repeating message to retrieve
        
    Returns:
        Repeating message data
    """
    try:
        message_ops = RepeatingMessageOperations(session)
        message = await message_ops.get_repeating_message(message_id)
        
        if not message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Repeating message not found"
            )
        
        return RepeatingMessageResponse(
            id=str(message.id),
            guild_id=message.guild_id,
            channel_id=message.channel_id,
            message_content=message.message_content,
            role_id=message.role_id,
            start_time=message.start_time,
            interval_minutes=message.interval_minutes,
            next_send_time=message.next_send_time,
            is_active=message.is_active,
            total_sent=message.total_sent,
            last_sent_at=message.last_sent_at,
            created_by=message.created_by,
            created_at=message.created_at,
            updated_at=message.updated_at
        )
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting repeating message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve repeating message"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting repeating message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.put("/{message_id}")
async def update_repeating_message(
    message_id: UUID,
    request: UpdateRepeatingMessageRequest,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, bool]:
    """Update a repeating message.
    
    Args:
        message_id: UUID of the repeating message to update
        request: Update data
        
    Returns:
        Success status
    """
    try:
        message_ops = RepeatingMessageOperations(session)
        
        # Build update dict from request, excluding None values
        updates = {}
        for field, value in request.model_dump(exclude_none=True).items():
            updates[field] = value
        
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update"
            )
        
        success = await message_ops.update_repeating_message(message_id, **updates)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Repeating message not found"
            )
        
        logger.info(f"Updated repeating message {message_id}")
        
        return {"success": True}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error updating repeating message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update repeating message"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error updating repeating message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.delete("/{message_id}")
async def delete_repeating_message(
    message_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, bool]:
    """Delete a repeating message.
    
    Args:
        message_id: UUID of the repeating message to delete
        
    Returns:
        Success status
    """
    try:
        message_ops = RepeatingMessageOperations(session)
        success = await message_ops.delete_repeating_message(message_id)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Repeating message not found"
            )
        
        logger.info(f"Deleted repeating message {message_id}")
        
        return {"success": True}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error deleting repeating message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete repeating message"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error deleting repeating message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.post("/{message_id}/toggle")
async def toggle_repeating_message(
    message_id: UUID,
    is_active: bool,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, bool]:
    """Enable or disable a repeating message.
    
    Args:
        message_id: UUID of the repeating message to toggle
        is_active: Whether to enable or disable the message
        
    Returns:
        Success status
    """
    try:
        message_ops = RepeatingMessageOperations(session)
        success = await message_ops.toggle_repeating_message(message_id, is_active)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Repeating message not found"
            )
        
        action = "enabled" if is_active else "disabled"
        logger.info(f"{action.capitalize()} repeating message {message_id}")
        
        return {"success": True}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error toggling repeating message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle repeating message"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error toggling repeating message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )