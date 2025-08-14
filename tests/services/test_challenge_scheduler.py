"""Tests for the challenge scheduling service.

This module tests the automated challenge release functionality
including the scheduler service and release management.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from smarter_dev.services.challenge_scheduler import ChallengeScheduler, ChallengeReleaseService
from smarter_dev.web.models import Campaign, Challenge


class TestChallengeScheduler:
    """Test suite for the ChallengeScheduler service."""

    @pytest.fixture
    def sample_campaign(self):
        """Create a sample campaign for testing."""
        return Campaign(
            id=uuid4(),
            name="Test Campaign",
            description="A test campaign",
            guild_id="123456789012345678",
            campaign_type="player",
            state="active",
            start_date=datetime.now(timezone.utc) - timedelta(hours=1),
            release_delay_minutes=60,  # 1 hour between releases
            scoring_type="time_based",
            starting_points=100,
            points_decrease_step=10,
            announcement_channel_id="123456789012345678"
        )

    @pytest.fixture
    def sample_challenges(self, sample_campaign):
        """Create sample challenges for testing."""
        base_time = datetime.now(timezone.utc)
        return [
            Challenge(
                id=uuid4(),
                campaign_id=sample_campaign.id,
                order_position=1,
                title="Challenge 1",
                description="First challenge",
                generation_script="print('test')",
                categories=["basic"],
                released_at=base_time - timedelta(minutes=30)  # Already released
            ),
            Challenge(
                id=uuid4(),
                campaign_id=sample_campaign.id,
                order_position=2,
                title="Challenge 2", 
                description="Second challenge",
                generation_script="print('test2')",
                categories=["basic"],
                released_at=None  # Not yet released
            ),
            Challenge(
                id=uuid4(),
                campaign_id=sample_campaign.id,
                order_position=3,
                title="Challenge 3",
                description="Third challenge", 
                generation_script="print('test3')",
                categories=["basic"],
                released_at=None  # Not yet released
            )
        ]

    def test_challenge_scheduler_initialization(self):
        """Test that the scheduler can be initialized properly."""
        scheduler = ChallengeScheduler(check_interval_seconds=30)
        assert scheduler.check_interval == 30
        assert not scheduler.running

    @pytest.mark.asyncio
    async def test_scheduler_identifies_challenges_to_release(self, sample_campaign, sample_challenges):
        """Test that the scheduler correctly identifies challenges that should be released."""
        scheduler = ChallengeScheduler()
        
        # Mock the database session and queries
        with patch('smarter_dev.services.challenge_scheduler.get_db_session_context') as mock_db_context:
            mock_session = AsyncMock()
            mock_db_context.return_value.__aenter__.return_value = mock_session
            mock_db_context.return_value.__aexit__.return_value = None
            
            # Mock campaign query
            mock_campaign_result = Mock()
            mock_campaign_result.scalars.return_value.all.return_value = [sample_campaign]
            mock_session.execute.return_value = mock_campaign_result
            
            # Mock challenge query - return on second call
            mock_challenge_result = Mock()
            mock_challenge_result.scalars.return_value.all.return_value = sample_challenges
            mock_session.execute.side_effect = [mock_campaign_result, mock_challenge_result]
            
            # Mock the release method
            with patch.object(scheduler, '_release_challenge', new_callable=AsyncMock) as mock_release:
                await scheduler._check_and_release_challenges()
                
                # Should have tried to release challenge 2 (challenge 1 already released, 
                # challenge 3 not yet due)
                mock_release.assert_called_once()
                _, _, released_challenge = mock_release.call_args[0]
                assert released_challenge.order_position == 2

    @pytest.mark.asyncio
    async def test_challenge_release_updates_timestamp(self, sample_campaign, sample_challenges):
        """Test that releasing a challenge updates the released_at timestamp."""
        scheduler = ChallengeScheduler()
        
        mock_session = AsyncMock()
        challenge_to_release = sample_challenges[1]  # Challenge 2
        
        # Ensure challenge is not yet released
        assert challenge_to_release.released_at is None
        
        await scheduler._release_challenge(mock_session, sample_campaign, challenge_to_release)
        
        # Check that released_at was set
        assert challenge_to_release.released_at is not None
        assert isinstance(challenge_to_release.released_at, datetime)
        
        # Check that session operations were called
        mock_session.add.assert_called_once_with(challenge_to_release)
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_scheduler_handles_errors_gracefully(self, sample_campaign):
        """Test that scheduler continues running even when errors occur."""
        scheduler = ChallengeScheduler()
        
        with patch('smarter_dev.services.challenge_scheduler.get_db_session_context') as mock_db_context:
            # Make the database context raise an exception when entering
            mock_db_context.return_value.__aenter__.side_effect = Exception("Database error")
            
            # This should not raise an exception
            await scheduler._check_and_release_challenges()
            
            # The method should complete without raising


class TestChallengeReleaseService:
    """Test suite for the ChallengeReleaseService."""

    @pytest.fixture
    def sample_campaign(self):
        """Create a sample campaign for testing."""
        return Campaign(
            id=uuid4(),
            name="Test Campaign",
            description="A test campaign",
            guild_id="123456789012345678", 
            campaign_type="player",
            state="active",
            start_date=datetime.now(timezone.utc) - timedelta(hours=2),
            release_delay_minutes=60,
            scoring_type="time_based",
            starting_points=100,
            points_decrease_step=10,
            announcement_channel_id="123456789012345678"
        )

    @pytest.mark.asyncio
    async def test_get_next_challenge_release(self, sample_campaign):
        """Test getting the next challenge to be released."""
        campaign_id = sample_campaign.id
        
        # Create sample challenges
        base_time = datetime.now(timezone.utc)
        challenges = [
            Challenge(
                id=uuid4(),
                campaign_id=campaign_id,
                order_position=1,
                title="Challenge 1",
                description="First challenge",
                generation_script="print('test')",
                categories=["basic"],
                released_at=base_time - timedelta(minutes=30)  # Already released
            ),
            Challenge(
                id=uuid4(), 
                campaign_id=campaign_id,
                order_position=2,
                title="Challenge 2",
                description="Second challenge",
                generation_script="print('test2')",
                categories=["basic"],
                released_at=None  # Not yet released
            )
        ]
        
        with patch('smarter_dev.services.challenge_scheduler.get_db_session_context') as mock_db_context:
            mock_session = AsyncMock()
            mock_db_context.return_value.__aenter__.return_value = mock_session
            mock_db_context.return_value.__aexit__.return_value = None
            
            # Mock database queries
            mock_campaign_result = Mock()
            mock_campaign_result.scalar_one_or_none.return_value = sample_campaign
            
            mock_challenges_result = Mock()
            mock_challenges_result.scalars.return_value.all.return_value = challenges
            
            mock_session.execute.side_effect = [mock_campaign_result, mock_challenges_result]
            
            # Test getting next release
            next_release = await ChallengeReleaseService.get_next_challenge_release(campaign_id)
            
            assert next_release is not None
            assert next_release["challenge_title"] == "Challenge 2"
            assert next_release["order_position"] == 2
            assert "release_time" in next_release
            assert "time_until_release" in next_release

    @pytest.mark.asyncio
    async def test_get_released_challenges(self, sample_campaign):
        """Test getting all released challenges for a campaign."""
        campaign_id = sample_campaign.id
        
        # Create sample challenges with mixed release states
        base_time = datetime.now(timezone.utc)
        challenges = [
            Challenge(
                id=uuid4(),
                campaign_id=campaign_id,
                order_position=1,
                title="Challenge 1",
                description="First challenge",
                generation_script="print('test')",
                categories=["basic"],
                released_at=base_time - timedelta(hours=2)  # Released
            ),
            Challenge(
                id=uuid4(),
                campaign_id=campaign_id,
                order_position=2,
                title="Challenge 2",
                description="Second challenge", 
                generation_script="print('test2')",
                categories=["basic"],
                released_at=base_time - timedelta(hours=1)  # Released
            ),
            Challenge(
                id=uuid4(),
                campaign_id=campaign_id,
                order_position=3,
                title="Challenge 3",
                description="Third challenge",
                generation_script="print('test3')",
                categories=["basic"],
                released_at=None  # Not released
            )
        ]
        
        with patch('smarter_dev.services.challenge_scheduler.get_db_session_context') as mock_db_context:
            mock_session = AsyncMock()
            mock_db_context.return_value.__aenter__.return_value = mock_session
            mock_db_context.return_value.__aexit__.return_value = None
            
            # Mock database queries
            mock_campaign_result = Mock()
            mock_campaign_result.scalar_one_or_none.return_value = sample_campaign
            
            mock_challenges_result = Mock()
            mock_challenges_result.scalars.return_value.all.return_value = challenges
            
            mock_session.execute.side_effect = [mock_campaign_result, mock_challenges_result]
            
            # Test getting released challenges
            released = await ChallengeReleaseService.get_released_challenges(campaign_id)
            
            assert len(released) == 2  # Only the first two are released
            assert all(challenge.is_released for challenge in released)
            assert released[0].order_position == 1
            assert released[1].order_position == 2

    @pytest.mark.asyncio 
    async def test_get_next_challenge_release_all_released(self, sample_campaign):
        """Test getting next challenge when all are released."""
        campaign_id = sample_campaign.id
        
        # Create challenges that are all released
        base_time = datetime.now(timezone.utc)
        challenges = [
            Challenge(
                id=uuid4(),
                campaign_id=campaign_id,
                order_position=1,
                title="Challenge 1",
                description="First challenge",
                generation_script="print('test')",
                categories=["basic"],
                released_at=base_time - timedelta(hours=2)
            ),
            Challenge(
                id=uuid4(),
                campaign_id=campaign_id,
                order_position=2,
                title="Challenge 2", 
                description="Second challenge",
                generation_script="print('test2')",
                categories=["basic"],
                released_at=base_time - timedelta(hours=1)
            )
        ]
        
        with patch('smarter_dev.services.challenge_scheduler.get_db_session_context') as mock_db_context:
            mock_session = AsyncMock()
            mock_db_context.return_value.__aenter__.return_value = mock_session
            mock_db_context.return_value.__aexit__.return_value = None
            
            # Mock database queries
            mock_campaign_result = Mock()
            mock_campaign_result.scalar_one_or_none.return_value = sample_campaign
            
            mock_challenges_result = Mock()
            mock_challenges_result.scalars.return_value.all.return_value = challenges
            
            mock_session.execute.side_effect = [mock_campaign_result, mock_challenges_result]
            
            # Test getting next release when all are released
            next_release = await ChallengeReleaseService.get_next_challenge_release(campaign_id)
            
            assert next_release is None  # No more challenges to release