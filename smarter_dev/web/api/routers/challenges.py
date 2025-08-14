"""Challenge API router for Discord bot integration.

Provides endpoints for challenge announcement management and release scheduling.
Used by the Discord bot to check for pending announcements and mark challenges as announced.
"""

from __future__ import annotations

import logging
from typing import List, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.crud import CampaignOperations, ChallengeInputOperations, SquadOperations, DatabaseOperationError, ScriptExecutionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/challenges", tags=["challenges"])


@router.get("/pending-announcements")
async def get_pending_announcements(
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, List[Dict[str, Any]]]:
    """Get challenges that should be announced but haven't been yet.
    
    Returns:
        Dictionary with list of challenge data for bot announcement
    """
    try:
        campaign_ops = CampaignOperations(session)
        challenges = await campaign_ops.get_pending_announcements()
        
        # Format challenges for bot consumption
        challenge_list = []
        for challenge in challenges:
            campaign = challenge.campaign
            challenge_data = {
                "id": str(challenge.id),
                "title": challenge.title,
                "description": challenge.description,
                "guild_id": campaign.guild_id,
                "announcement_channels": campaign.announcement_channels,
                "order_position": challenge.order_position,
                "released_at": challenge.released_at.isoformat() if challenge.released_at else None,
                "campaign": {
                    "id": str(campaign.id),
                    "title": campaign.title,
                    "start_time": campaign.start_time.isoformat(),
                    "release_cadence_hours": campaign.release_cadence_hours,
                }
            }
            challenge_list.append(challenge_data)
        
        logger.info(f"Retrieved {len(challenge_list)} challenges pending announcement")
        
        return {"challenges": challenge_list}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting pending announcements: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve pending announcements"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting pending announcements: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.post("/{challenge_id}/mark-released")
async def mark_challenge_released(
    challenge_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, bool]:
    """Mark a challenge as released.
    
    Args:
        challenge_id: UUID of the challenge to mark as released
        
    Returns:
        Success status
    """
    try:
        campaign_ops = CampaignOperations(session)
        success = await campaign_ops.mark_challenge_released(challenge_id)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Challenge not found"
            )
        
        logger.info(f"Marked challenge {challenge_id} as released")
        
        return {"success": True}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error marking challenge as released: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark challenge as released"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error marking challenge as released: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.post("/{challenge_id}/mark-announced")
async def mark_challenge_announced(
    challenge_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, bool]:
    """Mark a challenge as announced to Discord channels.
    
    Args:
        challenge_id: UUID of the challenge to mark as announced
        
    Returns:
        Success status
    """
    try:
        campaign_ops = CampaignOperations(session)
        success = await campaign_ops.mark_challenge_announced(challenge_id)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Challenge not found"
            )
        
        logger.info(f"Marked challenge {challenge_id} as announced")
        
        return {"success": True}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error marking challenge as announced: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark challenge as announced"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error marking challenge as announced: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/{challenge_id}")
async def get_challenge(
    challenge_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, Dict[str, Any]]:
    """Get a challenge with its campaign data.
    
    Args:
        challenge_id: UUID of the challenge to retrieve
        
    Returns:
        Challenge data with campaign information
    """
    try:
        campaign_ops = CampaignOperations(session)
        challenge = await campaign_ops.get_challenge_with_campaign(challenge_id)
        
        if not challenge:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Challenge not found"
            )
        
        campaign = challenge.campaign
        challenge_data = {
            "id": str(challenge.id),
            "title": challenge.title,
            "description": challenge.description,
            "order_position": challenge.order_position,
            "is_released": challenge.is_released,
            "is_announced": challenge.is_announced,
            "released_at": challenge.released_at.isoformat() if challenge.released_at else None,
            "announced_at": challenge.announced_at.isoformat() if challenge.announced_at else None,
            "created_at": challenge.created_at.isoformat(),
            "guild_id": campaign.guild_id,
            "announcement_channels": campaign.announcement_channels,
            "campaign": {
                "id": str(campaign.id),
                "title": campaign.title,
                "start_time": campaign.start_time.isoformat(),
                "release_cadence_hours": campaign.release_cadence_hours,
                "is_active": campaign.is_active,
            }
        }
        
        return {"challenge": challenge_data}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting challenge: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve challenge"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting challenge: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/{challenge_id}/input-exists")
async def check_challenge_input_exists(
    challenge_id: UUID,
    guild_id: str = Query(..., description="Discord guild ID"),
    user_id: str = Query(..., description="Discord user ID"),
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, bool]:
    """Check if challenge input data already exists for a user's squad.
    
    This endpoint checks if input has been generated without actually generating it,
    useful for determining whether to show a confirmation prompt.
    
    Args:
        challenge_id: UUID of the challenge
        guild_id: Discord guild ID (query parameter)
        user_id: Discord user ID (query parameter, used to determine squad membership)
        
    Returns:
        Dictionary with exists boolean flag
    """
    try:
        # Get user's squad
        squad_ops = SquadOperations()
        user_squad = await squad_ops.get_user_squad(session, guild_id, user_id)
        
        if not user_squad:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User is not a member of any squad"
            )
        
        # Check if input exists for this challenge and squad
        input_ops = ChallengeInputOperations(session)
        existing_input = await input_ops.get_existing_input(challenge_id, user_squad.id)
        
        return {"exists": existing_input is not None}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error checking input existence: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/{challenge_id}/input")
async def get_challenge_input(
    challenge_id: UUID,
    guild_id: str = Query(..., description="Discord guild ID"),
    user_id: str = Query(..., description="Discord user ID"),
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, Any]:
    """Get challenge input data for a user's squad.
    
    Gets existing input data if available, or generates new input by executing
    the challenge's input generator script. All squad members receive the same input.
    
    Args:
        challenge_id: UUID of the challenge
        guild_id: Discord guild ID (query parameter)
        user_id: Discord user ID (query parameter, used to determine squad membership)
        
    Returns:
        Dictionary with input data and metadata
    """
    try:
        # Get user's squad
        squad_ops = SquadOperations()
        user_squad = await squad_ops.get_user_squad(session, guild_id, user_id)
        
        if not user_squad:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User is not a member of any squad"
            )
        
        # Get the challenge to access its input generator script
        campaign_ops = CampaignOperations(session)
        challenge = await campaign_ops.get_challenge_with_campaign(challenge_id)
        
        if not challenge:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Challenge not found"
            )
        
        # Verify the challenge belongs to the correct guild
        if challenge.campaign.guild_id != guild_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Challenge does not belong to the specified guild"
            )
        
        # Check if challenge is released
        if not challenge.is_released:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Challenge has not been released yet"
            )
        
        # Check if the challenge has an input generator script
        if not challenge.input_generator_script:
            logger.warning(f"Challenge {challenge_id} has no input generator script")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This challenge does not have input generation configured yet. Please contact an administrator."
            )

        # Get or generate input data for the squad
        input_ops = ChallengeInputOperations(session)
        
        try:
            input_data, result_data = await input_ops.get_or_create_input(
                challenge_id=challenge_id,
                squad_id=user_squad.id,
                script=challenge.input_generator_script
            )
        except ScriptExecutionError as e:
            logger.error(f"Script execution error for challenge {challenge_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate challenge input due to script execution error"
            )
        
        logger.info(f"Provided challenge input for user {user_id} in squad {user_squad.id} for challenge {challenge_id}")
        
        return {
            "input_data": input_data,
            "challenge": {
                "id": str(challenge.id),
                "title": challenge.title,
                "description": challenge.description,
                "order_position": challenge.order_position,
            },
            "squad": {
                "id": str(user_squad.id),
                "name": user_squad.name,
            },
            "metadata": {
                "has_existing_input": True  # Since we always return existing or create new
            }
        }
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting challenge input: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve challenge input"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting challenge input: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )