"""Test cases for the Challenge Repository."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4
from unittest.mock import AsyncMock, Mock
from sqlalchemy.exc import IntegrityError

from web.repositories.challenge_repository import ChallengeRepository
from smarter_dev.web.models import Challenge


class TestChallengeRepository:
    """Test cases for Challenge Repository functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_session = AsyncMock()
        self.repository = ChallengeRepository(self.mock_session)
        self.sample_campaign_id = uuid4()
        self.sample_challenge = Challenge(
            id=uuid4(),
            campaign_id=self.sample_campaign_id,
            order_position=1,
            title="Test Challenge",
            description="Test challenge description",
            generation_script="print('test')"
        )
    
    async def test_create_challenge_success(self):
        """Test successful challenge creation."""
        # Arrange
        campaign_id = self.sample_campaign_id
        order_position = 1
        title = "Test Challenge"
        description = "Test challenge description"
        generation_script = "print('hello world')"
        
        # Mock session behavior
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.create_challenge(
            campaign_id=campaign_id,
            order_position=order_position,
            title=title,
            description=description,
            generation_script=generation_script
        )
        
        # Assert
        assert result is not None
        assert isinstance(result, Challenge)
        assert result.campaign_id == campaign_id
        assert result.order_position == order_position
        assert result.title == title
        assert result.description == description
        assert result.generation_script == generation_script
        assert result.categories == []  # Default value
        assert result.difficulty_level is None  # Default value
        
        self.mock_session.add.assert_called_once()
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_create_challenge_with_custom_values(self):
        """Test challenge creation with custom values."""
        # Arrange
        campaign_id = self.sample_campaign_id
        categories = ["algorithms", "data-structures"]
        difficulty_level = 7
        
        # Mock session behavior
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.create_challenge(
            campaign_id=campaign_id,
            order_position=2,
            title="Advanced Challenge",
            description="A more complex challenge",
            generation_script="import random; print(random.randint(1, 100))",
            categories=categories,
            difficulty_level=difficulty_level
        )
        
        # Assert
        assert result.categories == categories
        assert result.difficulty_level == difficulty_level
    
    async def test_create_challenge_validation_errors(self):
        """Test challenge creation validation errors."""
        campaign_id = self.sample_campaign_id
        
        # Invalid campaign_id
        with pytest.raises(ValueError, match="Invalid campaign_id format"):
            await self.repository.create_challenge(
                campaign_id="not-a-uuid",  # Invalid type
                order_position=1,
                title="Test Challenge",
                description="Test description",
                generation_script="print('test')"
            )
        
        # Invalid order_position
        with pytest.raises(ValueError, match="Order position must be positive"):
            await self.repository.create_challenge(
                campaign_id=campaign_id,
                order_position=0,  # Must be positive
                title="Test Challenge",
                description="Test description",
                generation_script="print('test')"
            )
        
        # Invalid title
        with pytest.raises(ValueError, match="Challenge title must be 1-200 characters"):
            await self.repository.create_challenge(
                campaign_id=campaign_id,
                order_position=1,
                title="",  # Empty
                description="Test description",
                generation_script="print('test')"
            )
        
        # Invalid description
        with pytest.raises(ValueError, match="Challenge description cannot be empty"):
            await self.repository.create_challenge(
                campaign_id=campaign_id,
                order_position=1,
                title="Test Challenge",
                description="",  # Empty
                generation_script="print('test')"
            )
        
        # Invalid generation script
        with pytest.raises(ValueError, match="Generation script cannot be empty"):
            await self.repository.create_challenge(
                campaign_id=campaign_id,
                order_position=1,
                title="Test Challenge",
                description="Test description",
                generation_script=""  # Empty
            )
        
        # Invalid difficulty level
        with pytest.raises(ValueError, match="Difficulty level must be between 1 and 10"):
            await self.repository.create_challenge(
                campaign_id=campaign_id,
                order_position=1,
                title="Test Challenge",
                description="Test description",
                generation_script="print('test')",
                difficulty_level=11  # Too high
            )
    
    async def test_create_challenge_integrity_error(self):
        """Test challenge creation with database integrity error."""
        # Arrange
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock(side_effect=IntegrityError("", "", ""))
        self.mock_session.rollback = AsyncMock()
        
        # Act & Assert
        with pytest.raises(IntegrityError):
            await self.repository.create_challenge(
                campaign_id=self.sample_campaign_id,
                order_position=1,
                title="Test Challenge",
                description="Test description",
                generation_script="print('test')"
            )
        
        self.mock_session.rollback.assert_called_once()
    
    async def test_get_challenge_by_id_success(self):
        """Test successful challenge retrieval by ID."""
        # Arrange
        challenge_id = uuid4()
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = self.sample_challenge
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_challenge_by_id(challenge_id)
        
        # Assert
        assert result == self.sample_challenge
        self.mock_session.execute.assert_called_once()
    
    async def test_get_challenge_by_id_not_found(self):
        """Test challenge retrieval when not found."""
        # Arrange
        challenge_id = uuid4()
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_challenge_by_id(challenge_id)
        
        # Assert
        assert result is None
    
    async def test_get_challenges_by_campaign(self):
        """Test retrieving challenges by campaign."""
        # Arrange
        campaign_id = self.sample_campaign_id
        challenges = [self.sample_challenge]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = challenges
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_challenges_by_campaign(campaign_id)
        
        # Assert
        assert result == challenges
        self.mock_session.execute.assert_called_once()
    
    async def test_get_challenges_by_campaign_with_options(self):
        """Test retrieving challenges by campaign with limit and offset."""
        # Arrange
        campaign_id = self.sample_campaign_id
        challenges = [self.sample_challenge]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = challenges
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_challenges_by_campaign(
            campaign_id=campaign_id,
            limit=10,
            offset=5,
            order_by_position=False
        )
        
        # Assert
        assert result == challenges
        self.mock_session.execute.assert_called_once()
    
    async def test_get_challenge_by_position(self):
        """Test retrieving challenge by position."""
        # Arrange
        campaign_id = self.sample_campaign_id
        position = 1
        
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = self.sample_challenge
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_challenge_by_position(campaign_id, position)
        
        # Assert
        assert result == self.sample_challenge
        self.mock_session.execute.assert_called_once()
    
    async def test_get_next_challenge(self):
        """Test retrieving next challenge after a position."""
        # Arrange
        campaign_id = self.sample_campaign_id
        current_position = 1
        
        next_challenge = Challenge(
            id=uuid4(),
            campaign_id=campaign_id,
            order_position=2,
            title="Next Challenge",
            description="Next challenge description",
            generation_script="print('next')"
        )
        
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = next_challenge
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_next_challenge(campaign_id, current_position)
        
        # Assert
        assert result == next_challenge
        self.mock_session.execute.assert_called_once()
    
    async def test_get_released_challenges(self):
        """Test retrieving released challenges based on timing."""
        # Arrange
        campaign_id = self.sample_campaign_id
        campaign_start_date = datetime.now(timezone.utc) - timedelta(hours=2)
        release_delay_minutes = 60  # 1 hour
        
        # Mock get_challenges_by_campaign to return challenges
        challenges = [
            Challenge(
                campaign_id=campaign_id,
                order_position=1,
                title="Challenge 1",
                description="First challenge",
                generation_script="print('1')"
            ),
            Challenge(
                campaign_id=campaign_id,
                order_position=2,
                title="Challenge 2",
                description="Second challenge",
                generation_script="print('2')"
            )
        ]
        
        self.repository.get_challenges_by_campaign = AsyncMock(return_value=challenges)
        
        # Act
        result = await self.repository.get_released_challenges(
            campaign_id, campaign_start_date, release_delay_minutes
        )
        
        # Assert
        # Should return both challenges since they're both released
        # (Challenge 1 released immediately, Challenge 2 released after 1 hour, we're 2 hours in)
        assert len(result) == 2
        self.repository.get_challenges_by_campaign.assert_called_once()
    
    async def test_update_challenge_success(self):
        """Test successful challenge update."""
        # Arrange
        challenge_id = uuid4()
        updates = {"title": "Updated Challenge", "description": "Updated description"}
        
        self.repository.get_challenge_by_id = AsyncMock(return_value=self.sample_challenge)
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.update_challenge(challenge_id, updates)
        
        # Assert
        assert result is not None
        assert result.title == "Updated Challenge"
        assert result.description == "Updated description"
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_update_challenge_not_found(self):
        """Test challenge update when challenge not found."""
        # Arrange
        challenge_id = uuid4()
        updates = {"title": "Updated Challenge"}
        
        self.repository.get_challenge_by_id = AsyncMock(return_value=None)
        
        # Act
        result = await self.repository.update_challenge(challenge_id, updates)
        
        # Assert
        assert result is None
    
    async def test_update_challenge_invalid_fields(self):
        """Test challenge update with invalid fields."""
        # Arrange
        challenge_id = uuid4()
        invalid_updates = {"invalid_field": "value", "id": "new_id"}
        
        self.repository.get_challenge_by_id = AsyncMock(return_value=self.sample_challenge)
        
        # Act & Assert
        with pytest.raises(ValueError, match="Invalid update fields"):
            await self.repository.update_challenge(challenge_id, invalid_updates)
    
    async def test_update_generation_script(self):
        """Test updating generation script."""
        # Arrange
        challenge_id = uuid4()
        new_script = "print('updated script')"
        
        self.repository.get_challenge_by_id = AsyncMock(return_value=self.sample_challenge)
        self.repository._invalidate_cached_inputs = AsyncMock()
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.update_generation_script(challenge_id, new_script)
        
        # Assert
        assert result is not None
        self.repository._invalidate_cached_inputs.assert_called_once_with(challenge_id)
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_delete_challenge_success(self):
        """Test successful challenge deletion."""
        # Arrange
        challenge_id = uuid4()
        
        self.repository.get_challenge_by_id = AsyncMock(return_value=self.sample_challenge)
        self.mock_session.delete = AsyncMock()
        self.mock_session.commit = AsyncMock()
        
        # Act
        result = await self.repository.delete_challenge(challenge_id)
        
        # Assert
        assert result is True
        self.mock_session.delete.assert_called_once_with(self.sample_challenge)
        self.mock_session.commit.assert_called_once()
    
    async def test_delete_challenge_not_found(self):
        """Test challenge deletion when challenge not found."""
        # Arrange
        challenge_id = uuid4()
        self.repository.get_challenge_by_id = AsyncMock(return_value=None)
        
        # Act
        result = await self.repository.delete_challenge(challenge_id)
        
        # Assert
        assert result is False
    
    async def test_reorder_challenges_success(self):
        """Test successful challenge reordering."""
        # Arrange
        campaign_id = self.sample_campaign_id
        challenge1 = Challenge(
            id=uuid4(),
            campaign_id=campaign_id,
            order_position=1,
            title="Challenge 1",
            description="First challenge",
            generation_script="print('1')"
        )
        challenge2 = Challenge(
            id=uuid4(),
            campaign_id=campaign_id,
            order_position=2,
            title="Challenge 2",
            description="Second challenge",
            generation_script="print('2')"
        )
        
        challenge_orders = {
            challenge1.id: 2,  # Move to position 2
            challenge2.id: 1   # Move to position 1
        }
        
        # Mock get_challenge_by_id to return challenges
        def mock_get_challenge(cid):
            if cid == challenge1.id:
                return challenge1
            elif cid == challenge2.id:
                return challenge2
            return None
        
        self.repository.get_challenge_by_id = AsyncMock(side_effect=mock_get_challenge)
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.reorder_challenges(campaign_id, challenge_orders)
        
        # Assert
        assert len(result) == 2
        assert result[0].order_position == 1  # challenge2 moved to position 1
        assert result[1].order_position == 2  # challenge1 moved to position 2
        self.mock_session.commit.assert_called_once()
        assert self.mock_session.refresh.call_count == 2
    
    async def test_reorder_challenges_validation_errors(self):
        """Test challenge reordering validation errors."""
        campaign_id = self.sample_campaign_id
        
        # Invalid positions (non-positive)
        with pytest.raises(ValueError, match="All positions must be positive"):
            await self.repository.reorder_challenges(campaign_id, {uuid4(): 0})
        
        # Duplicate positions
        challenge1_id = uuid4()
        challenge2_id = uuid4()
        with pytest.raises(ValueError, match="Positions must be unique"):
            await self.repository.reorder_challenges(campaign_id, {
                challenge1_id: 1,
                challenge2_id: 1  # Duplicate position
            })
    
    async def test_count_challenges_by_campaign(self):
        """Test counting challenges by campaign."""
        # Arrange
        campaign_id = self.sample_campaign_id
        expected_count = 5
        
        mock_result = Mock()
        mock_result.scalar.return_value = expected_count
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.count_challenges_by_campaign(campaign_id)
        
        # Assert
        assert result == expected_count
        self.mock_session.execute.assert_called_once()
    
    async def test_get_challenges_by_category(self):
        """Test retrieving challenges by category."""
        # Arrange
        category = "algorithms"
        challenges = [self.sample_challenge]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = challenges
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_challenges_by_category(category)
        
        # Assert
        assert result == challenges
        self.mock_session.execute.assert_called_once()
    
    async def test_get_challenges_by_difficulty(self):
        """Test retrieving challenges by difficulty."""
        # Arrange
        difficulty_level = 5
        challenges = [self.sample_challenge]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = challenges
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_challenges_by_difficulty(difficulty_level)
        
        # Assert
        assert result == challenges
        self.mock_session.execute.assert_called_once()
    
    async def test_get_challenges_by_difficulty_validation_error(self):
        """Test get challenges by difficulty with invalid difficulty level."""
        # Invalid difficulty level
        with pytest.raises(ValueError, match="Difficulty level must be between 1 and 10"):
            await self.repository.get_challenges_by_difficulty(11)
    
    async def test_get_challenge_statistics(self):
        """Test retrieving challenge statistics."""
        # Arrange
        campaign_id = self.sample_campaign_id
        
        # Mock multiple query results
        total_result = Mock()
        total_result.scalar.return_value = 10
        
        avg_difficulty_result = Mock()
        avg_difficulty_result.scalar.return_value = 5.5
        
        difficulty_result = Mock()
        difficulty_result.fetchall.return_value = [(1, 2), (5, 3), (10, 1)]
        
        self.mock_session.execute = AsyncMock(
            side_effect=[total_result, avg_difficulty_result, difficulty_result]
        )
        
        # Act
        result = await self.repository.get_challenge_statistics(campaign_id)
        
        # Assert
        assert result["total_challenges"] == 10
        assert result["average_difficulty"] == 5.5
        assert result["challenges_by_difficulty"] == {1: 2, 5: 3, 10: 1}
        assert "date_range" in result
        
        # Should have called execute 3 times (total, avg difficulty, by difficulty)
        assert self.mock_session.execute.call_count == 3