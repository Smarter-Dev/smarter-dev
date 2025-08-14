"""
Submission Repository - Following SOLID principles.

This repository implements the Single Responsibility Principle by handling
only data access operations for submissions, input caching, and rate limiting.
No business logic included.
"""

from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, and_, or_, desc, delete
from sqlalchemy.exc import IntegrityError

from smarter_dev.web.models import (
    ChallengeSubmission, 
    GeneratedInputCache, 
    SubmissionRateLimit,
    Challenge,
    Campaign
)
import logging

logger = logging.getLogger(__name__)


class SubmissionRepository:
    """
    Repository for submission data access operations.
    
    Following SRP: Only handles data persistence and retrieval.
    Following DIP: Depends on abstractions (AsyncSession interface).
    Following ISP: Implements minimal interface with focused methods.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize repository with database session dependency injection."""
        self.session = session
    
    async def create_submission(
        self,
        challenge_id: UUID,
        participant_id: str,
        participant_type: str,
        submitted_result: str,
        is_correct: bool,
        points_awarded: int = 0
    ) -> ChallengeSubmission:
        """
        Create a new challenge submission.
        
        Args:
            challenge_id: Challenge UUID
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            submitted_result: Participant's submitted answer
            is_correct: Whether the submission was correct
            points_awarded: Points awarded for this submission
            
        Returns:
            Created ChallengeSubmission instance
            
        Raises:
            ValueError: If required validation fails
            IntegrityError: If database constraints are violated
        """
        # Basic validation (data layer responsibility)
        if not isinstance(challenge_id, UUID):
            raise ValueError("Invalid challenge_id format")
        
        if not participant_id or not participant_id.strip():
            raise ValueError("Participant ID cannot be empty")
        
        if participant_type not in ["player", "squad"]:
            raise ValueError("Participant type must be 'player' or 'squad'")
        
        if not submitted_result or not submitted_result.strip():
            raise ValueError("Submitted result cannot be empty")
        
        if points_awarded < 0:
            raise ValueError("Points awarded cannot be negative")
        
        try:
            submission = ChallengeSubmission(
                challenge_id=challenge_id,
                participant_id=participant_id.strip(),
                participant_type=participant_type,
                submitted_result=submitted_result.strip(),
                is_correct=is_correct,
                points_awarded=points_awarded
            )
            
            self.session.add(submission)
            await self.session.commit()
            await self.session.refresh(submission)
            
            logger.info(f"Submission created: {submission.id} for challenge {challenge_id} by {participant_id}")
            
            return submission
            
        except IntegrityError as e:
            await self.session.rollback()
            logger.error(f"Failed to create submission: {str(e)}")
            raise
    
    async def get_submission_by_id(self, submission_id: UUID) -> Optional[ChallengeSubmission]:
        """
        Get submission by ID.
        
        Args:
            submission_id: Submission UUID
            
        Returns:
            ChallengeSubmission instance or None if not found
        """
        query = select(ChallengeSubmission).where(ChallengeSubmission.id == submission_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def get_submissions_by_challenge(
        self,
        challenge_id: UUID,
        limit: int = 50,
        offset: int = 0,
        correct_only: bool = False
    ) -> List[ChallengeSubmission]:
        """
        Get submissions for a challenge.
        
        Args:
            challenge_id: Challenge UUID
            limit: Maximum number of submissions to return
            offset: Number of submissions to skip
            correct_only: Whether to return only correct submissions
            
        Returns:
            List of ChallengeSubmission instances ordered by submission time
        """
        query = select(ChallengeSubmission).where(ChallengeSubmission.challenge_id == challenge_id)
        
        if correct_only:
            query = query.where(ChallengeSubmission.is_correct == True)
        
        query = query.order_by(ChallengeSubmission.submission_timestamp.asc())
        query = query.limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_submissions_by_participant(
        self,
        participant_id: str,
        participant_type: str,
        challenge_id: Optional[UUID] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[ChallengeSubmission]:
        """
        Get submissions by a participant.
        
        Args:
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            challenge_id: Optional challenge filter
            limit: Maximum number of submissions to return
            offset: Number of submissions to skip
            
        Returns:
            List of ChallengeSubmission instances ordered by submission time DESC
        """
        query = select(ChallengeSubmission).where(
            and_(
                ChallengeSubmission.participant_id == participant_id,
                ChallengeSubmission.participant_type == participant_type
            )
        )
        
        if challenge_id:
            query = query.where(ChallengeSubmission.challenge_id == challenge_id)
        
        query = query.order_by(ChallengeSubmission.submission_timestamp.desc())
        query = query.limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_successful_submission(
        self,
        challenge_id: UUID,
        participant_id: str,
        participant_type: str
    ) -> Optional[ChallengeSubmission]:
        """
        Get the successful submission for a participant on a challenge.
        
        Args:
            challenge_id: Challenge UUID
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            
        Returns:
            Successful ChallengeSubmission instance or None if not found
        """
        query = select(ChallengeSubmission).where(
            and_(
                ChallengeSubmission.challenge_id == challenge_id,
                ChallengeSubmission.participant_id == participant_id,
                ChallengeSubmission.participant_type == participant_type,
                ChallengeSubmission.is_correct == True
            )
        ).order_by(ChallengeSubmission.submission_timestamp.asc()).limit(1)
        
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def create_or_get_input_cache(
        self,
        challenge_id: UUID,
        participant_id: str,
        participant_type: str,
        input_json: Dict[str, Any],
        expected_result: str
    ) -> GeneratedInputCache:
        """
        Create or retrieve cached input for a participant.
        
        Args:
            challenge_id: Challenge UUID
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            input_json: Generated input data as JSON
            expected_result: Expected result for validation
            
        Returns:
            GeneratedInputCache instance (existing or newly created)
            
        Raises:
            ValueError: If required validation fails
            IntegrityError: If database constraints are violated
        """
        # First try to get existing cache
        existing_cache = await self.get_input_cache(challenge_id, participant_id, participant_type)
        
        if existing_cache and existing_cache.is_valid:
            # Mark first request if not already marked
            if not existing_cache.first_request_timestamp:
                existing_cache.mark_first_request()
                await self.session.commit()
                await self.session.refresh(existing_cache)
            return existing_cache
        
        # Create new cache entry
        if not isinstance(challenge_id, UUID):
            raise ValueError("Invalid challenge_id format")
        
        if not participant_id or not participant_id.strip():
            raise ValueError("Participant ID cannot be empty")
        
        if participant_type not in ["player", "squad"]:
            raise ValueError("Participant type must be 'player' or 'squad'")
        
        if not input_json:
            raise ValueError("Input JSON cannot be empty")
        
        if not expected_result or not expected_result.strip():
            raise ValueError("Expected result cannot be empty")
        
        try:
            # Delete existing invalid cache if it exists
            if existing_cache and not existing_cache.is_valid:
                await self.session.delete(existing_cache)
            
            cache = GeneratedInputCache(
                challenge_id=challenge_id,
                participant_id=participant_id.strip(),
                participant_type=participant_type,
                input_json=input_json,
                expected_result=expected_result.strip()
            )
            
            # Mark first request immediately
            cache.mark_first_request()
            
            self.session.add(cache)
            await self.session.commit()
            await self.session.refresh(cache)
            
            logger.info(f"Input cache created: {cache.id} for challenge {challenge_id} by {participant_id}")
            
            return cache
            
        except IntegrityError as e:
            await self.session.rollback()
            logger.error(f"Failed to create input cache: {str(e)}")
            raise
    
    async def get_input_cache(
        self,
        challenge_id: UUID,
        participant_id: str,
        participant_type: str
    ) -> Optional[GeneratedInputCache]:
        """
        Get cached input for a participant.
        
        Args:
            challenge_id: Challenge UUID
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            
        Returns:
            GeneratedInputCache instance or None if not found
        """
        query = select(GeneratedInputCache).where(
            and_(
                GeneratedInputCache.challenge_id == challenge_id,
                GeneratedInputCache.participant_id == participant_id,
                GeneratedInputCache.participant_type == participant_type
            )
        )
        
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def invalidate_input_cache(
        self,
        challenge_id: UUID,
        participant_id: Optional[str] = None,
        participant_type: Optional[str] = None
    ) -> int:
        """
        Invalidate cached inputs for a challenge.
        
        Args:
            challenge_id: Challenge UUID
            participant_id: Optional specific participant ID
            participant_type: Optional specific participant type
            
        Returns:
            Number of cache entries invalidated
        """
        query = select(GeneratedInputCache).where(
            and_(
                GeneratedInputCache.challenge_id == challenge_id,
                GeneratedInputCache.is_valid == True
            )
        )
        
        if participant_id and participant_type:
            query = query.where(
                and_(
                    GeneratedInputCache.participant_id == participant_id,
                    GeneratedInputCache.participant_type == participant_type
                )
            )
        
        result = await self.session.execute(query)
        cache_entries = result.scalars().all()
        
        invalidated_count = 0
        for cache_entry in cache_entries:
            cache_entry.invalidate()
            invalidated_count += 1
        
        if invalidated_count > 0:
            await self.session.commit()
            logger.info(f"Invalidated {invalidated_count} cache entries for challenge {challenge_id}")
        
        return invalidated_count
    
    async def record_rate_limit(
        self,
        participant_id: str,
        participant_type: str
    ) -> SubmissionRateLimit:
        """
        Record a submission attempt for rate limiting.
        
        Args:
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            
        Returns:
            Created SubmissionRateLimit instance
        """
        if not participant_id or not participant_id.strip():
            raise ValueError("Participant ID cannot be empty")
        
        if participant_type not in ["player", "squad"]:
            raise ValueError("Participant type must be 'player' or 'squad'")
        
        try:
            rate_limit = SubmissionRateLimit(
                participant_id=participant_id.strip(),
                participant_type=participant_type
            )
            
            self.session.add(rate_limit)
            await self.session.commit()
            await self.session.refresh(rate_limit)
            
            return rate_limit
            
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Failed to record rate limit: {str(e)}")
            raise
    
    async def check_rate_limit(
        self,
        participant_id: str,
        participant_type: str,
        max_per_minute: int = 1,
        max_per_5_minutes: int = 3,
        current_time: Optional[datetime] = None
    ) -> bool:
        """
        Check if participant is rate limited.
        
        Args:
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            max_per_minute: Maximum submissions per minute
            max_per_5_minutes: Maximum submissions per 5 minutes
            current_time: Current time (defaults to now)
            
        Returns:
            True if rate limited, False if submission allowed
        """
        return await SubmissionRateLimit.is_rate_limited(
            self.session,
            participant_id,
            participant_type,
            max_per_minute,
            max_per_5_minutes,
            current_time
        )
    
    async def cleanup_old_rate_limits(self, days_to_keep: int = 7) -> int:
        """
        Clean up old rate limit entries.
        
        Args:
            days_to_keep: Number of days of rate limit data to retain
            
        Returns:
            Number of rate limit entries deleted
        """
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
        
        # Count entries to be deleted
        count_query = select(func.count(SubmissionRateLimit.id)).where(
            SubmissionRateLimit.submission_timestamp < cutoff_date
        )
        count_result = await self.session.execute(count_query)
        count_to_delete = count_result.scalar() or 0
        
        if count_to_delete > 0:
            # Delete old rate limit entries
            delete_stmt = delete(SubmissionRateLimit).where(
                SubmissionRateLimit.submission_timestamp < cutoff_date
            )
            
            await self.session.execute(delete_stmt)
            await self.session.commit()
            
            logger.info(f"Cleaned up {count_to_delete} old rate limit entries")
        
        return count_to_delete
    
    async def get_leaderboard_data(
        self,
        challenge_id: Optional[UUID] = None,
        campaign_id: Optional[UUID] = None,
        participant_type: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Get leaderboard data for challenges or campaigns.
        
        Args:
            challenge_id: Optional specific challenge
            campaign_id: Optional specific campaign
            participant_type: Optional filter by participant type
            limit: Maximum number of entries to return
            
        Returns:
            List of leaderboard entries with participant info and scores
        """
        # Build base query for successful submissions
        query = select(
            ChallengeSubmission.participant_id,
            ChallengeSubmission.participant_type,
            func.sum(ChallengeSubmission.points_awarded).label('total_points'),
            func.count(ChallengeSubmission.id).label('completed_challenges'),
            func.min(ChallengeSubmission.submission_timestamp).label('first_completion')
        ).where(ChallengeSubmission.is_correct == True)
        
        if challenge_id:
            query = query.where(ChallengeSubmission.challenge_id == challenge_id)
        
        if campaign_id:
            # Join with challenges to filter by campaign
            query = query.join(Challenge, ChallengeSubmission.challenge_id == Challenge.id)
            query = query.where(Challenge.campaign_id == campaign_id)
        
        if participant_type:
            query = query.where(ChallengeSubmission.participant_type == participant_type)
        
        query = query.group_by(
            ChallengeSubmission.participant_id,
            ChallengeSubmission.participant_type
        ).order_by(
            func.sum(ChallengeSubmission.points_awarded).desc(),
            func.min(ChallengeSubmission.submission_timestamp).asc()
        ).limit(limit)
        
        result = await self.session.execute(query)
        rows = result.fetchall()
        
        leaderboard = []
        for row in rows:
            leaderboard.append({
                'participant_id': row.participant_id,
                'participant_type': row.participant_type,
                'total_points': int(row.total_points) if row.total_points else 0,
                'completed_challenges': int(row.completed_challenges),
                'first_completion': row.first_completion
            })
        
        return leaderboard
    
    async def get_submission_statistics(
        self,
        challenge_id: Optional[UUID] = None,
        campaign_id: Optional[UUID] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Get submission statistics.
        
        Args:
            challenge_id: Optional challenge filter
            campaign_id: Optional campaign filter
            date_from: Optional start date
            date_to: Optional end date
            
        Returns:
            Dictionary with submission statistics
        """
        base_query = select(ChallengeSubmission)
        
        if challenge_id:
            base_query = base_query.where(ChallengeSubmission.challenge_id == challenge_id)
        
        if campaign_id:
            base_query = base_query.join(Challenge, ChallengeSubmission.challenge_id == Challenge.id)
            base_query = base_query.where(Challenge.campaign_id == campaign_id)
        
        if date_from:
            base_query = base_query.where(ChallengeSubmission.submission_timestamp >= date_from)
        
        if date_to:
            base_query = base_query.where(ChallengeSubmission.submission_timestamp <= date_to)
        
        # Total submissions
        total_query = select(func.count(ChallengeSubmission.id)).select_from(base_query.subquery())
        total_result = await self.session.execute(total_query)
        total_submissions = total_result.scalar() or 0
        
        # Correct submissions
        correct_query = select(func.count(ChallengeSubmission.id)).select_from(
            base_query.where(ChallengeSubmission.is_correct == True).subquery()
        )
        correct_result = await self.session.execute(correct_query)
        correct_submissions = correct_result.scalar() or 0
        
        # Success rate
        success_rate = (correct_submissions / total_submissions * 100) if total_submissions > 0 else 0
        
        # Unique participants
        unique_participants_query = select(
            func.count(func.distinct(
                func.concat(ChallengeSubmission.participant_id, ':', ChallengeSubmission.participant_type)
            ))
        ).select_from(base_query.subquery())
        unique_participants_result = await self.session.execute(unique_participants_query)
        unique_participants = unique_participants_result.scalar() or 0
        
        return {
            "total_submissions": total_submissions,
            "correct_submissions": correct_submissions,
            "success_rate": round(success_rate, 2),
            "unique_participants": unique_participants,
            "date_range": {
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None
            }
        }
    
    async def get_campaign_submissions(
        self,
        campaign_id: UUID,
        participant_id: Optional[str] = None,
        challenge_id: Optional[str] = None,
        correct_only: bool = False,
        limit: int = 50,
        offset: int = 0
    ) -> List[ChallengeSubmission]:
        """
        Get submissions for a campaign with optional filters.
        
        Args:
            campaign_id: Campaign UUID
            participant_id: Optional participant filter
            challenge_id: Optional challenge filter
            correct_only: Whether to return only correct submissions
            limit: Maximum number of submissions to return
            offset: Number of submissions to skip
            
        Returns:
            List of ChallengeSubmission instances ordered by submission time DESC
        """
        # Join with challenges to filter by campaign
        query = select(ChallengeSubmission).join(
            Challenge, ChallengeSubmission.challenge_id == Challenge.id
        ).where(Challenge.campaign_id == campaign_id)
        
        if participant_id:
            query = query.where(ChallengeSubmission.participant_id == participant_id)
        
        if challenge_id:
            # Convert string to UUID if needed
            if isinstance(challenge_id, str):
                challenge_id = UUID(challenge_id)
            query = query.where(ChallengeSubmission.challenge_id == challenge_id)
        
        if correct_only:
            query = query.where(ChallengeSubmission.is_correct == True)
        
        query = query.order_by(ChallengeSubmission.submission_timestamp.desc())
        query = query.limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_campaign_statistics(self, campaign_id: UUID) -> Dict[str, Any]:
        """
        Get comprehensive statistics for a campaign.
        
        Args:
            campaign_id: Campaign UUID
            
        Returns:
            Dictionary with comprehensive campaign statistics
        """
        # Get basic submission statistics
        stats = await self.get_submission_statistics(campaign_id=campaign_id)
        
        # Get challenge count for the campaign
        challenge_count_query = select(func.count(Challenge.id)).where(Challenge.campaign_id == campaign_id)
        challenge_count_result = await self.session.execute(challenge_count_query)
        total_challenges = challenge_count_result.scalar() or 0
        
        # Get released challenge count
        released_query = select(func.count(Challenge.id)).where(
            and_(
                Challenge.campaign_id == campaign_id,
                Challenge.release_date.isnot(None),
                Challenge.release_date <= datetime.now(timezone.utc)
            )
        )
        released_result = await self.session.execute(released_query)
        released_challenges = released_result.scalar() or 0
        
        # Get leaderboard data for top participants
        top_participants = await self.get_leaderboard_data(
            campaign_id=campaign_id, 
            limit=10
        )
        
        # Get challenge completion rates
        completion_rates = []
        challenge_ids_query = select(Challenge.id, Challenge.title).where(Challenge.campaign_id == campaign_id)
        challenge_ids_result = await self.session.execute(challenge_ids_query)
        challenges = challenge_ids_result.fetchall()
        
        for challenge_row in challenges:
            challenge_stats = await self.get_submission_statistics(challenge_id=challenge_row.id)
            completion_rate = (challenge_stats["correct_submissions"] / max(challenge_stats["unique_participants"], 1)) * 100
            completion_rates.append({
                "challenge_id": str(challenge_row.id),
                "challenge_title": challenge_row.title,
                "completion_rate": round(completion_rate, 1),
                "total_submissions": challenge_stats["total_submissions"],
                "unique_participants": challenge_stats["unique_participants"]
            })
        
        # Calculate average points per participant
        avg_points = 0
        if stats["unique_participants"] > 0:
            total_points_query = select(func.sum(ChallengeSubmission.points_awarded)).join(
                Challenge, ChallengeSubmission.challenge_id == Challenge.id
            ).where(
                and_(
                    Challenge.campaign_id == campaign_id,
                    ChallengeSubmission.is_correct == True
                )
            )
            total_points_result = await self.session.execute(total_points_query)
            total_points = total_points_result.scalar() or 0
            avg_points = total_points / stats["unique_participants"]
        
        return {
            "campaign_id": campaign_id,
            "total_challenges": total_challenges,
            "released_challenges": released_challenges,
            "total_participants": stats["unique_participants"],
            "total_submissions": stats["total_submissions"],
            "correct_submissions": stats["correct_submissions"],
            "success_rate": stats["success_rate"],
            "avg_points_per_participant": round(avg_points, 1),
            "top_participants": top_participants,
            "challenge_completion_rates": completion_rates
        }