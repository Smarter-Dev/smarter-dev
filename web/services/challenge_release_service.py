"""
Challenge Release Service - Following SOLID principles.

This service implements challenge unlock timing logic for campaigns,
determining which challenges are available to participants based on
the campaign's release schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import List, Optional, Protocol
from uuid import UUID
import logging

logger = logging.getLogger(__name__)


class ChallengeReleaseStatus(Enum):
    """Status of a challenge release."""
    RELEASED = "released"
    PENDING = "pending"


@dataclass
class ChallengeReleaseInfo:
    """
    Information about a challenge's release status.
    
    Contains details about when a challenge is or will be available
    to participants.
    """
    challenge_id: UUID
    order_position: int
    title: str
    status: ChallengeReleaseStatus
    release_time: datetime
    time_until_release: Optional[timedelta] = None


class CampaignProtocol(Protocol):
    """Protocol defining the interface for campaign objects."""
    id: UUID
    start_date: datetime
    release_delay_minutes: int
    state: str


class ChallengeProtocol(Protocol):
    """Protocol defining the interface for challenge objects."""
    id: UUID
    order_position: int
    title: str
    campaign_id: UUID


class CampaignRepositoryProtocol(Protocol):
    """Protocol defining the interface for campaign repository."""
    async def get_campaign_by_id(self, campaign_id: UUID) -> Optional[CampaignProtocol]:
        """Get campaign by ID."""
        pass


class ChallengeRepositoryProtocol(Protocol):
    """Protocol defining the interface for challenge repository."""
    async def get_challenges_by_campaign(
        self, 
        campaign_id: UUID,
        order_by_position: bool = True
    ) -> List[ChallengeProtocol]:
        """Get challenges for a campaign."""
        pass


class ChallengeReleaseService:
    """
    Service for managing challenge release timing and availability.
    
    Following SRP: Only handles challenge release logic.
    Following DIP: Depends on abstractions (repository protocols).
    Following OCP: Extensible for different release strategies.
    """
    
    def __init__(
        self,
        campaign_repository: CampaignRepositoryProtocol,
        challenge_repository: ChallengeRepositoryProtocol
    ):
        """
        Initialize service with repository dependencies.
        
        Args:
            campaign_repository: Repository for campaign data access
            challenge_repository: Repository for challenge data access
        """
        self.campaign_repository = campaign_repository
        self.challenge_repository = challenge_repository
    
    async def get_challenge_release_schedule(
        self,
        campaign_id: UUID,
        current_time: Optional[datetime] = None
    ) -> List[ChallengeReleaseInfo]:
        """
        Get the complete release schedule for a campaign's challenges.
        
        Args:
            campaign_id: Campaign UUID
            current_time: Current time (defaults to now)
            
        Returns:
            List of ChallengeReleaseInfo ordered by challenge position
            
        Raises:
            ValueError: If campaign_id is None or campaign not found
        """
        # Input validation
        if campaign_id is None:
            raise ValueError("Campaign ID cannot be None")
        
        if current_time is None:
            current_time = datetime.now(timezone.utc)
        
        if current_time.tzinfo is None:
            raise ValueError("Current time must be timezone-aware")
        
        # Get campaign
        campaign = await self.campaign_repository.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign not found: {campaign_id}")
        
        # Get challenges
        challenges = await self.challenge_repository.get_challenges_by_campaign(
            campaign_id, order_by_position=True
        )
        
        # Build release schedule
        schedule = []
        for challenge in challenges:
            release_info = self._calculate_challenge_release_info(
                challenge=challenge,
                campaign=campaign,
                current_time=current_time
            )
            schedule.append(release_info)
        
        logger.info(
            f"Generated release schedule for campaign {campaign_id}: "
            f"{len(schedule)} challenges, "
            f"{len([info for info in schedule if info.status == ChallengeReleaseStatus.RELEASED])} released"
        )
        
        return schedule
    
    async def get_available_challenges(
        self,
        campaign_id: UUID,
        current_time: Optional[datetime] = None
    ) -> List[ChallengeProtocol]:
        """
        Get challenges that are currently available to participants.
        
        Args:
            campaign_id: Campaign UUID
            current_time: Current time (defaults to now)
            
        Returns:
            List of available challenges ordered by position
            
        Raises:
            ValueError: If campaign_id is None or campaign not found
        """
        # Get campaign
        campaign = await self.campaign_repository.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign not found: {campaign_id}")
        
        # For draft campaigns, no challenges are available
        if campaign.state == "draft":
            return []
        
        # For completed campaigns, all challenges are available
        if campaign.state == "completed":
            return await self.challenge_repository.get_challenges_by_campaign(
                campaign_id, order_by_position=True
            )
        
        # For active campaigns, check release timing
        if current_time is None:
            current_time = datetime.now(timezone.utc)
        
        challenges = await self.challenge_repository.get_challenges_by_campaign(
            campaign_id, order_by_position=True
        )
        
        available_challenges = []
        for challenge in challenges:
            if await self.is_challenge_released(
                challenge=challenge,
                campaign_start_date=campaign.start_date,
                release_delay_minutes=campaign.release_delay_minutes,
                current_time=current_time
            ):
                available_challenges.append(challenge)
        
        logger.info(
            f"Found {len(available_challenges)} available challenges "
            f"out of {len(challenges)} total for campaign {campaign_id}"
        )
        
        return available_challenges
    
    async def get_next_challenge_release(
        self,
        campaign_id: UUID,
        current_time: Optional[datetime] = None
    ) -> Optional[ChallengeReleaseInfo]:
        """
        Get information about the next challenge to be released.
        
        Args:
            campaign_id: Campaign UUID
            current_time: Current time (defaults to now)
            
        Returns:
            ChallengeReleaseInfo for next release, or None if all released
            
        Raises:
            ValueError: If campaign_id is None or campaign not found
        """
        schedule = await self.get_challenge_release_schedule(campaign_id, current_time)
        
        # Find first pending challenge
        for release_info in schedule:
            if release_info.status == ChallengeReleaseStatus.PENDING:
                return release_info
        
        return None  # All challenges are released
    
    async def is_challenge_released(
        self,
        challenge: ChallengeProtocol,
        campaign_start_date: datetime,
        release_delay_minutes: int,
        current_time: Optional[datetime] = None
    ) -> bool:
        """
        Check if a specific challenge has been released.
        
        Args:
            challenge: Challenge to check
            campaign_start_date: When the campaign started
            release_delay_minutes: Minutes between challenge releases
            current_time: Current time (defaults to now)
            
        Returns:
            True if challenge is released, False otherwise
        """
        if current_time is None:
            current_time = datetime.now(timezone.utc)
        
        release_time = self.get_challenge_release_time(
            campaign_start_date=campaign_start_date,
            challenge_order_position=challenge.order_position,
            release_delay_minutes=release_delay_minutes
        )
        
        return current_time >= release_time
    
    def get_challenge_release_time(
        self,
        campaign_start_date: datetime,
        challenge_order_position: int,
        release_delay_minutes: int
    ) -> datetime:
        """
        Calculate when a challenge will be released.
        
        Args:
            campaign_start_date: When the campaign started
            challenge_order_position: Challenge position (1-based)
            release_delay_minutes: Minutes between challenge releases
            
        Returns:
            Datetime when the challenge will be released
        """
        # First challenge (position 1) releases immediately when campaign starts
        # Subsequent challenges release after (position - 1) * delay minutes
        delay_minutes = (challenge_order_position - 1) * release_delay_minutes
        
        return campaign_start_date + timedelta(minutes=delay_minutes)
    
    def _calculate_challenge_release_info(
        self,
        challenge: ChallengeProtocol,
        campaign: CampaignProtocol,
        current_time: datetime
    ) -> ChallengeReleaseInfo:
        """
        Calculate release information for a specific challenge.
        
        Args:
            challenge: Challenge to analyze
            campaign: Campaign the challenge belongs to
            current_time: Current time
            
        Returns:
            ChallengeReleaseInfo with calculated status and timing
        """
        release_time = self.get_challenge_release_time(
            campaign_start_date=campaign.start_date,
            challenge_order_position=challenge.order_position,
            release_delay_minutes=campaign.release_delay_minutes
        )
        
        if current_time >= release_time:
            # Challenge is released
            status = ChallengeReleaseStatus.RELEASED
            time_until_release = None
        else:
            # Challenge is pending
            status = ChallengeReleaseStatus.PENDING
            time_until_release = release_time - current_time
        
        return ChallengeReleaseInfo(
            challenge_id=challenge.id,
            order_position=challenge.order_position,
            title=challenge.title,
            status=status,
            release_time=release_time,
            time_until_release=time_until_release
        )
    
    async def get_challenges_released_since(
        self,
        campaign_id: UUID,
        since_time: datetime,
        current_time: Optional[datetime] = None
    ) -> List[ChallengeProtocol]:
        """
        Get challenges that were released since a specific time.
        
        Useful for notifications and real-time updates.
        
        Args:
            campaign_id: Campaign UUID
            since_time: Time to check releases since
            current_time: Current time (defaults to now)
            
        Returns:
            List of challenges released since the specified time
            
        Raises:
            ValueError: If campaign_id is None or campaign not found
        """
        # Get campaign
        campaign = await self.campaign_repository.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign not found: {campaign_id}")
        
        if current_time is None:
            current_time = datetime.now(timezone.utc)
        
        # Get all challenges
        challenges = await self.challenge_repository.get_challenges_by_campaign(
            campaign_id, order_by_position=True
        )
        
        newly_released = []
        for challenge in challenges:
            release_time = self.get_challenge_release_time(
                campaign_start_date=campaign.start_date,
                challenge_order_position=challenge.order_position,
                release_delay_minutes=campaign.release_delay_minutes
            )
            
            # Check if released between since_time and current_time
            if since_time < release_time <= current_time:
                newly_released.append(challenge)
        
        logger.info(
            f"Found {len(newly_released)} challenges released since {since_time} "
            f"for campaign {campaign_id}"
        )
        
        return newly_released
    
    async def get_campaign_release_summary(
        self,
        campaign_id: UUID,
        current_time: Optional[datetime] = None
    ) -> dict:
        """
        Get a summary of the campaign's release status.
        
        Args:
            campaign_id: Campaign UUID
            current_time: Current time (defaults to now)
            
        Returns:
            Dictionary with release summary statistics
            
        Raises:
            ValueError: If campaign_id is None or campaign not found
        """
        schedule = await self.get_challenge_release_schedule(campaign_id, current_time)
        
        released_count = len([
            info for info in schedule 
            if info.status == ChallengeReleaseStatus.RELEASED
        ])
        pending_count = len([
            info for info in schedule 
            if info.status == ChallengeReleaseStatus.PENDING
        ])
        
        next_release = await self.get_next_challenge_release(campaign_id, current_time)
        
        return {
            "total_challenges": len(schedule),
            "released_challenges": released_count,
            "pending_challenges": pending_count,
            "completion_percentage": (released_count / len(schedule) * 100) if schedule else 0,
            "next_release": {
                "challenge_title": next_release.title if next_release else None,
                "release_time": next_release.release_time if next_release else None,
                "time_until_release": next_release.time_until_release if next_release else None
            } if next_release else None
        }