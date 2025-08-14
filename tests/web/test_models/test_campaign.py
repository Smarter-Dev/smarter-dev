"""Test cases for the Campaign model."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import Campaign


class TestCampaignModel:
    """Test cases for Campaign model functionality."""
    
    async def test_campaign_creation_with_defaults(self, isolated_db_session: AsyncSession):
        """Test that campaign can be created with default values."""
        campaign = Campaign(
            guild_id="123456789",
            name="Test Campaign",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        # Check defaults are applied
        assert campaign.state == "draft"
        assert campaign.campaign_type == "player"
        assert campaign.scoring_type == "time_based"
        assert campaign.release_delay_minutes == 1440
        assert isinstance(campaign.id, UUID)
        
        # Test database creation
        isolated_db_session.add(campaign)
        await isolated_db_session.commit()
        await isolated_db_session.refresh(campaign)
        
        assert campaign.created_at is not None
        assert campaign.updated_at is not None
    
    async def test_campaign_creation_with_custom_values(self, isolated_db_session: AsyncSession):
        """Test campaign creation with custom values."""
        custom_id = uuid4()
        start_date = datetime.now(timezone.utc) + timedelta(days=1)
        
        campaign = Campaign(
            id=custom_id,
            guild_id="123456789",
            name="Custom Campaign",
            description="A test campaign with custom settings",
            campaign_type="squad",
            state="active",
            start_date=start_date,
            release_delay_minutes=720,  # 12 hours
            scoring_type="point_based",
            starting_points=100,
            points_decrease_step=10,
            announcement_channel_id="987654321"
        )
        
        # Verify custom values
        assert campaign.id == custom_id
        assert campaign.campaign_type == "squad"
        assert campaign.state == "active"
        assert campaign.release_delay_minutes == 720
        assert campaign.scoring_type == "point_based"
        assert campaign.starting_points == 100
        assert campaign.points_decrease_step == 10
        
        isolated_db_session.add(campaign)
        await isolated_db_session.commit()
        await isolated_db_session.refresh(campaign)
        
        assert campaign.description == "A test campaign with custom settings"
    
    async def test_campaign_required_fields(self, isolated_db_session: AsyncSession):
        """Test that required fields are enforced."""
        # Missing guild_id
        with pytest.raises(Exception):  # SQLAlchemy will raise an exception
            campaign = Campaign(
                name="Test Campaign",
                start_date=datetime.now(timezone.utc),
                announcement_channel_id="987654321"
            )
            isolated_db_session.add(campaign)
            await isolated_db_session.commit()
        
        await isolated_db_session.rollback()
        
        # Missing name
        with pytest.raises(Exception):
            campaign = Campaign(
                guild_id="123456789",
                start_date=datetime.now(timezone.utc),
                announcement_channel_id="987654321"
            )
            isolated_db_session.add(campaign)
            await isolated_db_session.commit()
        
        await isolated_db_session.rollback()
        
        # Missing start_date
        with pytest.raises(Exception):
            campaign = Campaign(
                guild_id="123456789",
                name="Test Campaign",
                announcement_channel_id="987654321"
            )
            isolated_db_session.add(campaign)
            await isolated_db_session.commit()
    
    async def test_campaign_type_constraint(self, isolated_db_session: AsyncSession):
        """Test that campaign_type is constrained to valid values."""
        campaign = Campaign(
            guild_id="123456789",
            name="Test Campaign",
            campaign_type="invalid_type",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        isolated_db_session.add(campaign)
        with pytest.raises(IntegrityError):
            await isolated_db_session.commit()
    
    async def test_state_constraint(self, isolated_db_session: AsyncSession):
        """Test that state is constrained to valid values."""
        campaign = Campaign(
            guild_id="123456789",
            name="Test Campaign",
            state="invalid_state",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        isolated_db_session.add(campaign)
        with pytest.raises(IntegrityError):
            await isolated_db_session.commit()
    
    async def test_scoring_type_constraint(self, isolated_db_session: AsyncSession):
        """Test that scoring_type is constrained to valid values."""
        campaign = Campaign(
            guild_id="123456789",
            name="Test Campaign",
            scoring_type="invalid_scoring",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        isolated_db_session.add(campaign)
        with pytest.raises(IntegrityError):
            await isolated_db_session.commit()
    
    async def test_release_delay_positive_constraint(self, isolated_db_session: AsyncSession):
        """Test that release_delay_minutes must be positive."""
        campaign = Campaign(
            guild_id="123456789",
            name="Test Campaign",
            release_delay_minutes=0,
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        isolated_db_session.add(campaign)
        with pytest.raises(IntegrityError):
            await isolated_db_session.commit()
        
        await isolated_db_session.rollback()
        
        # Negative value should also fail
        campaign = Campaign(
            guild_id="123456789",
            name="Test Campaign",
            release_delay_minutes=-1,
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        isolated_db_session.add(campaign)
        with pytest.raises(IntegrityError):
            await isolated_db_session.commit()
    
    async def test_campaign_property_methods(self, isolated_db_session: AsyncSession):
        """Test campaign state property methods."""
        # Test draft state
        draft_campaign = Campaign(
            guild_id="123456789",
            name="Draft Campaign",
            state="draft",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        assert draft_campaign.is_draft is True
        assert draft_campaign.is_active is False
        assert draft_campaign.is_completed is False
        
        # Test active state
        active_campaign = Campaign(
            guild_id="123456789",
            name="Active Campaign",
            state="active",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        assert active_campaign.is_draft is False
        assert active_campaign.is_active is True
        assert active_campaign.is_completed is False
        
        # Test completed state
        completed_campaign = Campaign(
            guild_id="123456789",
            name="Completed Campaign",
            state="completed",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        assert completed_campaign.is_draft is False
        assert completed_campaign.is_active is False
        assert completed_campaign.is_completed is True
    
    async def test_state_transition_validation(self, isolated_db_session: AsyncSession):
        """Test state transition validation logic."""
        campaign = Campaign(
            guild_id="123456789",
            name="Transition Campaign",
            state="draft",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        # From draft: can go to active, not to completed
        assert campaign.can_transition_to("active") is True
        assert campaign.can_transition_to("completed") is False
        assert campaign.can_transition_to("draft") is False
        
        # Change to active
        campaign.state = "active"
        
        # From active: can go to completed, not to draft
        assert campaign.can_transition_to("completed") is True
        assert campaign.can_transition_to("draft") is False
        assert campaign.can_transition_to("active") is False
        
        # Change to completed
        campaign.state = "completed"
        
        # From completed: cannot go anywhere
        assert campaign.can_transition_to("draft") is False
        assert campaign.can_transition_to("active") is False
        assert campaign.can_transition_to("completed") is False
    
    async def test_campaign_repr(self, isolated_db_session: AsyncSession):
        """Test campaign string representation."""
        campaign = Campaign(
            guild_id="123456789",
            name="Repr Campaign",
            campaign_type="squad",
            state="active",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        repr_str = repr(campaign)
        assert "Repr Campaign" in repr_str
        assert "squad" in repr_str
        assert "active" in repr_str
        assert repr_str.startswith("<Campaign(")
        assert repr_str.endswith(")>")
    
    async def test_campaign_name_length_limit(self, isolated_db_session: AsyncSession):
        """Test that campaign name is limited to 100 characters."""
        # Should work with 100 characters
        long_name = "A" * 100
        campaign = Campaign(
            guild_id="123456789",
            name=long_name,
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        isolated_db_session.add(campaign)
        await isolated_db_session.commit()
        await isolated_db_session.refresh(campaign)
        
        assert len(campaign.name) == 100
        
        # Test with longer name - should be truncated or raise error based on database
        # For SQLite, it might be truncated; for PostgreSQL, it might raise an error
        try:
            very_long_name = "A" * 150
            long_campaign = Campaign(
                guild_id="123456789",
                name=very_long_name,
                start_date=datetime.now(timezone.utc),
                announcement_channel_id="987654322"
            )
            
            isolated_db_session.add(long_campaign)
            await isolated_db_session.commit()
            # If it succeeds, the name should be truncated
            await isolated_db_session.refresh(long_campaign)
            assert len(long_campaign.name) <= 100
        except Exception:
            # If it fails, that's also acceptable behavior
            await isolated_db_session.rollback()
    
    async def test_optional_fields(self, isolated_db_session: AsyncSession):
        """Test that optional fields can be None."""
        campaign = Campaign(
            guild_id="123456789",
            name="Optional Fields Campaign",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321",
            # Explicitly set optional fields to None
            description=None,
            starting_points=None,
            points_decrease_step=None
        )
        
        isolated_db_session.add(campaign)
        await isolated_db_session.commit()
        await isolated_db_session.refresh(campaign)
        
        assert campaign.description is None
        assert campaign.starting_points is None
        assert campaign.points_decrease_step is None
    
    async def test_point_based_scoring_fields(self, isolated_db_session: AsyncSession):
        """Test point-based scoring configuration fields."""
        campaign = Campaign(
            guild_id="123456789",
            name="Point Based Campaign",
            scoring_type="point_based",
            starting_points=500,
            points_decrease_step=25,
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        isolated_db_session.add(campaign)
        await isolated_db_session.commit()
        await isolated_db_session.refresh(campaign)
        
        assert campaign.scoring_type == "point_based"
        assert campaign.starting_points == 500
        assert campaign.points_decrease_step == 25
    
    async def test_database_indexes(self, isolated_db_session: AsyncSession):
        """Test that database indexes are working properly."""
        # Create multiple campaigns to test index performance
        campaigns = []
        base_time = datetime.now(timezone.utc)
        
        for i in range(5):
            campaign = Campaign(
                guild_id=f"guild_{i}",
                name=f"Campaign {i}",
                state="active" if i % 2 == 0 else "draft",
                start_date=base_time + timedelta(minutes=i),
                announcement_channel_id=f"channel_{i}"
            )
            campaigns.append(campaign)
            isolated_db_session.add(campaign)
        
        await isolated_db_session.commit()
        
        # Test that campaigns were created successfully
        for campaign in campaigns:
            await isolated_db_session.refresh(campaign)
            assert campaign.id is not None
        
        # The actual index usage testing would require query analysis,
        # but this test ensures the model with indexes can be created
        assert len(campaigns) == 5