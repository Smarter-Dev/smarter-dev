"""Simple test cases for the Campaign model focusing on model functionality."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

from smarter_dev.web.models import Campaign


class TestCampaignModelSimple:
    """Test cases for Campaign model functionality without database."""
    
    def test_campaign_creation_with_defaults(self):
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
    
    def test_campaign_creation_with_custom_values(self):
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
        assert campaign.description == "A test campaign with custom settings"
    
    def test_campaign_property_methods(self):
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
    
    def test_state_transition_validation(self):
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
    
    def test_campaign_repr(self):
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
    
    def test_optional_fields(self):
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
        
        assert campaign.description is None
        assert campaign.starting_points is None
        assert campaign.points_decrease_step is None
    
    def test_point_based_scoring_fields(self):
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
        
        assert campaign.scoring_type == "point_based"
        assert campaign.starting_points == 500
        assert campaign.points_decrease_step == 25
    
    def test_time_based_scoring_default(self):
        """Test time-based scoring is the default."""
        campaign = Campaign(
            guild_id="123456789",
            name="Time Based Campaign",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        assert campaign.scoring_type == "time_based"
        assert campaign.starting_points is None
        assert campaign.points_decrease_step is None
    
    def test_campaign_id_generation(self):
        """Test that campaign IDs are generated uniquely."""
        campaign1 = Campaign(
            guild_id="123456789",
            name="Campaign 1",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        campaign2 = Campaign(
            guild_id="123456789",
            name="Campaign 2",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        assert campaign1.id != campaign2.id
        assert isinstance(campaign1.id, UUID)
        assert isinstance(campaign2.id, UUID)
    
    def test_campaign_timestamp_initialization(self):
        """Test that campaigns initialize with timestamps."""
        before_creation = datetime.now(timezone.utc)
        
        campaign = Campaign(
            guild_id="123456789",
            name="Timestamp Campaign",
            start_date=datetime.now(timezone.utc),
            announcement_channel_id="987654321"
        )
        
        after_creation = datetime.now(timezone.utc)
        
        assert campaign.created_at is not None
        assert campaign.updated_at is not None
        assert before_creation <= campaign.created_at <= after_creation
        assert before_creation <= campaign.updated_at <= after_creation