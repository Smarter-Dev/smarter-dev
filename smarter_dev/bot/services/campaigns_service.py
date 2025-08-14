"""Campaign challenges service for the Discord bot.

This service handles all campaign challenge operations including campaign management,
challenge retrieval, submission processing, and leaderboard functionality.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from uuid import UUID

from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.exceptions import ServiceError, ValidationError
from smarter_dev.bot.services.models import ServiceHealth

logger = logging.getLogger(__name__)


class CampaignInfo:
    """Campaign information model."""
    
    def __init__(
        self,
        id: UUID,
        title: str,
        description: Optional[str],
        guild_id: Optional[str],
        participant_type: str,
        status: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        challenge_release_delay_hours: int,
        scoring_strategy: str,
        scoring_config: Dict[str, Any],
        created_at: datetime,
        updated_at: datetime
    ):
        self.id = id
        self.title = title
        self.description = description
        self.guild_id = guild_id
        self.participant_type = participant_type
        self.status = status
        self.start_date = start_date
        self.end_date = end_date
        self.challenge_release_delay_hours = challenge_release_delay_hours
        self.scoring_strategy = scoring_strategy
        self.scoring_config = scoring_config
        self.created_at = created_at
        self.updated_at = updated_at


class ChallengeInfo:
    """Challenge information model."""
    
    def __init__(
        self,
        id: UUID,
        campaign_id: UUID,
        title: str,
        description: Optional[str],
        difficulty: str,
        problem_statement: str,
        expected_output_format: Optional[str],
        time_limit_minutes: Optional[int],
        memory_limit_mb: Optional[int],
        order_index: int,
        release_date: Optional[datetime],
        created_at: datetime,
        updated_at: datetime
    ):
        self.id = id
        self.campaign_id = campaign_id
        self.title = title
        self.description = description
        self.difficulty = difficulty
        self.problem_statement = problem_statement
        self.expected_output_format = expected_output_format
        self.time_limit_minutes = time_limit_minutes
        self.memory_limit_mb = memory_limit_mb
        self.order_index = order_index
        self.release_date = release_date
        self.created_at = created_at
        self.updated_at = updated_at


class SubmissionInfo:
    """Submission information model."""
    
    def __init__(
        self,
        id: UUID,
        challenge_id: UUID,
        participant_id: str,
        participant_type: str,
        submitted_result: str,
        is_correct: bool,
        points_awarded: int,
        submission_timestamp: datetime
    ):
        self.id = id
        self.challenge_id = challenge_id
        self.participant_id = participant_id
        self.participant_type = participant_type
        self.submitted_result = submitted_result
        self.is_correct = is_correct
        self.points_awarded = points_awarded
        self.submission_timestamp = submission_timestamp


class CampaignStatsInfo:
    """Campaign statistics information model."""
    
    def __init__(
        self,
        campaign_id: UUID,
        total_challenges: int,
        released_challenges: int,
        total_participants: int,
        total_submissions: int,
        correct_submissions: int,
        success_rate: float,
        avg_points_per_participant: float,
        top_participants: List[Dict[str, Any]],
        challenge_completion_rates: List[Dict[str, Any]]
    ):
        self.campaign_id = campaign_id
        self.total_challenges = total_challenges
        self.released_challenges = released_challenges
        self.total_participants = total_participants
        self.total_submissions = total_submissions
        self.correct_submissions = correct_submissions
        self.success_rate = success_rate
        self.avg_points_per_participant = avg_points_per_participant
        self.top_participants = top_participants
        self.challenge_completion_rates = challenge_completion_rates


class CampaignsService(BaseService):
    """Service for managing campaign challenges through Discord bot."""
    
    def __init__(self, api_client, cache_manager=None):
        super().__init__(api_client, cache_manager)
        self.cache_prefix = "campaigns"
        
    async def initialize(self) -> None:
        """Initialize the campaigns service."""
        logger.info("Initializing campaigns service...")
        
        try:
            await super().initialize()
            logger.info("Campaigns service initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize campaigns service: {e}")
            raise ServiceError(f"Service initialization failed: {e}")
    
    async def health_check(self) -> ServiceHealth:
        """Check campaigns service health."""
        try:
            # Test API connection by trying to list campaigns
            await self._api_client.get("/campaigns/", params={"size": 1})
            return ServiceHealth(
                service_name="campaigns",
                is_healthy=True,
                details={"api_connection": "ok"}
            )
        except Exception as e:
            logger.error(f"Campaigns service health check failed: {e}")
            return ServiceHealth(
                service_name="campaigns",
                is_healthy=False,
                details={"error": str(e)}
            )
    
    async def list_campaigns(
        self,
        guild_id: str,
        status: Optional[str] = None,
        participant_type: Optional[str] = None,
        page: int = 1,
        size: int = 20
    ) -> List[CampaignInfo]:
        """List campaigns for a guild."""
        try:
            params = {
                "guild_id": guild_id,
                "page": page,
                "size": size
            }
            
            if status:
                params["status"] = status
            if participant_type:
                params["participant_type"] = participant_type
            
            response = await self._api_client.get("/campaigns/", params=params)
            campaigns_data = response.get("items", [])
            
            campaigns = []
            for campaign_data in campaigns_data:
                campaigns.append(CampaignInfo(
                    id=UUID(campaign_data["id"]),
                    title=campaign_data["title"],
                    description=campaign_data.get("description"),
                    guild_id=campaign_data.get("guild_id"),
                    participant_type=campaign_data["participant_type"],
                    status=campaign_data["status"],
                    start_date=datetime.fromisoformat(campaign_data["start_date"].replace("Z", "+00:00")) if campaign_data.get("start_date") else None,
                    end_date=datetime.fromisoformat(campaign_data["end_date"].replace("Z", "+00:00")) if campaign_data.get("end_date") else None,
                    challenge_release_delay_hours=campaign_data["challenge_release_delay_hours"],
                    scoring_strategy=campaign_data["scoring_strategy"],
                    scoring_config=campaign_data["scoring_config"],
                    created_at=datetime.fromisoformat(campaign_data["created_at"].replace("Z", "+00:00")),
                    updated_at=datetime.fromisoformat(campaign_data["updated_at"].replace("Z", "+00:00"))
                ))
            
            return campaigns
            
        except Exception as e:
            logger.error(f"Failed to list campaigns for guild {guild_id}: {e}")
            raise ServiceError(f"Failed to retrieve campaigns: {e}")
    
    async def get_campaign(self, campaign_id: str) -> Optional[CampaignInfo]:
        """Get a specific campaign by ID."""
        try:
            response = await self._api_client.get(f"/campaigns/{campaign_id}")
            
            return CampaignInfo(
                id=UUID(response["id"]),
                title=response["title"],
                description=response.get("description"),
                guild_id=response.get("guild_id"),
                participant_type=response["participant_type"],
                status=response["status"],
                start_date=datetime.fromisoformat(response["start_date"].replace("Z", "+00:00")) if response.get("start_date") else None,
                end_date=datetime.fromisoformat(response["end_date"].replace("Z", "+00:00")) if response.get("end_date") else None,
                challenge_release_delay_hours=response["challenge_release_delay_hours"],
                scoring_strategy=response["scoring_strategy"],
                scoring_config=response["scoring_config"],
                created_at=datetime.fromisoformat(response["created_at"].replace("Z", "+00:00")),
                updated_at=datetime.fromisoformat(response["updated_at"].replace("Z", "+00:00"))
            )
            
        except Exception as e:
            logger.error(f"Failed to get campaign {campaign_id}: {e}")
            if "not found" in str(e).lower():
                return None
            raise ServiceError(f"Failed to retrieve campaign: {e}")
    
    async def list_challenges(
        self,
        campaign_id: str,
        include_unreleased: bool = False
    ) -> List[ChallengeInfo]:
        """List challenges for a campaign."""
        try:
            params = {"include_unreleased": include_unreleased}
            response = await self._api_client.get(f"/campaigns/{campaign_id}/challenges", params=params)
            
            challenges = []
            for challenge_data in response:
                challenges.append(ChallengeInfo(
                    id=UUID(challenge_data["id"]),
                    campaign_id=UUID(challenge_data["campaign_id"]),
                    title=challenge_data["title"],
                    description=challenge_data.get("description"),
                    difficulty=challenge_data["difficulty"],
                    problem_statement=challenge_data["problem_statement"],
                    expected_output_format=challenge_data.get("expected_output_format"),
                    time_limit_minutes=challenge_data.get("time_limit_minutes"),
                    memory_limit_mb=challenge_data.get("memory_limit_mb"),
                    order_index=challenge_data["order_index"],
                    release_date=datetime.fromisoformat(challenge_data["release_date"].replace("Z", "+00:00")) if challenge_data.get("release_date") else None,
                    created_at=datetime.fromisoformat(challenge_data["created_at"].replace("Z", "+00:00")),
                    updated_at=datetime.fromisoformat(challenge_data["updated_at"].replace("Z", "+00:00"))
                ))
            
            return challenges
            
        except Exception as e:
            logger.error(f"Failed to list challenges for campaign {campaign_id}: {e}")
            raise ServiceError(f"Failed to retrieve challenges: {e}")
    
    async def get_challenge(self, campaign_id: str, challenge_id: str) -> Optional[ChallengeInfo]:
        """Get a specific challenge by ID."""
        try:
            response = await self._api_client.get(f"/campaigns/{campaign_id}/challenges/{challenge_id}")
            
            return ChallengeInfo(
                id=UUID(response["id"]),
                campaign_id=UUID(response["campaign_id"]),
                title=response["title"],
                description=response.get("description"),
                difficulty=response["difficulty"],
                problem_statement=response["problem_statement"],
                expected_output_format=response.get("expected_output_format"),
                time_limit_minutes=response.get("time_limit_minutes"),
                memory_limit_mb=response.get("memory_limit_mb"),
                order_index=response["order_index"],
                release_date=datetime.fromisoformat(response["release_date"].replace("Z", "+00:00")) if response.get("release_date") else None,
                created_at=datetime.fromisoformat(response["created_at"].replace("Z", "+00:00")),
                updated_at=datetime.fromisoformat(response["updated_at"].replace("Z", "+00:00"))
            )
            
        except Exception as e:
            logger.error(f"Failed to get challenge {challenge_id}: {e}")
            if "not found" in str(e).lower():
                return None
            raise ServiceError(f"Failed to retrieve challenge: {e}")
    
    async def submit_challenge_answer(
        self,
        campaign_id: str,
        challenge_id: str,
        participant_id: str,
        submitted_result: str
    ) -> SubmissionInfo:
        """Submit an answer for a challenge."""
        try:
            data = {"submitted_result": submitted_result}
            response = await self._api_client.post(
                f"/campaigns/{campaign_id}/challenges/{challenge_id}/submit",
                json=data,
                headers={"X-User-ID": participant_id}
            )
            
            return SubmissionInfo(
                id=UUID(response["id"]),
                challenge_id=UUID(response["challenge_id"]),
                participant_id=response["participant_id"],
                participant_type=response["participant_type"],
                submitted_result=response["submitted_result"],
                is_correct=response["is_correct"],
                points_awarded=response["points_awarded"],
                submission_timestamp=datetime.fromisoformat(response["submission_timestamp"].replace("Z", "+00:00"))
            )
            
        except Exception as e:
            logger.error(f"Failed to submit answer for challenge {challenge_id}: {e}")
            raise ServiceError(f"Failed to submit answer: {e}")
    
    async def get_campaign_stats(self, campaign_id: str) -> CampaignStatsInfo:
        """Get campaign statistics."""
        try:
            response = await self._api_client.get(f"/campaigns/{campaign_id}/stats")
            
            return CampaignStatsInfo(
                campaign_id=UUID(response["campaign_id"]),
                total_challenges=response["total_challenges"],
                released_challenges=response["released_challenges"],
                total_participants=response["total_participants"],
                total_submissions=response["total_submissions"],
                correct_submissions=response["correct_submissions"],
                success_rate=response["success_rate"],
                avg_points_per_participant=response["avg_points_per_participant"],
                top_participants=response["top_participants"],
                challenge_completion_rates=response["challenge_completion_rates"]
            )
            
        except Exception as e:
            logger.error(f"Failed to get campaign stats {campaign_id}: {e}")
            raise ServiceError(f"Failed to retrieve campaign statistics: {e}")
    
    async def get_user_submissions(
        self,
        campaign_id: str,
        user_id: str
    ) -> List[SubmissionInfo]:
        """Get user's submissions for a campaign."""
        try:
            response = await self._api_client.get(
                f"/campaigns/{campaign_id}/submissions",
                params={"participant_id": user_id},
                headers={"X-User-ID": user_id}
            )
            
            submissions = []
            for submission_data in response:
                submissions.append(SubmissionInfo(
                    id=UUID(submission_data["id"]),
                    challenge_id=UUID(submission_data["challenge_id"]),
                    participant_id=submission_data["participant_id"],
                    participant_type=submission_data["participant_type"],
                    submitted_result=submission_data["submitted_result"],
                    is_correct=submission_data["is_correct"],
                    points_awarded=submission_data["points_awarded"],
                    submission_timestamp=datetime.fromisoformat(submission_data["submission_timestamp"].replace("Z", "+00:00"))
                ))
            
            return submissions
            
        except Exception as e:
            logger.error(f"Failed to get user submissions for campaign {campaign_id}: {e}")
            raise ServiceError(f"Failed to retrieve user submissions: {e}")
    
    async def get_campaign_leaderboard(
        self,
        campaign_id: str,
        participant_type: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get campaign leaderboard data."""
        try:
            params = {"limit": limit}
            if participant_type:
                params["participant_type"] = participant_type
            
            response = await self._api_client.get(f"/campaigns/{campaign_id}/leaderboard", params=params)
            return response.get("leaderboard", [])
            
        except Exception as e:
            logger.error(f"Failed to get campaign leaderboard {campaign_id}: {e}")
            raise ServiceError(f"Failed to retrieve campaign leaderboard: {e}")
    
    async def get_challenge_leaderboard(
        self,
        campaign_id: str,
        challenge_id: str,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get challenge-specific leaderboard data."""
        try:
            params = {"limit": limit}
            
            response = await self._api_client.get(
                f"/campaigns/{campaign_id}/challenges/{challenge_id}/leaderboard", 
                params=params
            )
            return response.get("leaderboard", [])
            
        except Exception as e:
            logger.error(f"Failed to get challenge leaderboard {challenge_id}: {e}")
            raise ServiceError(f"Failed to retrieve challenge leaderboard: {e}")
    
    async def get_participant_stats(
        self,
        campaign_id: str,
        participant_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get detailed participant statistics."""
        try:
            response = await self._api_client.get(
                f"/campaigns/{campaign_id}/participant/{participant_id}/stats"
            )
            return response
            
        except Exception as e:
            logger.error(f"Failed to get participant stats for {participant_id}: {e}")
            if "not found" in str(e).lower():
                return None
            raise ServiceError(f"Failed to retrieve participant statistics: {e}")
    
    async def create_campaign(
        self,
        title: str,
        description: str,
        guild_id: str,
        participant_type: str = "player",
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        challenge_release_delay_hours: int = 24,
        scoring_strategy: str = "time_based",
        scoring_config: Optional[Dict[str, Any]] = None
    ) -> CampaignInfo:
        """Create a new campaign."""
        try:
            data = {
                "title": title,
                "description": description,
                "guild_id": guild_id,
                "participant_type": participant_type,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
                "challenge_release_delay_hours": challenge_release_delay_hours,
                "scoring_strategy": scoring_strategy,
                "scoring_config": scoring_config or {}
            }
            
            response = await self._api_client.post("/campaigns", json=data)
            
            return CampaignInfo(
                id=UUID(response["id"]),
                title=response["title"],
                description=response.get("description"),
                guild_id=response.get("guild_id"),
                participant_type=response["participant_type"],
                status=response["status"],
                start_date=datetime.fromisoformat(response["start_date"].replace("Z", "+00:00")) if response.get("start_date") else None,
                end_date=datetime.fromisoformat(response["end_date"].replace("Z", "+00:00")) if response.get("end_date") else None,
                challenge_release_delay_hours=response["challenge_release_delay_hours"],
                scoring_strategy=response["scoring_strategy"],
                scoring_config=response["scoring_config"],
                created_at=datetime.fromisoformat(response["created_at"].replace("Z", "+00:00")),
                updated_at=datetime.fromisoformat(response["updated_at"].replace("Z", "+00:00"))
            )
            
        except Exception as e:
            logger.error(f"Failed to create campaign: {e}")
            raise ServiceError(f"Failed to create campaign: {e}")
    
    async def create_challenge(
        self,
        campaign_id: str,
        title: str,
        description: str,
        difficulty: str,
        problem_statement: str,
        generation_script: str,
        expected_output_format: Optional[str] = None,
        time_limit_minutes: Optional[int] = None,
        memory_limit_mb: Optional[int] = None,
        order_index: Optional[int] = None
    ) -> ChallengeInfo:
        """Create a new challenge in a campaign."""
        try:
            data = {
                "title": title,
                "description": description,
                "difficulty": difficulty,
                "problem_statement": problem_statement,
                "generation_script": generation_script,
                "expected_output_format": expected_output_format,
                "time_limit_minutes": time_limit_minutes,
                "memory_limit_mb": memory_limit_mb,
                "order_index": order_index
            }
            
            response = await self._api_client.post(f"/campaigns/{campaign_id}/challenges", json=data)
            
            return ChallengeInfo(
                id=UUID(response["id"]),
                campaign_id=UUID(response["campaign_id"]),
                title=response["title"],
                description=response.get("description"),
                difficulty=response["difficulty"],
                problem_statement=response["problem_statement"],
                expected_output_format=response.get("expected_output_format"),
                time_limit_minutes=response.get("time_limit_minutes"),
                memory_limit_mb=response.get("memory_limit_mb"),
                order_index=response["order_index"],
                release_date=datetime.fromisoformat(response["release_date"].replace("Z", "+00:00")) if response.get("release_date") else None,
                created_at=datetime.fromisoformat(response["created_at"].replace("Z", "+00:00")),
                updated_at=datetime.fromisoformat(response["updated_at"].replace("Z", "+00:00"))
            )
            
        except Exception as e:
            logger.error(f"Failed to create challenge: {e}")
            raise ServiceError(f"Failed to create challenge: {e}")