"""Simple test cases for the Challenge model focusing on model functionality."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

from smarter_dev.web.models import Challenge


class TestChallengeModelSimple:
    """Test cases for Challenge model functionality without database."""
    
    def test_challenge_creation_with_defaults(self):
        """Test that challenge can be created with default values."""
        campaign_id = uuid4()
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="Test Challenge",
            description="A test challenge description",
            generation_script="print('test')"
        )
        
        # Check defaults are applied
        assert isinstance(challenge.id, UUID)
        assert challenge.categories == []
        assert challenge.difficulty_level is None
        assert challenge.script_updated_at is not None
        assert isinstance(challenge.script_updated_at, datetime)
        assert challenge.created_at is not None
        assert challenge.updated_at is not None
    
    def test_challenge_creation_with_custom_values(self):
        """Test challenge creation with custom values."""
        custom_id = uuid4()
        campaign_id = uuid4()
        categories = ["math", "algorithms"]
        
        challenge = Challenge(
            id=custom_id,
            campaign_id=campaign_id,
            order_position=3,
            title="Custom Challenge",
            description="A custom challenge with all fields set",
            generation_script="import random; print(random.randint(1, 100))",
            categories=categories,
            difficulty_level=7
        )
        
        # Verify custom values
        assert challenge.id == custom_id
        assert challenge.campaign_id == campaign_id
        assert challenge.order_position == 3
        assert challenge.title == "Custom Challenge"
        assert challenge.description == "A custom challenge with all fields set"
        assert challenge.generation_script == "import random; print(random.randint(1, 100))"
        assert challenge.categories == categories
        assert challenge.difficulty_level == 7
    
    def test_challenge_required_fields(self):
        """Test that all required fields are set."""
        campaign_id = uuid4()
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="Required Fields Test",
            description="Testing required fields",
            generation_script="# Test script"
        )
        
        assert challenge.campaign_id == campaign_id
        assert challenge.order_position == 1
        assert challenge.title == "Required Fields Test"
        assert challenge.description == "Testing required fields"
        assert challenge.generation_script == "# Test script"
    
    def test_is_first_challenge_property(self):
        """Test the is_first_challenge property."""
        campaign_id = uuid4()
        
        # First challenge
        first_challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="First Challenge",
            description="The first challenge",
            generation_script="print('first')"
        )
        
        assert first_challenge.is_first_challenge is True
        
        # Not first challenge
        second_challenge = Challenge(
            campaign_id=campaign_id,
            order_position=2,
            title="Second Challenge",
            description="The second challenge",
            generation_script="print('second')"
        )
        
        assert second_challenge.is_first_challenge is False
        
        # High order position
        tenth_challenge = Challenge(
            campaign_id=campaign_id,
            order_position=10,
            title="Tenth Challenge",
            description="The tenth challenge",
            generation_script="print('tenth')"
        )
        
        assert tenth_challenge.is_first_challenge is False
    
    def test_get_release_time_calculation(self):
        """Test challenge release time calculation."""
        campaign_id = uuid4()
        start_date = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        release_delay = 60  # 1 hour
        
        # First challenge (order_position = 1) should be released immediately
        first_challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="First Challenge",
            description="Released immediately",
            generation_script="print('first')"
        )
        
        release_time = first_challenge.get_release_time(start_date, release_delay)
        assert release_time == start_date  # No delay for first challenge
        
        # Second challenge should be released after 1 hour
        second_challenge = Challenge(
            campaign_id=campaign_id,
            order_position=2,
            title="Second Challenge",
            description="Released after 1 hour",
            generation_script="print('second')"
        )
        
        release_time = second_challenge.get_release_time(start_date, release_delay)
        expected_time = start_date + timedelta(minutes=60)
        assert release_time == expected_time
        
        # Fifth challenge should be released after 4 hours (4 * 60 minutes)
        fifth_challenge = Challenge(
            campaign_id=campaign_id,
            order_position=5,
            title="Fifth Challenge",
            description="Released after 4 hours",
            generation_script="print('fifth')"
        )
        
        release_time = fifth_challenge.get_release_time(start_date, release_delay)
        expected_time = start_date + timedelta(minutes=240)  # 4 * 60
        assert release_time == expected_time
    
    def test_is_released_timing(self):
        """Test challenge release status checking."""
        campaign_id = uuid4()
        start_date = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        release_delay = 60  # 1 hour
        
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=2,  # Should be released 1 hour after start
            title="Timed Challenge",
            description="Testing release timing",
            generation_script="print('timed')"
        )
        
        # Before release time
        before_release = start_date + timedelta(minutes=30)  # 30 minutes after start
        assert challenge.is_released(start_date, release_delay, before_release) is False
        
        # At exact release time
        at_release = start_date + timedelta(minutes=60)  # 1 hour after start
        assert challenge.is_released(start_date, release_delay, at_release) is True
        
        # After release time
        after_release = start_date + timedelta(minutes=90)  # 1.5 hours after start
        assert challenge.is_released(start_date, release_delay, after_release) is True
    
    def test_is_released_without_current_time(self):
        """Test is_released method when no current_time is provided."""
        campaign_id = uuid4()
        # Set start date far in the past to ensure challenge is released
        start_date = datetime(2020, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        release_delay = 60
        
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="Always Released Challenge",
            description="Should always be released due to past start date",
            generation_script="print('always released')"
        )
        
        # Should use current time and return True since start date is in past
        assert challenge.is_released(start_date, release_delay) is True
    
    def test_update_script_method(self):
        """Test the update_script method."""
        campaign_id = uuid4()
        original_script = "print('original')"
        new_script = "print('updated')"
        
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="Script Update Test",
            description="Testing script updates",
            generation_script=original_script
        )
        
        original_timestamp = challenge.script_updated_at
        
        # Small delay to ensure timestamp difference
        import time
        time.sleep(0.001)
        
        # Update the script
        challenge.update_script(new_script)
        
        assert challenge.generation_script == new_script
        assert challenge.script_updated_at > original_timestamp
    
    def test_challenge_repr(self):
        """Test challenge string representation."""
        campaign_id = uuid4()
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=3,
            title="Repr Challenge",
            description="Testing string representation",
            generation_script="print('repr')"
        )
        
        repr_str = repr(challenge)
        assert "Repr Challenge" in repr_str
        assert "position=3" in repr_str
        assert str(campaign_id) in repr_str
        assert repr_str.startswith("<Challenge(")
        assert repr_str.endswith(")>")
    
    def test_challenge_id_generation(self):
        """Test that challenge IDs are generated uniquely."""
        campaign_id = uuid4()
        
        challenge1 = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="Challenge 1",
            description="First challenge",
            generation_script="print('1')"
        )
        
        challenge2 = Challenge(
            campaign_id=campaign_id,
            order_position=2,
            title="Challenge 2",
            description="Second challenge",
            generation_script="print('2')"
        )
        
        assert challenge1.id != challenge2.id
        assert isinstance(challenge1.id, UUID)
        assert isinstance(challenge2.id, UUID)
    
    def test_categories_default_empty_list(self):
        """Test that categories defaults to empty list."""
        campaign_id = uuid4()
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="Categories Test",
            description="Testing default categories",
            generation_script="print('categories')"
        )
        
        assert challenge.categories == []
        assert isinstance(challenge.categories, list)
    
    def test_categories_custom_list(self):
        """Test setting custom categories."""
        campaign_id = uuid4()
        custom_categories = ["algorithm", "sorting", "complexity"]
        
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="Custom Categories",
            description="Testing custom categories",
            generation_script="print('custom categories')",
            categories=custom_categories
        )
        
        assert challenge.categories == custom_categories
    
    def test_difficulty_level_optional(self):
        """Test that difficulty level is optional."""
        campaign_id = uuid4()
        
        # Without difficulty level
        challenge_no_difficulty = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="No Difficulty",
            description="No difficulty set",
            generation_script="print('no difficulty')"
        )
        
        assert challenge_no_difficulty.difficulty_level is None
        
        # With difficulty level
        challenge_with_difficulty = Challenge(
            campaign_id=campaign_id,
            order_position=2,
            title="With Difficulty",
            description="Difficulty level set",
            generation_script="print('with difficulty')",
            difficulty_level=5
        )
        
        assert challenge_with_difficulty.difficulty_level == 5
    
    def test_title_length_handling(self):
        """Test title length constraints."""
        campaign_id = uuid4()
        
        # Normal length title
        normal_title = "A" * 100  # Within the 200 character limit
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title=normal_title,
            description="Testing title length",
            generation_script="print('title test')"
        )
        
        assert challenge.title == normal_title
        assert len(challenge.title) == 100
    
    def test_long_description_handling(self):
        """Test that long descriptions are handled properly."""
        campaign_id = uuid4()
        long_description = "A" * 1000  # Text field should handle this
        
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="Long Description Test",
            description=long_description,
            generation_script="print('long description')"
        )
        
        assert challenge.description == long_description
        assert len(challenge.description) == 1000
    
    def test_generation_script_storage(self):
        """Test that generation scripts can be stored properly."""
        campaign_id = uuid4()
        complex_script = """
import random
import json

def generate_input():
    numbers = [random.randint(1, 100) for _ in range(10)]
    target = random.randint(1, 200)
    return {
        "numbers": numbers,
        "target": target
    }

if __name__ == "__main__":
    result = generate_input()
    print(json.dumps(result))
"""
        
        challenge = Challenge(
            campaign_id=campaign_id,
            order_position=1,
            title="Complex Script Test",
            description="Testing complex generation script storage",
            generation_script=complex_script
        )
        
        assert challenge.generation_script == complex_script
        assert "import random" in challenge.generation_script
        assert "def generate_input():" in challenge.generation_script