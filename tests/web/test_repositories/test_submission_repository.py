"""Test cases for the Submission Repository."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4
from unittest.mock import AsyncMock, Mock
from sqlalchemy.exc import IntegrityError

from web.repositories.submission_repository import SubmissionRepository
from smarter_dev.web.models import ChallengeSubmission, GeneratedInputCache, SubmissionRateLimit


class TestSubmissionRepository:
    """Test cases for Submission Repository functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_session = AsyncMock()
        self.repository = SubmissionRepository(self.mock_session)
        self.sample_challenge_id = uuid4()
        self.sample_submission = ChallengeSubmission(
            id=uuid4(),
            challenge_id=self.sample_challenge_id,
            participant_id="player123",
            participant_type="player",
            submitted_result="correct answer",
            is_correct=True,
            points_awarded=100
        )
        self.sample_cache = GeneratedInputCache(
            id=uuid4(),
            challenge_id=self.sample_challenge_id,
            participant_id="player123",
            participant_type="player",
            input_json={"test": "data"},
            expected_result="expected"
        )
    
    async def test_create_submission_success(self):
        """Test successful submission creation."""
        # Arrange
        challenge_id = self.sample_challenge_id
        participant_id = "player123"
        participant_type = "player"
        submitted_result = "my answer"
        is_correct = True
        points_awarded = 50
        
        # Mock session behavior
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.create_submission(
            challenge_id=challenge_id,
            participant_id=participant_id,
            participant_type=participant_type,
            submitted_result=submitted_result,
            is_correct=is_correct,
            points_awarded=points_awarded
        )
        
        # Assert
        assert result is not None
        assert isinstance(result, ChallengeSubmission)
        assert result.challenge_id == challenge_id
        assert result.participant_id == participant_id
        assert result.participant_type == participant_type
        assert result.submitted_result == submitted_result
        assert result.is_correct == is_correct
        assert result.points_awarded == points_awarded
        
        self.mock_session.add.assert_called_once()
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_create_submission_validation_errors(self):
        """Test submission creation validation errors."""
        challenge_id = self.sample_challenge_id
        
        # Invalid challenge_id
        with pytest.raises(ValueError, match="Invalid challenge_id format"):
            await self.repository.create_submission(
                challenge_id="not-a-uuid",
                participant_id="player123",
                participant_type="player",
                submitted_result="answer",
                is_correct=True
            )
        
        # Empty participant_id
        with pytest.raises(ValueError, match="Participant ID cannot be empty"):
            await self.repository.create_submission(
                challenge_id=challenge_id,
                participant_id="",
                participant_type="player",
                submitted_result="answer",
                is_correct=True
            )
        
        # Invalid participant_type
        with pytest.raises(ValueError, match="Participant type must be"):
            await self.repository.create_submission(
                challenge_id=challenge_id,
                participant_id="player123",
                participant_type="invalid",
                submitted_result="answer",
                is_correct=True
            )
        
        # Empty submitted_result
        with pytest.raises(ValueError, match="Submitted result cannot be empty"):
            await self.repository.create_submission(
                challenge_id=challenge_id,
                participant_id="player123",
                participant_type="player",
                submitted_result="",
                is_correct=True
            )
        
        # Negative points
        with pytest.raises(ValueError, match="Points awarded cannot be negative"):
            await self.repository.create_submission(
                challenge_id=challenge_id,
                participant_id="player123",
                participant_type="player",
                submitted_result="answer",
                is_correct=True,
                points_awarded=-10
            )
    
    async def test_create_submission_integrity_error(self):
        """Test submission creation with database integrity error."""
        # Arrange
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock(side_effect=IntegrityError("", "", ""))
        self.mock_session.rollback = AsyncMock()
        
        # Act & Assert
        with pytest.raises(IntegrityError):
            await self.repository.create_submission(
                challenge_id=self.sample_challenge_id,
                participant_id="player123",
                participant_type="player",
                submitted_result="answer",
                is_correct=True
            )
        
        self.mock_session.rollback.assert_called_once()
    
    async def test_get_submission_by_id_success(self):
        """Test successful submission retrieval by ID."""
        # Arrange
        submission_id = uuid4()
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = self.sample_submission
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_submission_by_id(submission_id)
        
        # Assert
        assert result == self.sample_submission
        self.mock_session.execute.assert_called_once()
    
    async def test_get_submission_by_id_not_found(self):
        """Test submission retrieval when not found."""
        # Arrange
        submission_id = uuid4()
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_submission_by_id(submission_id)
        
        # Assert
        assert result is None
    
    async def test_get_submissions_by_challenge(self):
        """Test retrieving submissions by challenge."""
        # Arrange
        challenge_id = self.sample_challenge_id
        submissions = [self.sample_submission]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = submissions
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_submissions_by_challenge(challenge_id)
        
        # Assert
        assert result == submissions
        self.mock_session.execute.assert_called_once()
    
    async def test_get_submissions_by_challenge_correct_only(self):
        """Test retrieving only correct submissions by challenge."""
        # Arrange
        challenge_id = self.sample_challenge_id
        submissions = [self.sample_submission]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = submissions
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_submissions_by_challenge(
            challenge_id=challenge_id,
            correct_only=True,
            limit=10,
            offset=5
        )
        
        # Assert
        assert result == submissions
        self.mock_session.execute.assert_called_once()
    
    async def test_get_submissions_by_participant(self):
        """Test retrieving submissions by participant."""
        # Arrange
        participant_id = "player123"
        participant_type = "player"
        submissions = [self.sample_submission]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = submissions
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_submissions_by_participant(
            participant_id=participant_id,
            participant_type=participant_type
        )
        
        # Assert
        assert result == submissions
        self.mock_session.execute.assert_called_once()
    
    async def test_get_submissions_by_participant_with_challenge_filter(self):
        """Test retrieving submissions by participant with challenge filter."""
        # Arrange
        participant_id = "player123"
        participant_type = "player"
        challenge_id = self.sample_challenge_id
        submissions = [self.sample_submission]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = submissions
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_submissions_by_participant(
            participant_id=participant_id,
            participant_type=participant_type,
            challenge_id=challenge_id,
            limit=10,
            offset=5
        )
        
        # Assert
        assert result == submissions
        self.mock_session.execute.assert_called_once()
    
    async def test_get_successful_submission(self):
        """Test retrieving successful submission."""
        # Arrange
        challenge_id = self.sample_challenge_id
        participant_id = "player123"
        participant_type = "player"
        
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = self.sample_submission
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_successful_submission(
            challenge_id, participant_id, participant_type
        )
        
        # Assert
        assert result == self.sample_submission
        self.mock_session.execute.assert_called_once()
    
    async def test_create_or_get_input_cache_new_cache(self):
        """Test creating new input cache."""
        # Arrange
        challenge_id = self.sample_challenge_id
        participant_id = "player123"
        participant_type = "player"
        input_json = {"test": "data"}
        expected_result = "expected"
        
        # Mock no existing cache
        self.repository.get_input_cache = AsyncMock(return_value=None)
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.create_or_get_input_cache(
            challenge_id, participant_id, participant_type, input_json, expected_result
        )
        
        # Assert
        assert result is not None
        assert isinstance(result, GeneratedInputCache)
        assert result.challenge_id == challenge_id
        assert result.participant_id == participant_id
        assert result.participant_type == participant_type
        assert result.input_json == input_json
        assert result.expected_result == expected_result
        
        self.mock_session.add.assert_called_once()
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_create_or_get_input_cache_existing_valid_cache(self):
        """Test retrieving existing valid cache."""
        # Arrange
        challenge_id = self.sample_challenge_id
        participant_id = "player123"
        participant_type = "player"
        input_json = {"test": "data"}
        expected_result = "expected"
        
        # Mock existing valid cache
        existing_cache = GeneratedInputCache(
            challenge_id=challenge_id,
            participant_id=participant_id,
            participant_type=participant_type,
            input_json=input_json,
            expected_result=expected_result,
            is_valid=True
        )
        
        self.repository.get_input_cache = AsyncMock(return_value=existing_cache)
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.create_or_get_input_cache(
            challenge_id, participant_id, participant_type, input_json, expected_result
        )
        
        # Assert
        assert result == existing_cache
        # Should mark first request and commit
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_create_or_get_input_cache_existing_invalid_cache(self):
        """Test handling existing invalid cache."""
        # Arrange
        challenge_id = self.sample_challenge_id
        participant_id = "player123"
        participant_type = "player"
        input_json = {"test": "data"}
        expected_result = "expected"
        
        # Mock existing invalid cache
        existing_cache = GeneratedInputCache(
            challenge_id=challenge_id,
            participant_id=participant_id,
            participant_type=participant_type,
            input_json={"old": "data"},
            expected_result="old_expected",
            is_valid=False
        )
        
        self.repository.get_input_cache = AsyncMock(return_value=existing_cache)
        self.mock_session.delete = AsyncMock()
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.create_or_get_input_cache(
            challenge_id, participant_id, participant_type, input_json, expected_result
        )
        
        # Assert
        assert result is not None
        assert result.input_json == input_json  # Should be new data
        self.mock_session.delete.assert_called_once_with(existing_cache)
        self.mock_session.add.assert_called_once()
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_get_input_cache(self):
        """Test retrieving input cache."""
        # Arrange
        challenge_id = self.sample_challenge_id
        participant_id = "player123"
        participant_type = "player"
        
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = self.sample_cache
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_input_cache(
            challenge_id, participant_id, participant_type
        )
        
        # Assert
        assert result == self.sample_cache
        self.mock_session.execute.assert_called_once()
    
    async def test_invalidate_input_cache_all_participants(self):
        """Test invalidating all cache entries for a challenge."""
        # Arrange
        challenge_id = self.sample_challenge_id
        cache_entries = [self.sample_cache]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = cache_entries
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        self.mock_session.commit = AsyncMock()
        
        # Act
        result = await self.repository.invalidate_input_cache(challenge_id)
        
        # Assert
        assert result == 1  # One cache entry invalidated
        self.mock_session.commit.assert_called_once()
    
    async def test_invalidate_input_cache_specific_participant(self):
        """Test invalidating cache for specific participant."""
        # Arrange
        challenge_id = self.sample_challenge_id
        participant_id = "player123"
        participant_type = "player"
        cache_entries = [self.sample_cache]
        
        mock_result = Mock()
        mock_result.scalars.return_value.all.return_value = cache_entries
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        self.mock_session.commit = AsyncMock()
        
        # Act
        result = await self.repository.invalidate_input_cache(
            challenge_id, participant_id, participant_type
        )
        
        # Assert
        assert result == 1  # One cache entry invalidated
        self.mock_session.commit.assert_called_once()
    
    async def test_record_rate_limit(self):
        """Test recording rate limit entry."""
        # Arrange
        participant_id = "player123"
        participant_type = "player"
        
        self.mock_session.add = Mock()
        self.mock_session.commit = AsyncMock()
        self.mock_session.refresh = AsyncMock()
        
        # Act
        result = await self.repository.record_rate_limit(participant_id, participant_type)
        
        # Assert
        assert result is not None
        assert isinstance(result, SubmissionRateLimit)
        assert result.participant_id == participant_id
        assert result.participant_type == participant_type
        
        self.mock_session.add.assert_called_once()
        self.mock_session.commit.assert_called_once()
        self.mock_session.refresh.assert_called_once()
    
    async def test_record_rate_limit_validation_errors(self):
        """Test rate limit recording validation errors."""
        # Empty participant_id
        with pytest.raises(ValueError, match="Participant ID cannot be empty"):
            await self.repository.record_rate_limit("", "player")
        
        # Invalid participant_type
        with pytest.raises(ValueError, match="Participant type must be"):
            await self.repository.record_rate_limit("player123", "invalid")
    
    async def test_check_rate_limit(self):
        """Test checking rate limit."""
        # Arrange
        participant_id = "player123"
        participant_type = "player"
        
        # Mock SubmissionRateLimit.is_rate_limited
        from smarter_dev.web.models import SubmissionRateLimit
        original_is_rate_limited = SubmissionRateLimit.is_rate_limited
        SubmissionRateLimit.is_rate_limited = AsyncMock(return_value=False)
        
        try:
            # Act
            result = await self.repository.check_rate_limit(participant_id, participant_type)
            
            # Assert
            assert result is False
            SubmissionRateLimit.is_rate_limited.assert_called_once()
        finally:
            # Restore original method
            SubmissionRateLimit.is_rate_limited = original_is_rate_limited
    
    async def test_cleanup_old_rate_limits(self):
        """Test cleaning up old rate limit entries."""
        # Arrange
        expected_count = 5
        
        # Mock count query
        count_result = Mock()
        count_result.scalar.return_value = expected_count
        
        # Mock execute calls (count query, then delete)
        self.mock_session.execute = AsyncMock(side_effect=[count_result, Mock()])
        self.mock_session.commit = AsyncMock()
        
        # Act
        result = await self.repository.cleanup_old_rate_limits(days_to_keep=3)
        
        # Assert
        assert result == expected_count
        assert self.mock_session.execute.call_count == 2  # Count + Delete
        self.mock_session.commit.assert_called_once()
    
    async def test_get_leaderboard_data(self):
        """Test retrieving leaderboard data."""
        # Arrange
        challenge_id = self.sample_challenge_id
        
        # Mock query result
        mock_rows = [
            Mock(
                participant_id="player1",
                participant_type="player",
                total_points=150,
                completed_challenges=3,
                first_completion=datetime.now(timezone.utc)
            ),
            Mock(
                participant_id="player2",
                participant_type="player", 
                total_points=100,
                completed_challenges=2,
                first_completion=datetime.now(timezone.utc)
            )
        ]
        
        mock_result = Mock()
        mock_result.fetchall.return_value = mock_rows
        self.mock_session.execute = AsyncMock(return_value=mock_result)
        
        # Act
        result = await self.repository.get_leaderboard_data(challenge_id=challenge_id)
        
        # Assert
        assert len(result) == 2
        assert result[0]['participant_id'] == "player1"
        assert result[0]['total_points'] == 150
        assert result[0]['completed_challenges'] == 3
        assert result[1]['participant_id'] == "player2"
        assert result[1]['total_points'] == 100
        self.mock_session.execute.assert_called_once()
    
    async def test_get_submission_statistics(self):
        """Test retrieving submission statistics."""
        # Arrange
        challenge_id = self.sample_challenge_id
        
        # Mock multiple query results
        total_result = Mock()
        total_result.scalar.return_value = 100
        
        correct_result = Mock()
        correct_result.scalar.return_value = 75
        
        unique_participants_result = Mock()
        unique_participants_result.scalar.return_value = 25
        
        self.mock_session.execute = AsyncMock(
            side_effect=[total_result, correct_result, unique_participants_result]
        )
        
        # Act
        result = await self.repository.get_submission_statistics(challenge_id=challenge_id)
        
        # Assert
        assert result["total_submissions"] == 100
        assert result["correct_submissions"] == 75
        assert result["success_rate"] == 75.0  # 75/100 * 100
        assert result["unique_participants"] == 25
        assert "date_range" in result
        
        # Should have called execute 3 times (total, correct, unique participants)
        assert self.mock_session.execute.call_count == 3