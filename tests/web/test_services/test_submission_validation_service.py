"""Test cases for Submission Validation Service."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock, Mock, patch

from web.services.submission_validation_service import (
    SubmissionValidationService,
    ValidationResult,
    ValidationStatus,
    ValidationError
)


class TestValidationResult:
    """Test cases for ValidationResult data structure."""
    
    def test_validation_result_correct(self):
        """Test ValidationResult creation for correct submission."""
        result = ValidationResult(
            status=ValidationStatus.CORRECT,
            is_correct=True,
            points_awarded=100,
            expected_result="42",
            submitted_result="42",
            normalized_submitted="42",
            normalized_expected="42"
        )
        
        assert result.status == ValidationStatus.CORRECT
        assert result.is_correct is True
        assert result.points_awarded == 100
        assert result.expected_result == "42"
        assert result.submitted_result == "42"
        assert result.normalized_submitted == "42"
        assert result.normalized_expected == "42"
        assert result.error_message is None
    
    def test_validation_result_incorrect(self):
        """Test ValidationResult creation for incorrect submission."""
        result = ValidationResult(
            status=ValidationStatus.INCORRECT,
            is_correct=False,
            points_awarded=0,
            expected_result="42",
            submitted_result="24",
            normalized_submitted="24",
            normalized_expected="42"
        )
        
        assert result.status == ValidationStatus.INCORRECT
        assert result.is_correct is False
        assert result.points_awarded == 0
        assert result.submitted_result == "24"
        assert result.normalized_submitted == "24"
    
    def test_validation_result_error(self):
        """Test ValidationResult creation for error case."""
        result = ValidationResult(
            status=ValidationStatus.ERROR,
            is_correct=False,
            points_awarded=0,
            error_message="Invalid input format"
        )
        
        assert result.status == ValidationStatus.ERROR
        assert result.is_correct is False
        assert result.points_awarded == 0
        assert result.error_message == "Invalid input format"


class TestSubmissionValidationService:
    """Test cases for Submission Validation Service functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_submission_repo = AsyncMock()
        self.mock_scoring_strategy = Mock()
        self.service = SubmissionValidationService(
            submission_repository=self.mock_submission_repo
        )
        
        # Sample challenge
        self.sample_challenge = Mock(
            id=uuid4(),
            title="Test Challenge"
        )
        
        # Sample participant
        self.participant_id = "player123"
        self.participant_type = "player"
        
        # Sample input cache
        self.sample_input_cache = Mock(
            input_json={"n": 100},
            expected_result="42"
        )
    
    async def test_validate_submission_correct_exact_match(self):
        """Test validation with correct exact match."""
        submitted_result = "42"
        expected_result = "42"
        
        # Mock input cache
        self.mock_submission_repo.get_input_cache.return_value = self.sample_input_cache
        self.sample_input_cache.expected_result = expected_result
        
        # Act
        result = await self.service.validate_submission(
            challenge=self.sample_challenge,
            participant_id=self.participant_id,
            participant_type=self.participant_type,
            submitted_result=submitted_result
        )
        
        # Assert
        assert result.status == ValidationStatus.CORRECT
        assert result.is_correct is True
        assert result.submitted_result == "42"
        assert result.expected_result == "42"
        assert result.normalized_submitted == "42"
        assert result.normalized_expected == "42"
    
    async def test_validate_submission_correct_with_whitespace(self):
        """Test validation with correct result but whitespace differences."""
        submitted_result = "  42  \n"
        expected_result = "42"
        
        # Mock input cache
        self.mock_submission_repo.get_input_cache.return_value = self.sample_input_cache
        self.sample_input_cache.expected_result = expected_result
        
        # Act
        result = await self.service.validate_submission(
            challenge=self.sample_challenge,
            participant_id=self.participant_id,
            participant_type=self.participant_type,
            submitted_result=submitted_result
        )
        
        # Assert
        assert result.status == ValidationStatus.CORRECT
        assert result.is_correct is True
        assert result.submitted_result == "  42  \n"  # Original preserved
        assert result.expected_result == "42"
        assert result.normalized_submitted == "42"  # Normalized
        assert result.normalized_expected == "42"
    
    async def test_validate_submission_correct_case_insensitive(self):
        """Test validation with correct result but case differences."""
        submitted_result = "Hello World"
        expected_result = "hello world"
        
        # Mock input cache
        self.mock_submission_repo.get_input_cache.return_value = self.sample_input_cache
        self.sample_input_cache.expected_result = expected_result
        
        # Act
        result = await self.service.validate_submission(
            challenge=self.sample_challenge,
            participant_id=self.participant_id,
            participant_type=self.participant_type,
            submitted_result=submitted_result
        )
        
        # Assert
        assert result.status == ValidationStatus.CORRECT
        assert result.is_correct is True
        assert result.submitted_result == "Hello World"  # Original preserved
        assert result.expected_result == "hello world"
        assert result.normalized_submitted == "hello world"  # Normalized
        assert result.normalized_expected == "hello world"
    
    async def test_validate_submission_incorrect(self):
        """Test validation with incorrect result."""
        submitted_result = "24"
        expected_result = "42"
        
        # Mock input cache
        self.mock_submission_repo.get_input_cache.return_value = self.sample_input_cache
        self.sample_input_cache.expected_result = expected_result
        
        # Act
        result = await self.service.validate_submission(
            challenge=self.sample_challenge,
            participant_id=self.participant_id,
            participant_type=self.participant_type,
            submitted_result=submitted_result
        )
        
        # Assert
        assert result.status == ValidationStatus.INCORRECT
        assert result.is_correct is False
        assert result.submitted_result == "24"
        assert result.expected_result == "42"
        assert result.normalized_submitted == "24"
        assert result.normalized_expected == "42"
    
    async def test_validate_submission_no_input_cache(self):
        """Test validation when no input cache exists."""
        # Mock no input cache
        self.mock_submission_repo.get_input_cache.return_value = None
        
        # Act
        result = await self.service.validate_submission(
            challenge=self.sample_challenge,
            participant_id=self.participant_id,
            participant_type=self.participant_type,
            submitted_result="42"
        )
        
        # Assert
        assert result.status == ValidationStatus.ERROR
        assert result.is_correct is False
        assert result.points_awarded == 0
        assert "No input generated" in result.error_message
    
    async def test_validate_submission_input_validation_errors(self):
        """Test validation with invalid inputs."""
        # Test None challenge
        with pytest.raises(ValueError, match="Challenge cannot be None"):
            await self.service.validate_submission(
                challenge=None,
                participant_id=self.participant_id,
                participant_type=self.participant_type,
                submitted_result="42"
            )
        
        # Test empty participant_id
        with pytest.raises(ValueError, match="Participant ID cannot be empty"):
            await self.service.validate_submission(
                challenge=self.sample_challenge,
                participant_id="",
                participant_type=self.participant_type,
                submitted_result="42"
            )
        
        # Test invalid participant_type
        with pytest.raises(ValueError, match="Participant type must be"):
            await self.service.validate_submission(
                challenge=self.sample_challenge,
                participant_id=self.participant_id,
                participant_type="invalid",
                submitted_result="42"
            )
        
        # Test empty submitted_result
        with pytest.raises(ValueError, match="Submitted result cannot be empty"):
            await self.service.validate_submission(
                challenge=self.sample_challenge,
                participant_id=self.participant_id,
                participant_type=self.participant_type,
                submitted_result=""
            )
    
    async def test_validate_and_score_submission_correct(self):
        """Test validation and scoring with correct submission."""
        submitted_result = "42"
        expected_result = "42"
        
        # Mock input cache
        self.mock_submission_repo.get_input_cache.return_value = self.sample_input_cache
        self.sample_input_cache.expected_result = expected_result
        
        # Mock all submissions for scoring
        mock_submissions = [
            Mock(participant_id="player1", submission_timestamp=datetime.now(timezone.utc), is_correct=True),
            Mock(participant_id="player2", submission_timestamp=datetime.now(timezone.utc), is_correct=True)
        ]
        self.mock_submission_repo.get_submissions_by_challenge.return_value = mock_submissions
        
        # Mock scoring result
        from web.services.scoring_strategies import ScoringResult, TimeBasedScoringStrategy
        mock_scoring_result = ScoringResult(
            points_awarded=100,
            position=1,
            total_participants=2
        )
        
        # Act
        with patch.object(TimeBasedScoringStrategy, 'calculate_points', return_value=mock_scoring_result):
            result = await self.service.validate_and_score_submission(
                challenge=self.sample_challenge,
                participant_id=self.participant_id,
                participant_type=self.participant_type,
                submitted_result=submitted_result,
                scoring_strategy=TimeBasedScoringStrategy()
            )
        
        # Assert
        assert result.status == ValidationStatus.CORRECT
        assert result.is_correct is True
        assert result.points_awarded == 100
        assert result.scoring_position == 1
        assert result.total_participants == 2
    
    async def test_validate_and_score_submission_incorrect(self):
        """Test validation and scoring with incorrect submission."""
        submitted_result = "24"
        expected_result = "42"
        
        # Mock input cache
        self.mock_submission_repo.get_input_cache.return_value = self.sample_input_cache
        self.sample_input_cache.expected_result = expected_result
        
        # Mock all submissions for scoring
        mock_submissions = [Mock()]
        self.mock_submission_repo.get_submissions_by_challenge.return_value = mock_submissions
        
        # Mock scoring strategy
        from web.services.scoring_strategies import TimeBasedScoringStrategy
        
        # Act
        result = await self.service.validate_and_score_submission(
            challenge=self.sample_challenge,
            participant_id=self.participant_id,
            participant_type=self.participant_type,
            submitted_result=submitted_result,
            scoring_strategy=TimeBasedScoringStrategy()
        )
        
        # Assert
        assert result.status == ValidationStatus.INCORRECT
        assert result.is_correct is False
        assert result.points_awarded == 0
        assert result.scoring_position is None  # No position for incorrect
    
    async def test_normalize_result_whitespace(self):
        """Test result normalization with whitespace."""
        # Test various whitespace scenarios
        assert self.service._normalize_result("  hello  ") == "hello"
        assert self.service._normalize_result("hello\n") == "hello"
        assert self.service._normalize_result("\t hello \r\n") == "hello"
        assert self.service._normalize_result("  multiple   spaces  ") == "multiple   spaces"
    
    async def test_normalize_result_case(self):
        """Test result normalization with case changes."""
        assert self.service._normalize_result("Hello World") == "hello world"
        assert self.service._normalize_result("UPPERCASE") == "uppercase"
        assert self.service._normalize_result("MiXeD cAsE") == "mixed case"
    
    async def test_normalize_result_combined(self):
        """Test result normalization with combined whitespace and case."""
        assert self.service._normalize_result("  Hello World  \n") == "hello world"
        assert self.service._normalize_result("\t ANSWER \r") == "answer"
        assert self.service._normalize_result("  42.5  ") == "42.5"
    
    async def test_normalize_result_empty_and_none(self):
        """Test result normalization with empty and None values."""
        assert self.service._normalize_result("") == ""
        assert self.service._normalize_result("   ") == ""
        assert self.service._normalize_result("\n\t\r") == ""
    
    async def test_batch_validate_submissions(self):
        """Test batch validation of multiple submissions."""
        submissions_data = [
            {"participant_id": "player1", "participant_type": "player", "submitted_result": "42"},
            {"participant_id": "player2", "participant_type": "player", "submitted_result": "24"},
            {"participant_id": "squad1", "participant_type": "squad", "submitted_result": "42"}
        ]
        
        # Mock input cache
        self.mock_submission_repo.get_input_cache.return_value = self.sample_input_cache
        self.sample_input_cache.expected_result = "42"
        
        # Act
        results = await self.service.batch_validate_submissions(
            challenge=self.sample_challenge,
            submissions_data=submissions_data
        )
        
        # Assert
        assert len(results) == 3
        assert results[0].is_correct is True  # player1: "42" == "42"
        assert results[1].is_correct is False  # player2: "24" != "42"
        assert results[2].is_correct is True  # squad1: "42" == "42"
    
    async def test_get_validation_statistics(self):
        """Test getting validation statistics."""
        challenge_id = self.sample_challenge.id
        
        # Mock submission repository statistics
        self.mock_submission_repo.get_submission_statistics.return_value = {
            "total_submissions": 100,
            "correct_submissions": 75,
            "success_rate": 75.0,
            "unique_participants": 30
        }
        
        # Act
        stats = await self.service.get_validation_statistics(challenge_id)
        
        # Assert
        assert stats["total_submissions"] == 100
        assert stats["correct_submissions"] == 75
        assert stats["success_rate"] == 75.0
        assert stats["unique_participants"] == 30
        
        # Verify repository was called
        self.mock_submission_repo.get_submission_statistics.assert_called_once_with(
            challenge_id=challenge_id, 
            date_from=None, 
            date_to=None
        )
    
    async def test_check_duplicate_submission(self):
        """Test checking for duplicate submissions."""
        # Mock existing successful submission
        mock_existing_submission = Mock(
            participant_id=self.participant_id,
            is_correct=True,
            submission_timestamp=datetime.now(timezone.utc)
        )
        self.mock_submission_repo.get_successful_submission.return_value = mock_existing_submission
        
        # Act
        has_duplicate = await self.service.check_duplicate_submission(
            challenge_id=self.sample_challenge.id,
            participant_id=self.participant_id,
            participant_type=self.participant_type
        )
        
        # Assert
        assert has_duplicate is True
        self.mock_submission_repo.get_successful_submission.assert_called_once_with(
            challenge_id=self.sample_challenge.id,
            participant_id=self.participant_id,
            participant_type=self.participant_type
        )
    
    async def test_check_duplicate_submission_none_exists(self):
        """Test checking for duplicate when none exists."""
        # Mock no existing submission
        self.mock_submission_repo.get_successful_submission.return_value = None
        
        # Act
        has_duplicate = await self.service.check_duplicate_submission(
            challenge_id=self.sample_challenge.id,
            participant_id=self.participant_id,
            participant_type=self.participant_type
        )
        
        # Assert
        assert has_duplicate is False
    
    async def test_complex_normalization_scenarios(self):
        """Test complex normalization scenarios that might occur in practice."""
        # Numeric results with formatting
        assert self.service._normalize_result("  3.14159  ") == "3.14159"
        assert self.service._normalize_result("1,000,000") == "1,000,000"
        
        # Multi-line results (should become single line)
        assert self.service._normalize_result("line1\nline2") == "line1\nline2"  # Preserves internal newlines
        
        # JSON-like results
        assert self.service._normalize_result('  {"key": "value"}  ') == '{"key": "value"}'
        
        # Array-like results
        assert self.service._normalize_result("  [1, 2, 3]  ") == "[1, 2, 3]"


class TestValidationError:
    """Test cases for ValidationError exception."""
    
    def test_validation_error(self):
        """Test ValidationError exception creation."""
        error = ValidationError("Invalid submission format")
        
        assert str(error) == "Invalid submission format"
        assert isinstance(error, Exception)


class TestValidationStatus:
    """Test cases for ValidationStatus enum."""
    
    def test_validation_status_values(self):
        """Test ValidationStatus enum values."""
        assert ValidationStatus.CORRECT.value == "correct"
        assert ValidationStatus.INCORRECT.value == "incorrect"
        assert ValidationStatus.ERROR.value == "error"
    
    def test_validation_status_comparison(self):
        """Test ValidationStatus comparison."""
        assert ValidationStatus.CORRECT != ValidationStatus.INCORRECT
        assert ValidationStatus.INCORRECT != ValidationStatus.ERROR
        assert ValidationStatus.CORRECT.value == "correct"