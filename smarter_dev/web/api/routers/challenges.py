"""Challenge API router for Discord bot integration.

Provides endpoints for challenge announcement management and release scheduling.
Used by the Discord bot to check for pending announcements and mark challenges as announced.
"""

from __future__ import annotations

import logging
from typing import List, Dict, Any
from uuid import UUID
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.crud import CampaignOperations, ChallengeInputOperations, ChallengeSubmissionOperations, SquadOperations, DatabaseOperationError, ScriptExecutionError
from smarter_dev.web.models import Campaign

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/challenges", tags=["challenges"])


class SolutionSubmissionRequest(BaseModel):
    guild_id: str
    user_id: str
    username: str
    submitted_solution: str


@router.get("/upcoming-announcements")
async def get_upcoming_announcements(
    seconds: int = Query(default=45, description="Look ahead seconds"),
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, List[Dict[str, Any]]]:
    """Get challenges that will be announced in the next N seconds.
    
    Args:
        seconds: Number of seconds to look ahead (default 45)
        
    Returns:
        Dictionary with list of challenge data for bot queuing
    """
    try:
        campaign_ops = CampaignOperations(session)
        upcoming_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        challenges = await campaign_ops.get_upcoming_announcements(upcoming_time)
        
        # Format challenges for bot consumption
        challenge_list = []
        for challenge in challenges:
            campaign = challenge.campaign
            
            # Calculate release time based on campaign start and challenge position
            release_time = campaign.start_time + timedelta(hours=campaign.release_cadence_hours * (challenge.order_position - 1))
            
            challenge_data = {
                "id": str(challenge.id),
                "title": challenge.title,
                "description": challenge.description,
                "guild_id": campaign.guild_id,
                "announcement_channels": campaign.announcement_channels,
                "order_position": challenge.order_position,
                "release_time": release_time.isoformat(),
                "campaign": {
                    "id": str(campaign.id),
                    "title": campaign.title,
                    "start_time": campaign.start_time.isoformat(),
                    "release_cadence_hours": campaign.release_cadence_hours,
                }
            }
            challenge_list.append(challenge_data)
        
        logger.debug(f"Retrieved {len(challenge_list)} challenges for next {seconds} seconds")
        
        return {"challenges": challenge_list}
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting upcoming announcements: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve upcoming announcements"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting upcoming announcements: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


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


@router.get("/scoreboard")
async def get_scoreboard(
    guild_id: str = Query(..., description="Discord guild ID"),
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, Any]:
    """Get the challenge scoreboard for the most recently begun campaign.
    
    Returns squad rankings based on total points earned from successful challenge submissions.
    
    Args:
        guild_id: Discord guild ID (query parameter)
        
    Returns:
        Dictionary with campaign info, scoreboard data, and statistics
    """
    try:
        # Get the most recently begun campaign for the guild
        campaign_ops = CampaignOperations(session)
        current_campaign = await campaign_ops.get_most_recent_campaign(guild_id)
        
        if not current_campaign:
            # No campaign found - check for upcoming campaigns
            return {
                "campaign": None,
                "scoreboard": [],
                "total_submissions": 0,
                "total_challenges": 0
            }
        
        # Get scoreboard data for the campaign
        submission_ops = ChallengeSubmissionOperations(session)
        scoreboard_data = await submission_ops.get_campaign_scoreboard(current_campaign.id)
        
        # Get total submission and challenge counts for stats
        total_submissions = await submission_ops.get_campaign_submission_count(current_campaign.id)
        total_challenges = await campaign_ops.get_campaign_challenge_count(current_campaign.id)
        
        # Format campaign data
        campaign_data = {
            "id": str(current_campaign.id),
            "name": current_campaign.title,
            "start_date": current_campaign.start_time.strftime("%B %d, %Y") if current_campaign.start_time else None,
            "end_date": None,  # Campaign model doesn't have end_time field
            "is_active": current_campaign.is_active,
            "guild_id": current_campaign.guild_id
        }
        
        # Format scoreboard data
        formatted_scoreboard = []
        for entry in scoreboard_data:
            formatted_entry = {
                "squad_name": entry["squad_name"],
                "total_points": entry["total_points"] or 0,
                "successful_submissions": entry["successful_submissions"] or 0,
                "squad_id": str(entry["squad_id"])
            }
            formatted_scoreboard.append(formatted_entry)
        
        logger.info(f"Retrieved scoreboard for campaign {current_campaign.id} with {len(formatted_scoreboard)} squads")
        
        return {
            "campaign": campaign_data,
            "scoreboard": formatted_scoreboard,
            "total_submissions": total_submissions,
            "total_challenges": total_challenges
        }
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting scoreboard: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve scoreboard data"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting scoreboard: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/upcoming-campaign")
async def get_upcoming_campaign(
    guild_id: str = Query(..., description="Discord guild ID"),
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, Any]:
    """Get the next upcoming campaign for a guild.
    
    Used by the bot to show upcoming campaign info when no current campaign exists.
    
    Args:
        guild_id: Discord guild ID (query parameter)
        
    Returns:
        Dictionary with upcoming campaign info or None
    """
    try:
        # Get the next upcoming campaign (start time in future)
        query = select(Campaign).where(
            and_(
                Campaign.guild_id == guild_id,
                Campaign.start_time > datetime.now(timezone.utc)
            )
        ).order_by(Campaign.start_time.asc()).limit(1)
        
        result = await session.execute(query)
        upcoming_campaign = result.scalar_one_or_none()
        
        if not upcoming_campaign:
            return {"campaign": None}
        
        campaign_data = {
            "id": str(upcoming_campaign.id),
            "name": upcoming_campaign.title,
            "start_date": upcoming_campaign.start_time.strftime("%B %d, %Y at %I:%M %p UTC") if upcoming_campaign.start_time else None,
            "description": upcoming_campaign.description,
            "guild_id": upcoming_campaign.guild_id
        }
        
        return {"campaign": campaign_data}
        
    except Exception as e:
        logger.error(f"Unexpected error getting upcoming campaign: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


@router.get("/detailed-scoreboard")
async def get_detailed_scoreboard(
    guild_id: str = Query(..., description="Discord guild ID"),
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, Any]:
    """Get detailed scoreboard with points breakdown by challenge.
    
    Returns squad rankings with detailed breakdown of points earned per challenge.
    
    Args:
        guild_id: Discord guild ID (query parameter)
        
    Returns:
        Dictionary with campaign info, detailed scoreboard data, and statistics
    """
    try:
        # Get the most recently begun campaign for the guild
        campaign_ops = CampaignOperations(session)
        current_campaign = await campaign_ops.get_most_recent_campaign(guild_id)
        
        if not current_campaign:
            return {
                "campaign": None,
                "detailed_scoreboard": [],
                "total_submissions": 0,
                "total_challenges": 0
            }
        
        # Get detailed scoreboard data for the campaign
        submission_ops = ChallengeSubmissionOperations(session)
        detailed_data = await submission_ops.get_detailed_campaign_scoreboard(current_campaign.id)
        
        # Get total submission and challenge counts for stats
        total_submissions = await submission_ops.get_campaign_submission_count(current_campaign.id)
        total_challenges = await campaign_ops.get_campaign_challenge_count(current_campaign.id)
        
        # Format campaign data
        campaign_data = {
            "id": str(current_campaign.id),
            "name": current_campaign.title,
            "start_date": current_campaign.start_time.strftime("%B %d, %Y") if current_campaign.start_time else None,
            "end_date": None,
            "is_active": current_campaign.is_active,
            "guild_id": current_campaign.guild_id
        }
        
        logger.info(f"Retrieved detailed scoreboard for campaign {current_campaign.id} with {len(detailed_data)} entries")
        
        return {
            "campaign": campaign_data,
            "detailed_scoreboard": detailed_data,
            "total_submissions": total_submissions,
            "total_challenges": total_challenges
        }
        
    except DatabaseOperationError as e:
        logger.error(f"Database error getting detailed scoreboard: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve detailed scoreboard data"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting detailed scoreboard: {e}")
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


@router.post("/{challenge_id}/submit-solution")
async def submit_solution(
    challenge_id: UUID,
    submission_data: SolutionSubmissionRequest,
    session: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key),
) -> Dict[str, Any]:
    """Submit a solution for a challenge and check if it's correct.
    
    Compares the submitted solution against the stored expected result for the squad.
    Records all submissions and tracks first successful submission per squad.
    
    Args:
        challenge_id: UUID of the challenge
        submission_data: Solution submission details including guild_id, user_id, username, and solution
        
    Returns:
        Dictionary with correctness status, first success status, and timestamp
    """
    try:
        # Get user's squad
        squad_ops = SquadOperations()
        user_squad = await squad_ops.get_user_squad(
            session, 
            submission_data.guild_id, 
            submission_data.user_id
        )
        
        if not user_squad:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User is not a member of any squad"
            )
        
        # Get the challenge to verify it exists and belongs to the guild
        campaign_ops = CampaignOperations(session)
        challenge = await campaign_ops.get_challenge_with_campaign(challenge_id)
        
        if not challenge:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Challenge not found"
            )
        
        # Verify the challenge belongs to the correct guild
        if challenge.campaign.guild_id != submission_data.guild_id:
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
        
        # Submit solution and get result
        submission_ops = ChallengeSubmissionOperations(session)
        is_correct, is_first_success, points_earned = await submission_ops.submit_solution(
            challenge_id=challenge_id,
            squad_id=user_squad.id,
            user_id=submission_data.user_id,
            username=submission_data.username,
            submitted_solution=submission_data.submitted_solution
        )
        
        logger.info(
            f"Solution submitted for challenge {challenge_id} by user {submission_data.user_id} "
            f"in squad {user_squad.id}: correct={is_correct}, first_success={is_first_success}, points={points_earned}"
        )
        
        return {
            "is_correct": is_correct,
            "is_first_success": is_first_success,
            "points_earned": points_earned,
            "challenge": {
                "id": str(challenge.id),
                "title": challenge.title,
            },
            "squad": {
                "id": str(user_squad.id),
                "name": user_squad.name,
            },
            "submitted_at": "just_now"  # Frontend can format current timestamp
        }
        
    except DatabaseOperationError as e:
        logger.error(f"Database error submitting solution: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit solution"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error submitting solution: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )
