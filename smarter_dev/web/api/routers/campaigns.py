"""Campaign management endpoints for the Smarter Dev API.

This module provides REST API endpoints for managing campaigns and challenges
including creation, management, submissions, and statistics.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api.dependencies import (
    get_database_session,
    verify_api_key,
    verify_guild_access,
    get_request_metadata,
    APIKey
)
from smarter_dev.web.api.exceptions import (
    create_validation_error,
    create_not_found_error,
    create_conflict_error,
    validate_discord_id
)
from smarter_dev.web.api.security_utils import (
    create_database_error,
    create_not_found_error as create_secure_not_found_error,
    create_validation_error as create_secure_validation_error
)
from smarter_dev.web.api.schemas import (
    CampaignResponse,
    CampaignCreate,
    CampaignUpdate,
    CampaignListResponse,
    ChallengeResponse,
    ChallengeCreate,
    ChallengeUpdate,
    SubmissionResponse,
    SubmissionCreate,
    CampaignStatsResponse,
    SuccessResponse
)
from web.repositories.campaign_repository import CampaignRepository
from web.repositories.challenge_repository import ChallengeRepository  
from web.repositories.submission_repository import SubmissionRepository
from web.services import (
    SquadIntegrationService,
    ChallengeReleaseService,
    InputGenerationService,
    SubmissionValidationService,
    RateLimitingService
)
from web.services.input_generation_service import InputGenerationStatus
from smarter_dev.web.crud import (
    DatabaseOperationError,
    NotFoundError,
    ConflictError
)

router = APIRouter(prefix="/campaigns", tags=["campaigns"])
logger = logging.getLogger(__name__)


# ============================================================================
# Campaign Management Endpoints  
# ============================================================================

@router.post("/", response_model=CampaignResponse, status_code=201)
async def create_campaign(
    campaign_data: CampaignCreate,
    api_key: APIKey,
    session: AsyncSession = Depends(get_database_session),
    request_metadata: dict = Depends(get_request_metadata)
) -> CampaignResponse:
    """Create a new campaign.
    
    Creates a new campaign with the provided configuration. Only accessible
    with valid API key authentication.
    
    Args:
        campaign_data: Campaign creation data
        session: Database session
        api_key: Verified API key
        request_metadata: Request metadata for logging
        
    Returns:
        Created campaign data
        
    Raises:
        HTTPException: If creation fails or validation errors occur
    """
    try:
        campaign_repo = CampaignRepository(session)
        
        # Convert pydantic model to dict for repository
        campaign_dict = campaign_data.model_dump()
        
        # Create campaign
        campaign = await campaign_repo.create_campaign(**campaign_dict)
        
        return CampaignResponse.model_validate(campaign)
        
    except ConflictError as e:
        raise create_conflict_error(f"Campaign creation failed: {str(e)}")
    except DatabaseOperationError as e:
        raise create_database_error(f"Database error during campaign creation: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error during campaign creation")


@router.get("/", response_model=CampaignListResponse)
async def list_campaigns(
    api_key: APIKey,
    guild_id: Optional[str] = Query(None, description="Filter by guild ID"),
    status: Optional[str] = Query(None, description="Filter by status"),
    participant_type: Optional[str] = Query(None, description="Filter by participant type"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
    session: AsyncSession = Depends(get_database_session)
) -> CampaignListResponse:
    """List campaigns with optional filtering and pagination.
    
    Retrieves campaigns with optional filters for guild, status, and participant type.
    Results are paginated for performance.
    
    Args:
        guild_id: Optional Discord guild ID filter
        status: Optional status filter
        participant_type: Optional participant type filter  
        page: Page number for pagination
        size: Number of items per page
        session: Database session
        api_key: Verified API key
        
    Returns:
        Paginated list of campaigns
    """
    try:
        campaign_repo = CampaignRepository(session)
        
        # Validate filter parameters
        if participant_type and participant_type not in ['player', 'squad']:
            raise create_validation_error("participant_type must be 'player' or 'squad'")
        
        if status and status not in ['draft', 'active', 'completed', 'cancelled']:
            raise create_validation_error("Invalid status value")
        
        if guild_id:
            validate_discord_id(guild_id, "guild_id")
        
        # Get campaigns with filters
        campaigns, total = await campaign_repo.list_campaigns(
            guild_id=guild_id,
            status=status,
            participant_type=participant_type,
            limit=size,
            offset=(page - 1) * size
        )
        
        # Calculate pagination info
        pages = (total + size - 1) // size
        
        campaign_responses = [CampaignResponse.model_validate(campaign) for campaign in campaigns]
        
        return CampaignListResponse(
            items=campaign_responses,
            total=total,
            page=page,
            size=size,
            pages=pages
        )
        
    except DatabaseOperationError as e:
        raise create_database_error(f"Database error during campaign listing: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error during campaign listing")


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    session: AsyncSession = Depends(get_database_session)
) -> CampaignResponse:
    """Get campaign by ID.
    
    Retrieves detailed information about a specific campaign.
    
    Args:
        campaign_id: Campaign UUID
        session: Database session
        api_key: Verified API key
        
    Returns:
        Campaign data
        
    Raises:
        HTTPException: If campaign not found
    """
    try:
        campaign_repo = CampaignRepository(session)
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        return CampaignResponse.model_validate(campaign)
        
    except NotFoundError:
        raise create_not_found_error(f"Campaign {campaign_id} not found")
    except DatabaseOperationError as e:
        raise create_database_error(f"Database error retrieving campaign: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error retrieving campaign")


@router.put("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_update: CampaignUpdate,
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    session: AsyncSession = Depends(get_database_session)
) -> CampaignResponse:
    """Update campaign.
    
    Updates an existing campaign with new data. Only non-null fields are updated.
    
    Args:
        campaign_id: Campaign UUID
        campaign_update: Updated campaign data
        session: Database session
        api_key: Verified API key
        
    Returns:
        Updated campaign data
        
    Raises:
        HTTPException: If campaign not found or update fails
    """
    try:
        campaign_repo = CampaignRepository(session)
        
        # Check if campaign exists
        existing_campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not existing_campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        # Update campaign
        update_data = campaign_update.model_dump(exclude_unset=True)
        updated_campaign = await campaign_repo.update_campaign(campaign_id, **update_data)
        
        return CampaignResponse.model_validate(updated_campaign)
        
    except NotFoundError:
        raise create_not_found_error(f"Campaign {campaign_id} not found")
    except ConflictError as e:
        raise create_conflict_error(f"Campaign update failed: {str(e)}")
    except DatabaseOperationError as e:
        raise create_database_error(f"Database error updating campaign: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error updating campaign")


@router.delete("/{campaign_id}", response_model=SuccessResponse)
async def delete_campaign(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    session: AsyncSession = Depends(get_database_session)
) -> SuccessResponse:
    """Delete campaign.
    
    Soft deletes a campaign and marks it as cancelled.
    
    Args:
        campaign_id: Campaign UUID
        session: Database session
        api_key: Verified API key
        
    Returns:
        Success response
        
    Raises:
        HTTPException: If campaign not found or deletion fails
    """
    try:
        campaign_repo = CampaignRepository(session)
        
        success = await campaign_repo.delete_campaign(campaign_id)
        if not success:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        return SuccessResponse(message="Campaign deleted successfully")
        
    except NotFoundError:
        raise create_not_found_error(f"Campaign {campaign_id} not found")
    except DatabaseOperationError as e:
        raise create_database_error(f"Database error deleting campaign: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error deleting campaign")


# ============================================================================
# Challenge Management Endpoints
# ============================================================================

@router.post("/{campaign_id}/challenges", response_model=ChallengeResponse, status_code=201)
async def create_challenge(
    challenge_data: ChallengeCreate,
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    session: AsyncSession = Depends(get_database_session)
) -> ChallengeResponse:
    """Create a new challenge in a campaign.
    
    Args:
        campaign_id: Campaign UUID
        challenge_data: Challenge creation data
        session: Database session
        api_key: Verified API key
        
    Returns:
        Created challenge data
    """
    try:
        campaign_repo = CampaignRepository(session)
        challenge_repo = ChallengeRepository(session)
        
        # Verify campaign exists
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        # Create challenge
        challenge_dict = challenge_data.model_dump()
        challenge = await challenge_repo.create_challenge(campaign_id, **challenge_dict)
        
        return ChallengeResponse.model_validate(challenge)
        
    except NotFoundError:
        raise create_not_found_error(f"Campaign {campaign_id} not found")
    except ConflictError as e:
        raise create_conflict_error(f"Challenge creation failed: {str(e)}")
    except DatabaseOperationError as e:
        raise create_database_error(f"Database error creating challenge: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error creating challenge")


@router.get("/{campaign_id}/challenges", response_model=List[ChallengeResponse])
async def list_challenges(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    include_unreleased: bool = Query(False, description="Include unreleased challenges"),
    session: AsyncSession = Depends(get_database_session)
) -> List[ChallengeResponse]:
    """List challenges in a campaign.
    
    Args:
        campaign_id: Campaign UUID  
        include_unreleased: Whether to include unreleased challenges
        session: Database session
        api_key: Verified API key
        
    Returns:
        List of challenges
    """
    try:
        challenge_repo = ChallengeRepository(session)
        
        challenges = await challenge_repo.get_challenges_by_campaign(
            campaign_id,
            released_only=not include_unreleased
        )
        
        return [ChallengeResponse.model_validate(challenge) for challenge in challenges]
        
    except DatabaseOperationError as e:
        raise create_database_error(f"Database error listing challenges: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error listing challenges")


@router.get("/{campaign_id}/challenges/{challenge_id}", response_model=ChallengeResponse)
async def get_challenge(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    challenge_id: UUID = Path(..., description="Challenge ID"),
    session: AsyncSession = Depends(get_database_session)
) -> ChallengeResponse:
    """Get challenge by ID.
    
    Args:
        campaign_id: Campaign UUID
        challenge_id: Challenge UUID
        session: Database session
        api_key: Verified API key
        
    Returns:
        Challenge data
    """
    try:
        challenge_repo = ChallengeRepository(session)
        challenge = await challenge_repo.get_challenge_by_id(challenge_id)
        
        if not challenge or challenge.campaign_id != campaign_id:
            raise create_not_found_error(f"Challenge {challenge_id} not found in campaign {campaign_id}")
        
        return ChallengeResponse.model_validate(challenge)
        
    except NotFoundError:
        raise create_not_found_error(f"Challenge {challenge_id} not found")
    except DatabaseOperationError as e:
        raise create_database_error(f"Database error retrieving challenge: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error retrieving challenge")


# ============================================================================
# Campaign Statistics Endpoints
# ============================================================================

@router.get("/{campaign_id}/stats", response_model=CampaignStatsResponse)
async def get_campaign_stats(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    session: AsyncSession = Depends(get_database_session)
) -> CampaignStatsResponse:
    """Get campaign statistics.
    
    Args:
        campaign_id: Campaign UUID
        session: Database session
        api_key: Verified API key
        
    Returns:
        Campaign statistics
    """
    try:
        campaign_repo = CampaignRepository(session)
        submission_repo = SubmissionRepository(session)
        
        # Verify campaign exists
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        # Get statistics
        stats = await submission_repo.get_campaign_statistics(campaign_id)
        
        return CampaignStatsResponse(
            campaign_id=campaign_id,
            total_challenges=stats.get('total_challenges', 0),
            released_challenges=stats.get('released_challenges', 0), 
            total_participants=stats.get('total_participants', 0),
            total_submissions=stats.get('total_submissions', 0),
            correct_submissions=stats.get('correct_submissions', 0),
            success_rate=stats.get('success_rate', 0.0),
            avg_points_per_participant=stats.get('avg_points_per_participant', 0.0),
            top_participants=stats.get('top_participants', []),
            challenge_completion_rates=stats.get('challenge_completion_rates', [])
        )
        
    except NotFoundError:
        raise create_not_found_error(f"Campaign {campaign_id} not found")
    except DatabaseOperationError as e:
        raise create_database_error(f"Database error retrieving statistics: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error retrieving statistics")


# ============================================================================
# Submission Management Endpoints
# ============================================================================

@router.post("/{campaign_id}/challenges/{challenge_id}/submit", response_model=SubmissionResponse, status_code=201)
async def submit_challenge_answer(
    submission_data: SubmissionCreate,
    api_key: APIKey,
    request: Request,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    challenge_id: UUID = Path(..., description="Challenge ID"),
    session: AsyncSession = Depends(get_database_session)
) -> SubmissionResponse:
    """Submit an answer for a challenge.
    
    Processes the submission through validation services and scoring strategies
    to determine correctness and points awarded.
    
    Args:
        campaign_id: Campaign UUID
        challenge_id: Challenge UUID  
        submission_data: Submission data with answer
        api_key: Verified API key
        session: Database session
        request: FastAPI request object
        
    Returns:
        Submission result with correctness and points
        
    Raises:
        HTTPException: If submission processing fails
    """
    try:
        # Get participant ID from request headers
        participant_id = request.headers.get("X-User-ID")
        if not participant_id:
            raise create_validation_error("User ID required in X-User-ID header")
        
        campaign_repo = CampaignRepository(session)
        challenge_repo = ChallengeRepository(session)
        submission_repo = SubmissionRepository(session)
        
        # Verify campaign and challenge exist and are accessible
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        challenge = await challenge_repo.get_challenge_by_id(challenge_id)
        if not challenge or challenge.campaign_id != campaign_id:
            raise create_not_found_error(f"Challenge {challenge_id} not found in campaign {campaign_id}")
        
        # Check if challenge is released using release service
        from web.services import ChallengeReleaseService
        release_service = ChallengeReleaseService()
        
        is_released = await release_service.is_challenge_released(
            challenge=challenge,
            campaign_start_date=campaign.start_date,
            release_delay_minutes=campaign.release_delay_minutes
        )
        
        if not is_released:
            raise create_validation_error("Challenge has not been released yet")
        
        # Initialize services for submission processing
        from web.services import (
            InputGenerationService,
            SubmissionValidationService,
            RateLimitingService,
            create_scoring_strategy
        )
        
        # Apply rate limiting
        rate_limiter = RateLimitingService()
        try:
            await rate_limiter.check_submission_limit(participant_id, str(challenge_id))
        except Exception as e:
            if "rate limit exceeded" in str(e).lower():
                raise HTTPException(status_code=429, detail="Submission rate limit exceeded. Please try again later.")
        
        # Generate expected output using challenge script
        input_generator = InputGenerationService(submission_repository=submission_repo)
        generation_result = await input_generator.generate_input(
            challenge=challenge,
            participant_id=participant_id,
            participant_type=campaign.campaign_type
        )
        
        if generation_result.status != InputGenerationStatus.SUCCESS:
            logger.error(f"Failed to generate expected output for challenge {challenge_id}: {generation_result.error_message}")
            raise HTTPException(status_code=500, detail="Unable to validate submission at this time")
        
        # Validate submission against expected output
        validator = SubmissionValidationService()
        validation_result = await validator.validate_submission(
            submitted_answer=submission_data.submitted_result,
            expected_output=generation_result.expected_result,
            challenge_id=str(challenge_id)
        )
        
        # Calculate points if correct
        points_awarded = 0
        if validation_result.is_correct:
            scoring_strategy = create_scoring_strategy(
                strategy_type=campaign.scoring_type,
                config={
                    "starting_points": campaign.starting_points,
                    "points_decrease_step": campaign.points_decrease_step
                }
            )
            
            # Get current position/rank for scoring (simplified - could be enhanced)
            existing_submissions = await submission_repo.get_submissions_by_challenge(
                challenge_id, 
                correct_only=True
            )
            current_position = len(existing_submissions) + 1
            
            # Calculate challenge release time based on campaign start and position
            challenge_release_time = campaign.start_date + timedelta(
                minutes=(challenge.order_position - 1) * campaign.release_delay_minutes
            )
            
            scoring_result = scoring_strategy.calculate_score(
                submission_time=datetime.now(timezone.utc),
                challenge_release_time=challenge_release_time,
                position=current_position
            )
            points_awarded = scoring_result.points
        
        # Save submission to database
        submission = await submission_repo.create_submission(
            challenge_id=challenge_id,
            participant_id=participant_id,
            participant_type=campaign.campaign_type,
            submitted_result=submission_data.submitted_result,
            is_correct=validation_result.is_correct,
            points_awarded=points_awarded
        )
        
        # Record rate limiting usage
        await rate_limiter.record_submission(participant_id, str(challenge_id))
        
        return SubmissionResponse.model_validate(submission)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to process submission for challenge {challenge_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error processing submission")


@router.get("/{campaign_id}/submissions", response_model=List[SubmissionResponse])
async def get_campaign_submissions(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    participant_id: Optional[str] = Query(None, description="Filter by participant ID"),
    challenge_id: Optional[str] = Query(None, description="Filter by challenge ID"),
    correct_only: bool = Query(False, description="Show only correct submissions"),
    limit: int = Query(50, ge=1, le=200, description="Maximum submissions to return"),
    session: AsyncSession = Depends(get_database_session)
) -> List[SubmissionResponse]:
    """Get submissions for a campaign.
    
    Retrieves submissions with optional filtering by participant or challenge.
    
    Args:
        campaign_id: Campaign UUID
        api_key: Verified API key
        participant_id: Optional participant filter
        challenge_id: Optional challenge filter
        correct_only: Whether to show only correct submissions
        limit: Maximum number of results
        session: Database session
        
    Returns:
        List of submissions matching criteria
    """
    try:
        campaign_repo = CampaignRepository(session)
        submission_repo = SubmissionRepository(session)
        
        # Verify campaign exists
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        # Get submissions with filters
        submissions = await submission_repo.get_campaign_submissions(
            campaign_id=campaign_id,
            participant_id=participant_id,
            challenge_id=challenge_id,
            correct_only=correct_only,
            limit=limit
        )
        
        return [SubmissionResponse.model_validate(submission) for submission in submissions]
        
    except Exception as e:
        logger.error(f"Failed to get campaign submissions {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error retrieving submissions")


@router.get("/{campaign_id}/challenges/{challenge_id}/submissions", response_model=List[SubmissionResponse])
async def get_challenge_submissions(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    challenge_id: UUID = Path(..., description="Challenge ID"),
    correct_only: bool = Query(False, description="Show only correct submissions"),
    limit: int = Query(50, ge=1, le=200, description="Maximum submissions to return"),
    session: AsyncSession = Depends(get_database_session)
) -> List[SubmissionResponse]:
    """Get submissions for a specific challenge.
    
    Args:
        campaign_id: Campaign UUID
        challenge_id: Challenge UUID
        api_key: Verified API key
        correct_only: Whether to show only correct submissions
        limit: Maximum number of results
        session: Database session
        
    Returns:
        List of submissions for the challenge
    """
    try:
        campaign_repo = CampaignRepository(session)
        challenge_repo = ChallengeRepository(session)
        submission_repo = SubmissionRepository(session)
        
        # Verify campaign and challenge exist
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        challenge = await challenge_repo.get_challenge_by_id(challenge_id)
        if not challenge or challenge.campaign_id != campaign_id:
            raise create_not_found_error(f"Challenge {challenge_id} not found in campaign {campaign_id}")
        
        # Get submissions for this challenge
        submissions = await submission_repo.get_submissions_by_challenge(
            challenge_id=challenge_id,
            correct_only=correct_only,
            limit=limit
        )
        
        return [SubmissionResponse.model_validate(submission) for submission in submissions]
        
    except Exception as e:
        logger.error(f"Failed to get challenge submissions {challenge_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error retrieving submissions")


# ============================================================================
# Challenge Input Generation Endpoints
# ============================================================================

@router.post("/{campaign_id}/challenges/{challenge_id}/generate-input", status_code=201)
async def generate_challenge_input(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    challenge_id: UUID = Path(..., description="Challenge ID"),
    participant_id: str = Query(..., description="Participant ID"),
    participant_type: str = Query("player", description="Participant type: 'player' or 'squad'"),
    force_regenerate: bool = Query(False, description="Force regenerate even if cached"),
    session: AsyncSession = Depends(get_database_session)
):
    """Generate input for a specific challenge and participant.
    
    This endpoint allows manual input generation for testing and debugging purposes.
    Useful for pre-generating inputs or troubleshooting generation scripts.
    
    Args:
        campaign_id: Campaign UUID
        challenge_id: Challenge UUID
        participant_id: Participant ID (player or squad)
        participant_type: Type of participant
        force_regenerate: Whether to force regeneration even if cached
        api_key: Verified API key
        session: Database session
        
    Returns:
        Generation result with input data and expected output
    """
    try:
        campaign_repo = CampaignRepository(session)
        challenge_repo = ChallengeRepository(session)
        submission_repo = SubmissionRepository(session)
        
        # Verify campaign and challenge exist
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        challenge = await challenge_repo.get_challenge_by_id(challenge_id)
        if not challenge or challenge.campaign_id != campaign_id:
            raise create_not_found_error(f"Challenge {challenge_id} not found in campaign {campaign_id}")
        
        # Validate participant type
        if participant_type not in ['player', 'squad']:
            raise create_validation_error("participant_type must be 'player' or 'squad'")
        
        # Force cache invalidation if requested
        if force_regenerate:
            await submission_repo.invalidate_input_cache(
                challenge_id=challenge_id,
                participant_id=participant_id,
                participant_type=participant_type
            )
        
        # Generate input
        input_generator = InputGenerationService(submission_repository=submission_repo)
        generation_result = await input_generator.generate_input(
            challenge=challenge,
            participant_id=participant_id,
            participant_type=participant_type
        )
        
        return {
            "status": generation_result.status.value,
            "input_data": generation_result.input_data,
            "expected_result": generation_result.expected_result,
            "execution_time_ms": generation_result.execution_time_ms,
            "cached": generation_result.cached,
            "error_message": generation_result.error_message,
            "script_output": generation_result.script_output
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to generate input for challenge {challenge_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error generating input")


@router.delete("/{campaign_id}/challenges/{challenge_id}/input-cache")
async def invalidate_challenge_input_cache(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    challenge_id: UUID = Path(..., description="Challenge ID"),
    participant_id: Optional[str] = Query(None, description="Specific participant ID to invalidate"),
    participant_type: Optional[str] = Query(None, description="Specific participant type to invalidate"),
    session: AsyncSession = Depends(get_database_session)
):
    """Invalidate input cache for a challenge.
    
    Useful when challenge generation script is updated and cached inputs
    need to be cleared to force regeneration with the new script.
    
    Args:
        campaign_id: Campaign UUID
        challenge_id: Challenge UUID
        participant_id: Optional specific participant
        participant_type: Optional specific participant type
        api_key: Verified API key
        session: Database session
        
    Returns:
        Success response with count of invalidated entries
    """
    try:
        campaign_repo = CampaignRepository(session)
        challenge_repo = ChallengeRepository(session)
        submission_repo = SubmissionRepository(session)
        
        # Verify campaign and challenge exist
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        challenge = await challenge_repo.get_challenge_by_id(challenge_id)
        if not challenge or challenge.campaign_id != campaign_id:
            raise create_not_found_error(f"Challenge {challenge_id} not found in campaign {campaign_id}")
        
        # Validate participant type if provided
        if participant_type and participant_type not in ['player', 'squad']:
            raise create_validation_error("participant_type must be 'player' or 'squad'")
        
        # Invalidate cache
        invalidated_count = await submission_repo.invalidate_input_cache(
            challenge_id=challenge_id,
            participant_id=participant_id,
            participant_type=participant_type
        )
        
        return {
            "message": f"Invalidated {invalidated_count} cached input entries",
            "challenge_id": str(challenge_id),
            "invalidated_count": invalidated_count
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to invalidate input cache for challenge {challenge_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error invalidating cache")


@router.get("/{campaign_id}/input-cache-stats")
async def get_campaign_input_cache_stats(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    session: AsyncSession = Depends(get_database_session)
):
    """Get input cache statistics for a campaign.
    
    Provides insights into cache usage, hit rates, and performance metrics
    for the campaign's input generation system.
    
    Args:
        campaign_id: Campaign UUID
        api_key: Verified API key
        session: Database session
        
    Returns:
        Cache statistics and performance metrics
    """
    try:
        from sqlalchemy import select, func
        from smarter_dev.web.models import GeneratedInputCache
        
        campaign_repo = CampaignRepository(session)
        
        # Verify campaign exists
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        # Get cache statistics by joining with challenges
        cache_stats_query = select(
            func.count(GeneratedInputCache.id).label('total_cached_entries'),
            func.count(func.distinct(GeneratedInputCache.participant_id)).label('unique_participants'),
            func.count(func.distinct(GeneratedInputCache.challenge_id)).label('cached_challenges'),
            func.sum(
                func.case((GeneratedInputCache.is_valid == True, 1), else_=0)
            ).label('valid_entries'),
            func.avg(
                func.extract('epoch', GeneratedInputCache.updated_at - GeneratedInputCache.created_at)
            ).label('avg_cache_age_seconds')
        ).join(
            Challenge, GeneratedInputCache.challenge_id == Challenge.id
        ).where(
            Challenge.campaign_id == campaign_id
        )
        
        result = await session.execute(cache_stats_query)
        stats_row = result.fetchone()
        
        if not stats_row:
            return {
                "campaign_id": str(campaign_id),
                "total_cached_entries": 0,
                "unique_participants": 0,
                "cached_challenges": 0,
                "valid_entries": 0,
                "cache_hit_rate": 0.0,
                "avg_cache_age_seconds": 0.0,
                "cache_efficiency": {
                    "participants_with_cache": 0,
                    "challenges_with_cache": 0,
                    "cache_coverage": "0%"
                }
            }
        
        # Calculate additional metrics
        total_entries = stats_row.total_cached_entries or 0
        valid_entries = stats_row.valid_entries or 0
        cache_hit_rate = (valid_entries / max(total_entries, 1)) * 100
        
        # Get total challenges count for coverage calculation
        total_challenges_query = select(func.count(Challenge.id)).where(
            Challenge.campaign_id == campaign_id
        )
        total_challenges_result = await session.execute(total_challenges_query)
        total_challenges = total_challenges_result.scalar() or 0
        
        cached_challenges = stats_row.cached_challenges or 0
        cache_coverage_pct = (cached_challenges / max(total_challenges, 1)) * 100
        
        return {
            "campaign_id": str(campaign_id),
            "total_cached_entries": total_entries,
            "unique_participants": stats_row.unique_participants or 0,
            "cached_challenges": cached_challenges,
            "valid_entries": valid_entries,
            "cache_hit_rate": round(cache_hit_rate, 2),
            "avg_cache_age_seconds": round(stats_row.avg_cache_age_seconds or 0.0, 2),
            "cache_efficiency": {
                "participants_with_cache": stats_row.unique_participants or 0,
                "challenges_with_cache": cached_challenges,
                "cache_coverage": f"{cache_coverage_pct:.1f}%"
            },
            "performance_insights": {
                "cache_status": "healthy" if cache_hit_rate > 80 else "needs_attention" if cache_hit_rate > 50 else "poor",
                "recommendation": 
                    "Cache performing well" if cache_hit_rate > 80 else
                    "Consider pre-generating inputs for better performance" if cache_hit_rate > 50 else
                    "Low cache efficiency - check generation scripts"
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get input cache stats for campaign {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error retrieving cache statistics")


# ============================================================================
# Leaderboard and Statistics Endpoints
# ============================================================================

@router.get("/{campaign_id}/leaderboard")
async def get_campaign_leaderboard(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    participant_type: Optional[str] = Query(None, description="Filter by participant type: 'player' or 'squad'"),
    limit: int = Query(50, ge=1, le=200, description="Maximum leaderboard entries to return"),
    session: AsyncSession = Depends(get_database_session)
):
    """Get leaderboard for a campaign.
    
    Returns ranked participants based on total points earned across all
    completed challenges in the campaign. Integrates with Discord squad system
    for squad-based competitions.
    
    Args:
        campaign_id: Campaign UUID
        participant_type: Optional filter by participant type
        limit: Maximum number of entries to return
        api_key: Verified API key
        session: Database session
        
    Returns:
        Ranked leaderboard with participant scores and statistics
    """
    try:
        campaign_repo = CampaignRepository(session)
        submission_repo = SubmissionRepository(session)
        
        # Verify campaign exists
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        # Validate participant type if provided
        if participant_type and participant_type not in ['player', 'squad']:
            raise create_validation_error("participant_type must be 'player' or 'squad'")
        
        # Get leaderboard data
        leaderboard_data = await submission_repo.get_leaderboard_data(
            campaign_id=campaign_id,
            participant_type=participant_type,
            limit=limit
        )
        
        # Enhance leaderboard data with additional info
        enhanced_leaderboard = []
        for i, entry in enumerate(leaderboard_data, 1):
            enhanced_entry = {
                "rank": i,
                "participant_id": entry["participant_id"],
                "participant_type": entry["participant_type"],
                "total_points": entry["total_points"],
                "completed_challenges": entry["completed_challenges"],
                "first_completion": entry["first_completion"],
                "points_per_challenge": round(
                    entry["total_points"] / max(entry["completed_challenges"], 1), 1
                )
            }
            
            # Add participant display info (integrate with Discord system later)
            if entry["participant_type"] == "squad":
                enhanced_entry["display_name"] = f"Squad {entry['participant_id']}"
            else:
                enhanced_entry["display_name"] = f"Player {entry['participant_id']}"
            
            enhanced_leaderboard.append(enhanced_entry)
        
        return {
            "campaign_id": str(campaign_id),
            "participant_type_filter": participant_type,
            "total_entries": len(enhanced_leaderboard),
            "leaderboard": enhanced_leaderboard
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get campaign leaderboard {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error retrieving leaderboard")


@router.get("/{campaign_id}/challenges/{challenge_id}/leaderboard")
async def get_challenge_leaderboard(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    challenge_id: UUID = Path(..., description="Challenge ID"),
    participant_type: Optional[str] = Query(None, description="Filter by participant type"),
    limit: int = Query(50, ge=1, le=200, description="Maximum leaderboard entries"),
    session: AsyncSession = Depends(get_database_session)
):
    """Get leaderboard for a specific challenge.
    
    Returns participants ranked by their performance on a single challenge,
    including timing and scoring details.
    
    Args:
        campaign_id: Campaign UUID
        challenge_id: Challenge UUID
        participant_type: Optional participant type filter
        limit: Maximum entries to return
        api_key: Verified API key
        session: Database session
        
    Returns:
        Challenge-specific leaderboard with detailed performance metrics
    """
    try:
        campaign_repo = CampaignRepository(session)
        challenge_repo = ChallengeRepository(session)
        submission_repo = SubmissionRepository(session)
        
        # Verify campaign and challenge exist
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        challenge = await challenge_repo.get_challenge_by_id(challenge_id)
        if not challenge or challenge.campaign_id != campaign_id:
            raise create_not_found_error(f"Challenge {challenge_id} not found in campaign {campaign_id}")
        
        # Validate participant type
        if participant_type and participant_type not in ['player', 'squad']:
            raise create_validation_error("participant_type must be 'player' or 'squad'")
        
        # Get challenge-specific leaderboard
        leaderboard_data = await submission_repo.get_leaderboard_data(
            challenge_id=challenge_id,
            participant_type=participant_type,
            limit=limit
        )
        
        # Get challenge release time for timing calculations
        challenge_release_time = campaign.start_date + timedelta(
            minutes=(challenge.order_position - 1) * campaign.release_delay_minutes
        )
        
        # Enhance with challenge-specific metrics
        enhanced_leaderboard = []
        for i, entry in enumerate(leaderboard_data, 1):
            # Calculate solve time
            solve_time_minutes = None
            if entry["first_completion"] and challenge_release_time:
                solve_time_delta = entry["first_completion"] - challenge_release_time
                solve_time_minutes = max(0, solve_time_delta.total_seconds() / 60)
            
            enhanced_entry = {
                "rank": i,
                "participant_id": entry["participant_id"],
                "participant_type": entry["participant_type"],
                "points_awarded": entry["total_points"],
                "submission_time": entry["first_completion"],
                "solve_time_minutes": round(solve_time_minutes, 2) if solve_time_minutes is not None else None,
                "display_name": f"Squad {entry['participant_id']}" if entry["participant_type"] == "squad" else f"Player {entry['participant_id']}"
            }
            
            enhanced_leaderboard.append(enhanced_entry)
        
        return {
            "campaign_id": str(campaign_id),
            "challenge_id": str(challenge_id),
            "challenge_title": challenge.title,
            "challenge_release_time": challenge_release_time,
            "participant_type_filter": participant_type,
            "total_entries": len(enhanced_leaderboard),
            "leaderboard": enhanced_leaderboard
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get challenge leaderboard {challenge_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error retrieving challenge leaderboard")


@router.get("/{campaign_id}/participant/{participant_id}/stats")
async def get_participant_stats(
    api_key: APIKey,
    campaign_id: UUID = Path(..., description="Campaign ID"),
    participant_id: str = Path(..., description="Participant ID"),
    participant_type: str = Query("player", description="Participant type: 'player' or 'squad'"),
    session: AsyncSession = Depends(get_database_session)
):
    """Get detailed statistics for a specific participant.
    
    Provides comprehensive analytics including performance trends,
    challenge completion rates, and comparative rankings.
    
    Args:
        campaign_id: Campaign UUID
        participant_id: Participant ID (player or squad ID)
        participant_type: Type of participant
        api_key: Verified API key
        session: Database session
        
    Returns:
        Detailed participant performance statistics
    """
    try:
        campaign_repo = CampaignRepository(session)
        submission_repo = SubmissionRepository(session)
        
        # Verify campaign exists
        campaign = await campaign_repo.get_campaign_by_id(campaign_id)
        if not campaign:
            raise create_not_found_error(f"Campaign {campaign_id} not found")
        
        # Validate participant type
        if participant_type not in ['player', 'squad']:
            raise create_validation_error("participant_type must be 'player' or 'squad'")
        
        # Get participant's submissions
        participant_submissions = await submission_repo.get_campaign_submissions(
            campaign_id=campaign_id,
            participant_id=participant_id,
            correct_only=False,
            limit=1000  # Get all submissions
        )
        
        # Calculate statistics
        total_submissions = len(participant_submissions)
        correct_submissions = len([s for s in participant_submissions if s.is_correct])
        total_points = sum(s.points_awarded for s in participant_submissions if s.is_correct)
        
        # Get unique challenges attempted
        attempted_challenges = set(s.challenge_id for s in participant_submissions)
        completed_challenges = set(s.challenge_id for s in participant_submissions if s.is_correct)
        
        # Calculate success rate
        success_rate = (correct_submissions / max(total_submissions, 1)) * 100
        
        # Get participant's rank in campaign leaderboard
        campaign_leaderboard = await submission_repo.get_leaderboard_data(
            campaign_id=campaign_id,
            participant_type=participant_type,
            limit=1000
        )
        
        participant_rank = None
        for i, entry in enumerate(campaign_leaderboard, 1):
            if entry["participant_id"] == participant_id:
                participant_rank = i
                break
        
        # Calculate recent performance (last 7 days)
        from datetime import datetime, timezone, timedelta
        recent_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        recent_submissions = [
            s for s in participant_submissions 
            if s.submission_timestamp >= recent_cutoff
        ]
        recent_correct = len([s for s in recent_submissions if s.is_correct])
        recent_points = sum(s.points_awarded for s in recent_submissions if s.is_correct)
        
        return {
            "campaign_id": str(campaign_id),
            "participant_id": participant_id,
            "participant_type": participant_type,
            "display_name": f"Squad {participant_id}" if participant_type == "squad" else f"Player {participant_id}",
            "overall_performance": {
                "total_points": total_points,
                "campaign_rank": participant_rank,
                "completed_challenges": len(completed_challenges),
                "attempted_challenges": len(attempted_challenges),
                "completion_rate": round((len(completed_challenges) / max(len(attempted_challenges), 1)) * 100, 1)
            },
            "submission_stats": {
                "total_submissions": total_submissions,
                "correct_submissions": correct_submissions,
                "success_rate": round(success_rate, 1),
                "average_points_per_challenge": round(total_points / max(len(completed_challenges), 1), 1)
            },
            "recent_activity": {
                "last_7_days_submissions": len(recent_submissions),
                "last_7_days_correct": recent_correct,
                "last_7_days_points": recent_points,
                "recent_success_rate": round((recent_correct / max(len(recent_submissions), 1)) * 100, 1) if recent_submissions else 0
            },
            "first_submission": min((s.submission_timestamp for s in participant_submissions), default=None),
            "last_submission": max((s.submission_timestamp for s in participant_submissions), default=None)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get participant stats for {participant_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error retrieving participant statistics")