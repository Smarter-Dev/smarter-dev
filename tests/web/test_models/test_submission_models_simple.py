"""Simple test cases for the submission models focusing on model functionality."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4
from unittest.mock import Mock

from smarter_dev.web.models import GeneratedInputCache, ChallengeSubmission, SubmissionRateLimit


class TestGeneratedInputCacheModelSimple:
    """Test cases for GeneratedInputCache model functionality without database."""
    
    def test_generated_input_cache_creation_with_defaults(self):
        """Test that cache can be created with default values."""
        challenge_id = uuid4()
        cache_entry = GeneratedInputCache(
            challenge_id=challenge_id,
            participant_id="player123",
            participant_type="player",
            input_json={"numbers": [1, 2, 3], "target": 6},
            expected_result="6"
        )
        
        # Check defaults are applied
        assert isinstance(cache_entry.id, UUID)
        assert cache_entry.is_valid is True
        assert cache_entry.generation_timestamp is not None
        assert cache_entry.first_request_timestamp is None
        assert cache_entry.created_at is not None
        assert cache_entry.updated_at is not None
    
    def test_generated_input_cache_custom_values(self):
        """Test cache creation with custom values."""
        custom_id = uuid4()
        challenge_id = uuid4()
        generation_time = datetime.now(timezone.utc) - timedelta(hours=1)
        
        cache_entry = GeneratedInputCache(
            id=custom_id,
            challenge_id=challenge_id,
            participant_id="squad456",
            participant_type="squad",
            input_json={"data": "test data", "seed": 42},
            expected_result="expected output",
            is_valid=False,
            generation_timestamp=generation_time
        )
        
        assert cache_entry.id == custom_id
        assert cache_entry.challenge_id == challenge_id
        assert cache_entry.participant_id == "squad456"
        assert cache_entry.participant_type == "squad"
        assert cache_entry.input_json == {"data": "test data", "seed": 42}
        assert cache_entry.expected_result == "expected output"
        assert cache_entry.is_valid is False
        assert cache_entry.generation_timestamp == generation_time
    
    def test_invalidate_method(self):
        """Test the invalidate method."""
        challenge_id = uuid4()
        cache_entry = GeneratedInputCache(
            challenge_id=challenge_id,
            participant_id="player123",
            participant_type="player",
            input_json={"test": "data"},
            expected_result="result"
        )
        
        assert cache_entry.is_valid is True
        
        cache_entry.invalidate()
        
        assert cache_entry.is_valid is False
    
    def test_mark_first_request_method(self):
        """Test the mark_first_request method."""
        challenge_id = uuid4()
        cache_entry = GeneratedInputCache(
            challenge_id=challenge_id,
            participant_id="player123",
            participant_type="player",
            input_json={"test": "data"},
            expected_result="result"
        )
        
        assert cache_entry.first_request_timestamp is None
        
        # First call should set the timestamp
        cache_entry.mark_first_request()
        
        first_timestamp = cache_entry.first_request_timestamp
        assert first_timestamp is not None
        assert isinstance(first_timestamp, datetime)
        
        # Second call should not change the timestamp
        cache_entry.mark_first_request()
        assert cache_entry.first_request_timestamp == first_timestamp
    
    def test_generated_input_cache_repr(self):
        """Test cache string representation."""
        challenge_id = uuid4()
        cache_entry = GeneratedInputCache(
            challenge_id=challenge_id,
            participant_id="player123",
            participant_type="player",
            input_json={"test": "data"},
            expected_result="result"
        )
        
        repr_str = repr(cache_entry)
        assert "GeneratedInputCache" in repr_str
        assert str(challenge_id) in repr_str
        assert "player123" in repr_str
        assert "valid=True" in repr_str


class TestChallengeSubmissionModelSimple:
    """Test cases for ChallengeSubmission model functionality without database."""
    
    def test_challenge_submission_creation_with_defaults(self):
        """Test that submission can be created with default values."""
        challenge_id = uuid4()
        submission = ChallengeSubmission(
            challenge_id=challenge_id,
            participant_id="player123",
            participant_type="player",
            submitted_result="42",
            is_correct=True
        )
        
        # Check defaults are applied
        assert isinstance(submission.id, UUID)
        assert submission.points_awarded == 0
        assert submission.submission_timestamp is not None
        assert submission.created_at is not None
        assert submission.updated_at is not None
    
    def test_challenge_submission_custom_values(self):
        """Test submission creation with custom values."""
        custom_id = uuid4()
        challenge_id = uuid4()
        submission_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        
        submission = ChallengeSubmission(
            id=custom_id,
            challenge_id=challenge_id,
            participant_id="squad456",
            participant_type="squad",
            submitted_result="incorrect answer",
            is_correct=False,
            points_awarded=0,
            submission_timestamp=submission_time
        )
        
        assert submission.id == custom_id
        assert submission.challenge_id == challenge_id
        assert submission.participant_id == "squad456"
        assert submission.participant_type == "squad"
        assert submission.submitted_result == "incorrect answer"
        assert submission.is_correct is False
        assert submission.points_awarded == 0
        assert submission.submission_timestamp == submission_time
    
    def test_successful_submission_with_points(self):
        """Test successful submission with points awarded."""
        challenge_id = uuid4()
        submission = ChallengeSubmission(
            challenge_id=challenge_id,
            participant_id="player123",
            participant_type="player",
            submitted_result="correct answer",
            is_correct=True,
            points_awarded=100
        )
        
        assert submission.is_correct is True
        assert submission.points_awarded == 100
        assert submission.is_successful is True
    
    def test_unsuccessful_submission(self):
        """Test unsuccessful submission."""
        challenge_id = uuid4()
        submission = ChallengeSubmission(
            challenge_id=challenge_id,
            participant_id="player123",
            participant_type="player",
            submitted_result="wrong answer",
            is_correct=False,
            points_awarded=0
        )
        
        assert submission.is_correct is False
        assert submission.points_awarded == 0
        assert submission.is_successful is False
    
    def test_is_successful_property(self):
        """Test the is_successful property."""
        challenge_id = uuid4()
        
        # Successful submission
        successful_submission = ChallengeSubmission(
            challenge_id=challenge_id,
            participant_id="player123",
            participant_type="player",
            submitted_result="correct",
            is_correct=True
        )
        assert successful_submission.is_successful is True
        
        # Unsuccessful submission
        unsuccessful_submission = ChallengeSubmission(
            challenge_id=challenge_id,
            participant_id="player456",
            participant_type="player",
            submitted_result="incorrect",
            is_correct=False
        )
        assert unsuccessful_submission.is_successful is False
    
    def test_challenge_submission_repr(self):
        """Test submission string representation."""
        challenge_id = uuid4()
        submission = ChallengeSubmission(
            challenge_id=challenge_id,
            participant_id="player123",
            participant_type="player",
            submitted_result="answer",
            is_correct=True,
            points_awarded=50
        )
        
        repr_str = repr(submission)
        assert "ChallengeSubmission" in repr_str
        assert str(challenge_id) in repr_str
        assert "player123" in repr_str
        assert "correct" in repr_str
        assert "points=50" in repr_str


class TestSubmissionRateLimitModelSimple:
    """Test cases for SubmissionRateLimit model functionality without database."""
    
    def test_submission_rate_limit_creation_with_defaults(self):
        """Test that rate limit entry can be created with default values."""
        rate_limit = SubmissionRateLimit(
            participant_id="player123",
            participant_type="player"
        )
        
        # Check defaults are applied
        assert isinstance(rate_limit.id, UUID)
        assert rate_limit.submission_timestamp is not None
        assert rate_limit.created_at is not None
        assert rate_limit.updated_at is not None
    
    def test_submission_rate_limit_custom_values(self):
        """Test rate limit creation with custom values."""
        custom_id = uuid4()
        custom_timestamp = datetime.now(timezone.utc) - timedelta(minutes=2)
        
        rate_limit = SubmissionRateLimit(
            id=custom_id,
            participant_id="squad456",
            participant_type="squad",
            submission_timestamp=custom_timestamp
        )
        
        assert rate_limit.id == custom_id
        assert rate_limit.participant_id == "squad456"
        assert rate_limit.participant_type == "squad"
        assert rate_limit.submission_timestamp == custom_timestamp
    
    def test_is_rate_limited_method_no_limits(self):
        """Test rate limiting when no previous submissions exist."""
        # Mock session with no existing submissions
        mock_session = Mock()
        mock_query = Mock()
        mock_query.filter.return_value.count.return_value = 0
        mock_session.query.return_value = mock_query
        
        result = SubmissionRateLimit.is_rate_limited(
            mock_session,
            "player123",
            "player",
            max_per_minute=1,
            max_per_5_minutes=3
        )
        
        assert result is False
    
    def test_is_rate_limited_method_minute_limit_exceeded(self):
        """Test rate limiting when minute limit is exceeded."""
        mock_session = Mock()
        mock_query = Mock()
        
        # First call (1-minute check) returns 1, which equals max_per_minute=1
        mock_query.filter.return_value.count.return_value = 1
        mock_session.query.return_value = mock_query
        
        result = SubmissionRateLimit.is_rate_limited(
            mock_session,
            "player123",
            "player",
            max_per_minute=1,
            max_per_5_minutes=3
        )
        
        assert result is True
    
    def test_is_rate_limited_method_five_minute_limit_exceeded(self):
        """Test rate limiting when 5-minute limit is exceeded."""
        mock_session = Mock()
        mock_query = Mock()
        
        # Mock the count to return different values for each call
        # First call (1-minute check) returns 0 (under limit)  
        # Second call (5-minute check) returns 3 (at/over limit for max_per_5_minutes=3)
        mock_query.filter.return_value.count.side_effect = [0, 3]
        mock_session.query.return_value = mock_query
        
        result = SubmissionRateLimit.is_rate_limited(
            mock_session,
            "player123",
            "player",
            max_per_minute=1,
            max_per_5_minutes=3
        )
        
        assert result is True
    
    def test_is_rate_limited_method_under_limits(self):
        """Test rate limiting when under all limits."""
        mock_session = Mock()
        mock_query = Mock()
        
        # Both minute and 5-minute checks return counts under limits
        mock_query.filter.return_value.count.side_effect = [0, 2]  # Under limits
        mock_session.query.return_value = mock_query
        
        result = SubmissionRateLimit.is_rate_limited(
            mock_session,
            "player123",
            "player",
            max_per_minute=1,
            max_per_5_minutes=3
        )
        
        assert result is False
    
    def test_is_rate_limited_with_custom_time(self):
        """Test rate limiting with custom current time."""
        mock_session = Mock()
        mock_query = Mock()
        mock_query.filter.return_value.count.return_value = 0
        mock_session.query.return_value = mock_query
        
        custom_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        
        result = SubmissionRateLimit.is_rate_limited(
            mock_session,
            "player123",
            "player",
            current_time=custom_time
        )
        
        assert result is False
        # Verify the session.query was called (indicating the method ran)
        mock_session.query.assert_called()
    
    def test_submission_rate_limit_repr(self):
        """Test rate limit string representation."""
        timestamp = datetime.now(timezone.utc)
        rate_limit = SubmissionRateLimit(
            participant_id="player123",
            participant_type="player",
            submission_timestamp=timestamp
        )
        
        repr_str = repr(rate_limit)
        assert "SubmissionRateLimit" in repr_str
        assert "player123" in repr_str
        assert "type='player'" in repr_str
        assert str(timestamp) in repr_str
    
    def test_different_participant_types(self):
        """Test rate limit entries for different participant types."""
        player_rate_limit = SubmissionRateLimit(
            participant_id="player123",
            participant_type="player"
        )
        
        squad_rate_limit = SubmissionRateLimit(
            participant_id="squad456",
            participant_type="squad"
        )
        
        assert player_rate_limit.participant_type == "player"
        assert squad_rate_limit.participant_type == "squad"
        assert player_rate_limit.participant_id != squad_rate_limit.participant_id
    
    def test_unique_id_generation(self):
        """Test that rate limit entries have unique IDs."""
        rate_limit1 = SubmissionRateLimit(
            participant_id="player123",
            participant_type="player"
        )
        
        rate_limit2 = SubmissionRateLimit(
            participant_id="player123",
            participant_type="player"
        )
        
        assert rate_limit1.id != rate_limit2.id
        assert isinstance(rate_limit1.id, UUID)
        assert isinstance(rate_limit2.id, UUID)