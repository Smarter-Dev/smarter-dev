"""Scheduled Message API router for Discord bot integration.

Provides endpoints for scheduled message management and sending automation.
Used by the Discord bot to check for pending scheduled messages and mark them as sent.
"""

from __future__ import annotations

import logging
from typing import List, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.crud import ScheduledMessageOperations, CampaignOperations, DatabaseOperationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduled-messages", tags=["scheduled-messages"])


@router.get("/pending")
async def get_pending_scheduled_messages(
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, List[Dict[str, Any]]]:
    """Get scheduled messages that should be sent but haven't been yet.
    
    Returns:
        Dictionary with list of scheduled message data for bot sending
    """
    try:
        message_ops = ScheduledMessageOperations(session)
        scheduled_messages = await message_ops.get_pending_scheduled_messages()
        
        # Format scheduled messages for bot consumption
        message_list = []
        for message in scheduled_messages:
            campaign = message.campaign
            message_data = {
                "id": str(message.id),
                "title": message.title,
                "description": message.description,
                "scheduled_time": message.scheduled_time.isoformat(),
                "guild_id": campaign.guild_id,
                "announcement_channels": campaign.announcement_channels,
                "campaign": {
                    "id": str(campaign.id),
                    "title": campaign.title,
                    "is_active": campaign.is_active,
                }
            }
            message_list.append(message_data)
        
        logger.info(f"Retrieved {len(message_list)} scheduled messages pending")
        
        return {"scheduled_messages": message_list}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting pending scheduled messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve pending scheduled messages"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting pending scheduled messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.post("/{message_id}/mark-sent")
async def mark_scheduled_message_sent(
    message_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, bool]:
    """Mark a scheduled message as sent.
    
    Args:
        message_id: UUID of the scheduled message to mark as sent
        
    Returns:
        Success status
    """
    try:
        message_ops = ScheduledMessageOperations(session)
        success = await message_ops.mark_scheduled_message_sent(message_id)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Scheduled message not found"
            )
        
        logger.info(f"Marked scheduled message {message_id} as sent")
        
        return {"success": True}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error marking scheduled message as sent: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark scheduled message as sent"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error marking scheduled message as sent: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/{message_id}")
async def get_scheduled_message(
    message_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, Dict[str, Any]]:
    """Get a scheduled message with its campaign data.
    
    Args:
        message_id: UUID of the scheduled message to retrieve
        
    Returns:
        Scheduled message data with campaign information
    """
    try:
        message_ops = ScheduledMessageOperations(session)
        message = await message_ops.get_scheduled_message_with_campaign(message_id)
        
        if not message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Scheduled message not found"
            )
        
        campaign = message.campaign
        message_data = {
            "id": str(message.id),
            "title": message.title,
            "description": message.description,
            "scheduled_time": message.scheduled_time.isoformat(),
            "is_sent": message.is_sent,
            "sent_at": message.sent_at.isoformat() if message.sent_at else None,
            "created_at": message.created_at.isoformat(),
            "guild_id": campaign.guild_id,
            "announcement_channels": campaign.announcement_channels,
            "campaign": {
                "id": str(campaign.id),
                "title": campaign.title,
                "is_active": campaign.is_active,
            }
        }
        
        return {"scheduled_message": message_data}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting scheduled message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve scheduled message"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting scheduled message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )