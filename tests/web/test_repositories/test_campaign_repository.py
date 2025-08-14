"""Test cases for the Campaign Repository."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4
from unittest.mock import AsyncMock, Mock
from sqlalchemy.exc import IntegrityError

from web.repositories.campaign_repository import CampaignRepository
from smarter_dev.web.models import Campaign


class TestCampaignRepository:
    """Test cases for Campaign Repository functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_session = AsyncMock()
        self.repository = CampaignRepository(self.mock_session)
        self.sample_campaign = Campaign(
            id=uuid4(),
            guild_id="123456789",
            name="Test Campaign",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
    
    async def test_create_campaign_success(self):
        """Test successful campaign creation."""
        # Arrange
        guild_id = "123456789"
        name = "Test Campaign"
        start_date = datetime.now(timezone.utc)
        announcement_channel_id = "987654321"
        
        # Mock session behavior
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.create_campaign(
            guild_id=guild_id,
            name=name,
            start_date=start_date,
            announcement_channel_id=announcement_channel_id
        )
        
        # Assert
        assert result is not None
        assert isinstance(result, Campaign)
        assert result.guild_id == guild_id
        assert result.name == name
        assert result.start_date == start_date
        assert result.announcement_channel_id == announcement_channel_id
        assert result.campaign_type == "player"  # Default value
        assert result.scoring_type == "time_based"  # Default value
        
        self.mock_session.add.assert_called_once()
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_create_campaign_with_custom_values(self):
        """Test campaign creation with custom values."""
        # Arrange
        guild_id = "123456789"
        name = "Custom Campaign"
        start_date = datetime.now(timezone.utc) + timedelta(days=1)
        announcement_channel_id = "987654321"
        
        # Mock session behavior
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.create_campaign(
            guild_id=guild_id,
            name=name,
            start_date=start_date,
            announcement_channel_id=announcement_channel_id,
            campaign_type="squad",
            description="Custom description",
            release_delay_minutes=720,
            scoring_type="point_based",
            starting_points=100,
            points_decrease_step=10
        )
        
        # Assert
        assert result.campaign_type == "squad"
        assert result.description == "Custom description"
        assert result.release_delay_minutes == 720
        assert result.scoring_type == "point_based"
        assert result.starting_points == 100
        assert result.points_decrease_step == 10
    
    async def test_create_campaign_validation_errors(self):
        """Test campaign creation validation errors."""
        start_date = datetime.now(timezone.utc)
        
        # Invalid guild_id
        with pytest.raises(ValueError, match="Invalid guild_id format"):
            await self.repository.create_campaign(
                guild_id="123",  # Too short
                name="Test Campaign",
                start_date=start_date,
                announcement_channel_id="987654321"
            )
        
        # Invalid name
        with pytest.raises(ValueError, match="Campaign name must be 1-100 characters"):
            await self.repository.create_campaign(
                guild_id="123456789",
                name="",  # Empty
                start_date=start_date,
                announcement_channel_id="987654321"
            )
        
        # Invalid campaign type
        with pytest.raises(ValueError, match="Campaign type must be"):
            await self.repository.create_campaign(
                guild_id="123456789",
                name="Test Campaign",
                start_date=start_date,
                announcement_channel_id="987654321",
                campaign_type="invalid"
            )
        
        # Invalid scoring type
        with pytest.raises(ValueError, match="Scoring type must be"):
            await self.repository.create_campaign(
                guild_id="123456789",
                name="Test Campaign",
                start_date=start_date,
                announcement_channel_id="987654321",
                scoring_type="invalid"
            )
        
        # Invalid release delay
        with pytest.raises(ValueError, match="Release delay must be positive"):
            await self.repository.create_campaign(
                guild_id="123456789",
                name="Test Campaign",
                start_date=start_date,
                announcement_channel_id="987654321",
                release_delay_minutes=0
            )
        
        # Point-based scoring without starting points
        with pytest.raises(ValueError, match="Starting points must be positive"):
            await self.repository.create_campaign(
                guild_id="123456789",
                name="Test Campaign",
                start_date=start_date,
                announcement_channel_id="987654321",
                scoring_type="point_based"
            )
    
    async def test_create_campaign_integrity_error(self):
        """Test campaign creation with database integrity error."""
        # Arrange
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock(side_effect=IntegrityError("", "", ""))
        self.mock_session.rollback = AsyncMock()
        
        # Act & Assert
        with pytest.raises(IntegrityError):
            await self.repository.create_campaign(
                guild_id="123456789",
                name="Test Campaign",
                start_date=datetime.now(timezone.utc),
                announcement_channel_id="987654321"
            )
        
        self.mock_session.rollback.assert_called_once()
    
    async def test_get_campaign_by_id_success(self):
        """Test successful campaign retrieval by ID."""
        # Arrange
        campaign_id = uuid4()
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = self.sample_campaign
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_campaign_by_id(campaign_id)
        
        # Assert
        assert result == self.sample_campaign
        self.mock_session.execute.assert_called_once()
    
    async def test_get_campaign_by_id_not_found(self):
        """Test campaign retrieval when not found."""
        # Arrange
        campaign_id = uuid4()
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_campaign_by_id(campaign_id)
        
        # Assert
        assert result is None
    
    async def test_get_campaigns_by_guild(self):
        """Test retrieving campaigns by guild."""
        # Arrange
        guild_id = "123456789"
        campaigns = [self.sample_campaign]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = campaigns
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_campaigns_by_guild(guild_id)
        
        # Assert
        assert result == campaigns
        self.mock_session.execute.assert_called_once()
    
    async def test_get_campaigns_by_guild_with_filters(self):
        """Test retrieving campaigns by guild with state and type filters."""
        # Arrange
        guild_id = "123456789"
        campaigns = [self.sample_campaign]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = campaigns
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_campaigns_by_guild(
            guild_id=guild_id,
            state="active",
            campaign_type="player",
            limit=10,
            offset=5
        )
        
        # Assert
        assert result == campaigns
        self.mock_session.execute.assert_called_once()
    
    async def test_get_active_campaigns(self):
        """Test retrieving active campaigns."""
        # Arrange
        active_campaigns = [self.sample_campaign]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = active_campaigns
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_active_campaigns()
        
        # Assert
        assert result == active_campaigns
        self.mock_session.execute.assert_called_once()
    
    async def test_update_campaign_state_success(self):
        """Test successful campaign state update."""
        # Arrange
        campaign_id = uuid4()
        new_state = "active"
        
        # Mock campaign retrieval
        draft_campaign = Campaign(
            id=campaign_id,
            guild_id="123456789",
            name="Test Campaign",
            state="draft",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        # Mock get_campaign_by_id
        self.repository.get_campaign_by_id = AsyncMock(return_value=draft_campaign)
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.update_campaign_state(campaign_id, new_state)
        
        # Assert
        assert result is not None
        assert result.state == new_state
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_update_campaign_state_not_found(self):
        """Test campaign state update when campaign not found."""
        # Arrange
        campaign_id = uuid4()
        self.repository.get_campaign_by_id = AsyncMock(return_value=None)
        
        # Act
        result = await self.repository.update_campaign_state(campaign_id, "active")
        
        # Assert
        assert result is None
    
    async def test_update_campaign_state_invalid_transition(self):
        """Test campaign state update with invalid transition."""
        # Arrange
        campaign_id = uuid4()
        
        # Mock completed campaign (cannot transition anywhere)
        completed_campaign = Campaign(
            id=campaign_id,
            guild_id="123456789",
            name="Test Campaign",
            state="completed",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        self.repository.get_campaign_by_id = AsyncMock(return_value=completed_campaign)
        
        # Act & Assert
        with pytest.raises(ValueError, match="Cannot transition from"):
            await self.repository.update_campaign_state(campaign_id, "active")
    
    async def test_update_campaign_success(self):
        """Test successful campaign update."""
        # Arrange
        campaign_id = uuid4()
        updates = {"name": "Updated Campaign", "description": "Updated description"}
        
        self.repository.get_campaign_by_id = AsyncMock(return_value=self.sample_campaign)
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.update_campaign(campaign_id, updates)
        
        # Assert
        assert result is not None
        assert result.name == "Updated Campaign"
        assert result.description == "Updated description"
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_update_campaign_invalid_fields(self):
        """Test campaign update with invalid fields."""
        # Arrange
        campaign_id = uuid4()
        invalid_updates = {"invalid_field": "value", "id": "new_id"}
        
        self.repository.get_campaign_by_id = AsyncMock(return_value=self.sample_campaign)
        
        # Act & Assert
        with pytest.raises(ValueError, match="Invalid update fields"):
            await self.repository.update_campaign(campaign_id, invalid_updates)
    
    async def test_delete_campaign_success(self):
        """Test successful campaign deletion."""
        # Arrange
        campaign_id = uuid4()
        
        self.repository.get_campaign_by_id = AsyncMock(return_value=self.sample_campaign)
        self.mock_session.delete = AsyncMock()
        self.mock_session.commit = AsyncMock()
        
        # Act
        result = await self.repository.delete_campaign(campaign_id)
        
        # Assert
        assert result is True
        self.mock_session.delete.assert_called_once_with(self.sample_campaign)
        self.mock_session.commit.assert_called_once()
    
    async def test_delete_campaign_not_found(self):
        """Test campaign deletion when campaign not found."""
        # Arrange
        campaign_id = uuid4()
        self.repository.get_campaign_by_id = AsyncMock(return_value=None)
        
        # Act
        result = await self.repository.delete_campaign(campaign_id)
        
        # Assert
        assert result is False
    
    async def test_get_campaigns_starting_soon(self):
        """Test retrieving campaigns starting soon."""
        # Arrange
        campaigns = [self.sample_campaign]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = campaigns
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_campaigns_starting_soon(hours_ahead=12)
        
        # Assert
        assert result == campaigns
        self.mock_session.execute.assert_called_once()
    
    async def test_count_campaigns_by_guild(self):
        """Test counting campaigns by guild."""
        # Arrange
        guild_id = "123456789"
        expected_count = 5
        
        mock_result = Mock()
        mock_result.scalar.return_value = expected_count
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.count_campaigns_by_guild(guild_id)
        
        # Assert
        assert result == expected_count
        self.mock_session.execute.assert_called_once()
    
    async def test_count_campaigns_by_guild_with_state_filter(self):
        """Test counting campaigns by guild with state filter."""
        # Arrange
        guild_id = "123456789"
        state = "active"
        expected_count = 3
        
        mock_result = Mock()
        mock_result.scalar.return_value = expected_count
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.count_campaigns_by_guild(guild_id, state=state)
        
        # Assert
        assert result == expected_count
        self.mock_session.execute.assert_called_once()
    
    async def test_get_campaign_statistics(self):
        """Test retrieving campaign statistics."""
        # Arrange
        guild_id = "123456789"
        
        # Mock multiple query results
        total_result = Mock()
        total_result.scalar.return_value = 10
        
        state_result = Mock()
        state_result.fetchall.return_value = [("active", 5), ("completed", 3), ("draft", 2)]
        
        type_result = Mock()
        type_result.fetchall.return_value = [("player", 7), ("squad", 3)]
        
        self.mock_session.execute = AsyncMock(
            side_effect=[total_result, state_result, type_result]
        )
        
        # Act
        result = await self.repository.get_campaign_statistics(guild_id)
        
        # Assert
        assert result["total_campaigns"] == 10
        assert result["campaigns_by_state"] == {"active": 5, "completed": 3, "draft": 2}
        assert result["campaigns_by_type"] == {"player": 7, "squad": 3}
        assert "date_range" in result
        
        # Should have called execute 3 times (total, by state, by type)
        assert self.mock_session.execute.call_count == 3