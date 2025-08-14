"""Test cases for Challenge Release Service."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock, Mock

from web.services.challenge_release_service import (
    ChallengeReleaseService,
    ChallengeReleaseInfo,
    ChallengeReleaseStatus
)


class TestChallengeReleaseInfo:
    """Test cases for ChallengeReleaseInfo data structure."""
    
    def test_challenge_release_info_creation(self):
        """Test ChallengeReleaseInfo creation with all fields."""
        release_time = datetime.now(timezone.utc)
        
        info = ChallengeReleaseInfo(
            challenge_id=uuid4(),
            order_position=1,
            title="Test Challenge",
            status=ChallengeReleaseStatus.RELEASED,
            release_time=release_time,
            time_until_release=None
        )
        
        assert info.status == ChallengeReleaseStatus.RELEASED
        assert info.order_position == 1
        assert info.title == "Test Challenge"
        assert info.release_time == release_time
        assert info.time_until_release is None
    
    def test_challenge_release_info_pending_release(self):
        """Test ChallengeReleaseInfo for pending release."""
        release_time = datetime.now(timezone.utc) + timedelta(hours=2)
        time_until = timedelta(hours=2)
        
        info = ChallengeReleaseInfo(
            challenge_id=uuid4(),
            order_position=2,
            title="Future Challenge",
            status=ChallengeReleaseStatus.PENDING,
            release_time=release_time,
            time_until_release=time_until
        )
        
        assert info.status == ChallengeReleaseStatus.PENDING
        assert info.time_until_release == time_until
        assert info.release_time == release_time


class TestChallengeReleaseService:
    """Test cases for Challenge Release Service functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_campaign_repo = AsyncMock()
        self.mock_challenge_repo = AsyncMock()
        self.service = ChallengeReleaseService(
            campaign_repository=self.mock_campaign_repo,
            challenge_repository=self.mock_challenge_repo
        )
        
        # Base time for testing
        self.base_time = datetime.now(timezone.utc)
        
        # Sample campaign
        self.sample_campaign = Mock(
            id=uuid4(),
            start_date=self.base_time,
            release_delay_minutes=60,  # 1 hour between releases
            state="active"
        )
        
        # Sample challenges
        self.sample_challenges = [
            Mock(
                id=uuid4(),
                order_position=1,
                title="Challenge 1",
                campaign_id=self.sample_campaign.id
            ),
            Mock(
                id=uuid4(),
                order_position=2,
                title="Challenge 2", 
                campaign_id=self.sample_campaign.id
            ),
            Mock(
                id=uuid4(),
                order_position=3,
                title="Challenge 3",
                campaign_id=self.sample_campaign.id
            )
        ]
    
    async def test_get_challenge_release_schedule_all_released(self):
        """Test getting release schedule when all challenges are released."""
        # Campaign started 3 hours ago, so all challenges should be released
        campaign_start = self.base_time - timedelta(hours=3)
        current_time = self.base_time
        
        self.sample_campaign.start_date = campaign_start
        
        # Mock repository responses
        self.mock_campaign_repo.get_campaign_by_id.return_value = self.sample_campaign
        self.mock_challenge_repo.get_challenges_by_campaign.return_value = self.sample_challenges
        
        # Act
        schedule = await self.service.get_challenge_release_schedule(
            campaign_id=self.sample_campaign.id,
            current_time=current_time
        )
        
        # Assert
        assert len(schedule) == 3
        
        # All challenges should be released
        for info in schedule:
            assert info.status == ChallengeReleaseStatus.RELEASED
            assert info.time_until_release is None
        
        # Check order positions
        assert schedule[0].order_position == 1
        assert schedule[1].order_position == 2
        assert schedule[2].order_position == 3
        
        # Check release times
        assert schedule[0].release_time == campaign_start  # First challenge releases immediately
        assert schedule[1].release_time == campaign_start + timedelta(hours=1)  # Second after 1 hour
        assert schedule[2].release_time == campaign_start + timedelta(hours=2)  # Third after 2 hours
    
    async def test_get_challenge_release_schedule_partial_release(self):
        """Test getting release schedule with some challenges still pending."""
        # Campaign started 1.5 hours ago, so only first 2 challenges should be released
        campaign_start = self.base_time - timedelta(hours=1, minutes=30)
        current_time = self.base_time
        
        self.sample_campaign.start_date = campaign_start
        
        # Mock repository responses
        self.mock_campaign_repo.get_campaign_by_id.return_value = self.sample_campaign
        self.mock_challenge_repo.get_challenges_by_campaign.return_value = self.sample_challenges
        
        # Act
        schedule = await self.service.get_challenge_release_schedule(
            campaign_id=self.sample_campaign.id,
            current_time=current_time
        )
        
        # Assert
        assert len(schedule) == 3
        
        # First two challenges should be released
        assert schedule[0].status == ChallengeReleaseStatus.RELEASED
        assert schedule[1].status == ChallengeReleaseStatus.RELEASED
        
        # Third challenge should be pending
        assert schedule[2].status == ChallengeReleaseStatus.PENDING
        assert schedule[2].time_until_release == timedelta(minutes=30)  # 30 minutes until release
    
    async def test_get_challenge_release_schedule_no_challenges_released(self):
        """Test getting release schedule when no challenges are released yet."""
        # Campaign starts in 1 hour
        campaign_start = self.base_time + timedelta(hours=1)
        current_time = self.base_time
        
        self.sample_campaign.start_date = campaign_start
        
        # Mock repository responses
        self.mock_campaign_repo.get_campaign_by_id.return_value = self.sample_campaign
        self.mock_challenge_repo.get_challenges_by_campaign.return_value = self.sample_challenges
        
        # Act
        schedule = await self.service.get_challenge_release_schedule(
            campaign_id=self.sample_campaign.id,
            current_time=current_time
        )
        
        # Assert
        assert len(schedule) == 3
        
        # All challenges should be pending
        for info in schedule:
            assert info.status == ChallengeReleaseStatus.PENDING
            assert info.time_until_release is not None
        
        # Check time until release
        assert schedule[0].time_until_release == timedelta(hours=1)  # First challenge in 1 hour
        assert schedule[1].time_until_release == timedelta(hours=2)  # Second challenge in 2 hours
        assert schedule[2].time_until_release == timedelta(hours=3)  # Third challenge in 3 hours
    
    async def test_get_available_challenges_active_campaign(self):
        """Test getting available challenges for active campaign."""
        # Campaign started 1.5 hours ago
        campaign_start = self.base_time - timedelta(hours=1, minutes=30)
        current_time = self.base_time
        
        self.sample_campaign.start_date = campaign_start
        
        # Mock repository responses
        self.mock_campaign_repo.get_campaign_by_id.return_value = self.sample_campaign
        self.mock_challenge_repo.get_challenges_by_campaign.return_value = self.sample_challenges
        
        # Act
        available = await self.service.get_available_challenges(
            campaign_id=self.sample_campaign.id,
            current_time=current_time
        )
        
        # Assert
        assert len(available) == 2  # Only first 2 challenges should be available
        assert available[0].order_position == 1
        assert available[1].order_position == 2
    
    async def test_get_available_challenges_draft_campaign(self):
        """Test getting available challenges for draft campaign."""
        self.sample_campaign.state = "draft"
        
        # Mock repository responses
        self.mock_campaign_repo.get_campaign_by_id.return_value = self.sample_campaign
        
        # Act
        available = await self.service.get_available_challenges(
            campaign_id=self.sample_campaign.id
        )
        
        # Assert
        assert len(available) == 0  # No challenges available for draft campaign
    
    async def test_get_available_challenges_completed_campaign(self):
        """Test getting available challenges for completed campaign."""
        self.sample_campaign.state = "completed"
        
        # Mock repository responses
        self.mock_campaign_repo.get_campaign_by_id.return_value = self.sample_campaign
        self.mock_challenge_repo.get_challenges_by_campaign.return_value = self.sample_challenges
        
        # Act
        available = await self.service.get_available_challenges(
            campaign_id=self.sample_campaign.id
        )
        
        # Assert
        assert len(available) == 3  # All challenges available for completed campaign
    
    async def test_is_challenge_released_true(self):
        """Test challenge release check when challenge is released."""
        # Challenge should be released (campaign started 2 hours ago, challenge 2 releases after 1 hour)
        campaign_start = self.base_time - timedelta(hours=2)
        current_time = self.base_time
        
        challenge = self.sample_challenges[1]  # Order position 2
        
        # Act
        is_released = await self.service.is_challenge_released(
            challenge=challenge,
            campaign_start_date=campaign_start,
            release_delay_minutes=60,
            current_time=current_time
        )
        
        # Assert
        assert is_released is True
    
    async def test_is_challenge_released_false(self):
        """Test challenge release check when challenge is not yet released."""
        # Challenge should not be released (campaign started 30 minutes ago, challenge 2 releases after 1 hour)
        campaign_start = self.base_time - timedelta(minutes=30)
        current_time = self.base_time
        
        challenge = self.sample_challenges[1]  # Order position 2
        
        # Act
        is_released = await self.service.is_challenge_released(
            challenge=challenge,
            campaign_start_date=campaign_start,
            release_delay_minutes=60,
            current_time=current_time
        )
        
        # Assert
        assert is_released is False
    
    async def test_get_next_challenge_release_some_pending(self):
        """Test getting next challenge release when some are pending."""
        # Campaign started 1.5 hours ago
        campaign_start = self.base_time - timedelta(hours=1, minutes=30)
        current_time = self.base_time
        
        self.sample_campaign.start_date = campaign_start
        
        # Mock repository responses
        self.mock_campaign_repo.get_campaign_by_id.return_value = self.sample_campaign
        self.mock_challenge_repo.get_challenges_by_campaign.return_value = self.sample_challenges
        
        # Act
        next_release = await self.service.get_next_challenge_release(
            campaign_id=self.sample_campaign.id,
            current_time=current_time
        )
        
        # Assert
        assert next_release is not None
        assert next_release.order_position == 3  # Third challenge is next
        assert next_release.status == ChallengeReleaseStatus.PENDING
        assert next_release.time_until_release == timedelta(minutes=30)
    
    async def test_get_next_challenge_release_all_released(self):
        """Test getting next challenge release when all are released."""
        # Campaign started 5 hours ago, all challenges released
        campaign_start = self.base_time - timedelta(hours=5)
        current_time = self.base_time
        
        self.sample_campaign.start_date = campaign_start
        
        # Mock repository responses
        self.mock_campaign_repo.get_campaign_by_id.return_value = self.sample_campaign
        self.mock_challenge_repo.get_challenges_by_campaign.return_value = self.sample_challenges
        
        # Act
        next_release = await self.service.get_next_challenge_release(
            campaign_id=self.sample_campaign.id,
            current_time=current_time
        )
        
        # Assert
        assert next_release is None  # No more challenges to release
    
    async def test_get_challenge_release_time(self):
        """Test calculating challenge release time."""
        campaign_start = self.base_time
        
        # Test first challenge (releases immediately)
        release_time = self.service.get_challenge_release_time(
            campaign_start_date=campaign_start,
            challenge_order_position=1,
            release_delay_minutes=60
        )
        assert release_time == campaign_start
        
        # Test second challenge (releases after 1 hour)
        release_time = self.service.get_challenge_release_time(
            campaign_start_date=campaign_start,
            challenge_order_position=2,
            release_delay_minutes=60
        )
        assert release_time == campaign_start + timedelta(hours=1)
        
        # Test third challenge (releases after 2 hours)
        release_time = self.service.get_challenge_release_time(
            campaign_start_date=campaign_start,
            challenge_order_position=3,
            release_delay_minutes=60
        )
        assert release_time == campaign_start + timedelta(hours=2)
    
    async def test_service_with_campaign_not_found(self):
        """Test service behavior when campaign is not found."""
        # Mock campaign not found
        self.mock_campaign_repo.get_campaign_by_id.return_value = None
        
        # Act & Assert
        with pytest.raises(ValueError, match="Campaign not found"):
            await self.service.get_challenge_release_schedule(
                campaign_id=uuid4()
            )
    
    async def test_service_with_no_challenges(self):
        """Test service behavior when campaign has no challenges."""
        # Mock empty challenges list
        self.mock_campaign_repo.get_campaign_by_id.return_value = self.sample_campaign
        self.mock_challenge_repo.get_challenges_by_campaign.return_value = []
        
        # Act
        schedule = await self.service.get_challenge_release_schedule(
            campaign_id=self.sample_campaign.id
        )
        
        # Assert
        assert len(schedule) == 0
    
    async def test_service_validation_errors(self):
        """Test service input validation."""
        # Test None campaign_id
        with pytest.raises(ValueError, match="Campaign ID cannot be None"):
            await self.service.get_challenge_release_schedule(campaign_id=None)
        
        # Test invalid current_time
        with pytest.raises(ValueError, match="Current time must be timezone-aware"):
            await self.service.get_challenge_release_schedule(
                campaign_id=uuid4(),
                current_time=datetime.now()  # No timezone
            )
    
    async def test_custom_release_delay(self):
        """Test service with custom release delay."""
        # Set 30-minute delay between releases
        self.sample_campaign.release_delay_minutes = 30
        
        campaign_start = self.base_time - timedelta(minutes=45)  # Started 45 minutes ago
        current_time = self.base_time
        
        self.sample_campaign.start_date = campaign_start
        
        # Mock repository responses
        self.mock_campaign_repo.get_campaign_by_id.return_value = self.sample_campaign
        self.mock_challenge_repo.get_challenges_by_campaign.return_value = self.sample_challenges
        
        # Act
        available = await self.service.get_available_challenges(
            campaign_id=self.sample_campaign.id,
            current_time=current_time
        )
        
        # Assert
        # Challenge 1: releases at start (45 minutes ago) - available
        # Challenge 2: releases after 30 minutes (15 minutes ago) - available  
        # Challenge 3: releases after 60 minutes (15 minutes from now) - not available
        assert len(available) == 2


class TestChallengeReleaseStatus:
    """Test cases for ChallengeReleaseStatus enum."""
    
    def test_challenge_release_status_values(self):
        """Test ChallengeReleaseStatus enum values."""
        assert ChallengeReleaseStatus.RELEASED.value == "released"
        assert ChallengeReleaseStatus.PENDING.value == "pending"
    
    def test_challenge_release_status_comparison(self):
        """Test ChallengeReleaseStatus comparison."""
        assert ChallengeReleaseStatus.RELEASED != ChallengeReleaseStatus.PENDING
        assert ChallengeReleaseStatus.RELEASED.value == "released"
        assert ChallengeReleaseStatus.PENDING.value == "pending"
    
    async def test_get_challenges_released_since(self):
        """Test getting challenges released since a specific time."""
        # Setup service with mocks for this test
        mock_campaign_repo = AsyncMock()
        mock_challenge_repo = AsyncMock()
        service = ChallengeReleaseService(mock_campaign_repo, mock_challenge_repo)
        
        # Campaign started 2 hours ago
        base_time = datetime.now(timezone.utc)
        campaign_start = base_time - timedelta(hours=2)
        current_time = base_time
        
        # Check for challenges released in last hour
        since_time = base_time - timedelta(hours=1)
        
        sample_campaign = Mock(
            id=uuid4(),
            start_date=campaign_start,
            release_delay_minutes=60,
            state="active"
        )
        
        sample_challenges = [
            Mock(id=uuid4(), order_position=1, title="Challenge 1", campaign_id=sample_campaign.id),
            Mock(id=uuid4(), order_position=2, title="Challenge 2", campaign_id=sample_campaign.id),
            Mock(id=uuid4(), order_position=3, title="Challenge 3", campaign_id=sample_campaign.id)
        ]
        
        # Mock repository responses
        mock_campaign_repo.get_campaign_by_id.return_value = sample_campaign
        mock_challenge_repo.get_challenges_by_campaign.return_value = sample_challenges
        
        # Act
        newly_released = await service.get_challenges_released_since(
            campaign_id=sample_campaign.id,
            since_time=since_time,
            current_time=current_time
        )
        
        # Assert
        # Challenge 1: released at start (2 hours ago) - not in last hour
        # Challenge 2: released after 1 hour (1 hour ago) - exactly at since_time boundary, not included
        # Challenge 3: released after 2 hours (now) - released in last hour
        assert len(newly_released) == 1
        assert newly_released[0].order_position == 3
    
    async def test_get_campaign_release_summary(self):
        """Test getting campaign release summary statistics."""
        # Setup service with mocks for this test
        mock_campaign_repo = AsyncMock()
        mock_challenge_repo = AsyncMock()
        service = ChallengeReleaseService(mock_campaign_repo, mock_challenge_repo)
        
        # Campaign started 1.5 hours ago
        base_time = datetime.now(timezone.utc)
        campaign_start = base_time - timedelta(hours=1, minutes=30)
        current_time = base_time
        
        sample_campaign = Mock(
            id=uuid4(),
            start_date=campaign_start,
            release_delay_minutes=60,
            state="active"
        )
        
        sample_challenges = [
            Mock(id=uuid4(), order_position=1, title="Challenge 1", campaign_id=sample_campaign.id),
            Mock(id=uuid4(), order_position=2, title="Challenge 2", campaign_id=sample_campaign.id),
            Mock(id=uuid4(), order_position=3, title="Challenge 3", campaign_id=sample_campaign.id)
        ]
        
        # Mock repository responses  
        mock_campaign_repo.get_campaign_by_id.return_value = sample_campaign
        mock_challenge_repo.get_challenges_by_campaign.return_value = sample_challenges
        
        # Act
        summary = await service.get_campaign_release_summary(
            campaign_id=sample_campaign.id,
            current_time=current_time
        )
        
        # Assert
        assert summary["total_challenges"] == 3
        assert summary["released_challenges"] == 2  # First 2 challenges released
        assert summary["pending_challenges"] == 1   # Third challenge pending
        assert abs(summary["completion_percentage"] - 66.67) < 0.01  # 2/3 * 100 ≈ 66.67%
        assert summary["next_release"] is not None
        assert summary["next_release"]["challenge_title"] == "Challenge 3"
        assert summary["next_release"]["time_until_release"] == timedelta(minutes=30)