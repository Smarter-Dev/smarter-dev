"""
Submission Validation Service - Following SOLID principles.

This service handles result validation, point awarding, and whitespace trimming
with exact matching logic for challenge submissions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Any, List, Optional, Protocol
from uuid import UUID
import logging

logger = logging.getLogger(__name__)


class ValidationStatus(Enum):
    """Status of submission validation."""
    CORRECT = "correct"
    INCORRECT = "incorrect"
    ERROR = "error"


@dataclass
class ValidationResult:
    """
    Result of submission validation.
    
    Contains validation status, correctness, points, and detailed comparison data.
    """
    status: ValidationStatus
    is_correct: bool
    points_awarded: int = 0
    expected_result: Optional[str] = None
    submitted_result: Optional[str] = None
    normalized_submitted: Optional[str] = None
    normalized_expected: Optional[str] = None
    error_message: Optional[str] = None
    scoring_position: Optional[int] = None
    total_participants: Optional[int] = None
    scoring_details: Optional[Dict[str, Any]] = None


class ValidationError(Exception):
    """Exception raised when submission validation fails."""
    pass


class ChallengeProtocol(Protocol):
    """Protocol defining the interface for challenge objects."""
    id: UUID
    title: str


class InputCacheProtocol(Protocol):
    """Protocol defining the interface for input cache objects."""
    input_json: Dict[str, Any]
    expected_result: str
    is_valid: bool


class SubmissionProtocol(Protocol):
    """Protocol defining the interface for submission objects."""
    participant_id: str
    participant_type: str
    submission_timestamp: datetime
    is_correct: bool


class SubmissionRepositoryProtocol(Protocol):
    """Protocol defining the interface for submission repository."""
    
    async def get_input_cache(
        self, 
        challenge_id: UUID, 
        participant_id: str, 
        participant_type: str
    ) -> Optional[InputCacheProtocol]:
        """Get cached input for a participant."""
        pass
    
    async def get_submissions_by_challenge(
        self,
        challenge_id: UUID,
        limit: int = 50,
        offset: int = 0,
        correct_only: bool = False
    ) -> List[SubmissionProtocol]:
        """Get submissions for a challenge."""
        pass
    
    async def get_successful_submission(
        self,
        challenge_id: UUID,
        participant_id: str,
        participant_type: str
    ) -> Optional[SubmissionProtocol]:
        """Get successful submission for a participant."""
        pass
    
    async def get_submission_statistics(
        self,
        challenge_id: Optional[UUID] = None,
        campaign_id: Optional[UUID] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Get submission statistics."""
        pass


class ScoringStrategyProtocol(Protocol):
    """Protocol defining the interface for scoring strategies."""
    
    def calculate_points(
        self,
        submission: SubmissionProtocol,
        all_submissions: List[SubmissionProtocol],
        challenge_start_time: Optional[datetime] = None
    ) -> Any:  # ScoringResult
        """Calculate points for a submission."""
        pass


class SubmissionValidationService:
    """
    Service for validating challenge submissions and awarding points.
    
    Following SRP: Only handles submission validation and scoring logic.
    Following DIP: Depends on abstractions (repository and strategy protocols).
    Following OCP: Extensible for different validation strategies.
    """
    
    def __init__(
        self,
        submission_repository: SubmissionRepositoryProtocol
    ):
        """
        Initialize service with repository dependency.
        
        Args:
            submission_repository: Repository for submission data access
        """
        self.submission_repository = submission_repository
    
    async def validate_submission(
        self,
        challenge: ChallengeProtocol,
        participant_id: str,
        participant_type: str,
        submitted_result: str
    ) -> ValidationResult:
        """
        Validate a submitted result against the expected answer.
        
        Performs whitespace trimming and case-insensitive comparison.
        
        Args:
            challenge: Challenge being submitted to
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            submitted_result: Participant's submitted answer
            
        Returns:
            ValidationResult with validation outcome and details
            
        Raises:
            ValueError: If input validation fails
        """
        # Input validation
        if challenge is None:
            raise ValueError("Challenge cannot be None")
        
        if not participant_id or not participant_id.strip():
            raise ValueError("Participant ID cannot be empty")
        
        if participant_type not in ["player", "squad"]:
            raise ValueError("Participant type must be 'player' or 'squad'")
        
        if not submitted_result or not submitted_result.strip():
            raise ValueError("Submitted result cannot be empty")
        
        try:
            # Get expected result from input cache
            input_cache = await self.submission_repository.get_input_cache(
                challenge_id=challenge.id,
                participant_id=participant_id,
                participant_type=participant_type
            )
            
            if not input_cache:
                logger.warning(
                    f"No input cache found for challenge {challenge.id}, "
                    f"participant {participant_id} ({participant_type})"
                )
                
                return ValidationResult(
                    status=ValidationStatus.ERROR,
                    is_correct=False,
                    points_awarded=0,
                    error_message="No input generated for this challenge. Please request input first."
                )
            
            expected_result = input_cache.expected_result
            
            # Normalize both results for comparison
            normalized_submitted = self._normalize_result(submitted_result)
            normalized_expected = self._normalize_result(expected_result)
            
            # Compare normalized results
            is_correct = normalized_submitted == normalized_expected
            
            logger.info(
                f"Validation result for challenge {challenge.id}, "
                f"participant {participant_id}: {'CORRECT' if is_correct else 'INCORRECT'}"
            )
            
            return ValidationResult(
                status=ValidationStatus.CORRECT if is_correct else ValidationStatus.INCORRECT,
                is_correct=is_correct,
                points_awarded=0,  # Points calculated separately by scoring service
                expected_result=expected_result,
                submitted_result=submitted_result,
                normalized_submitted=normalized_submitted,
                normalized_expected=normalized_expected
            )
            
        except Exception as e:
            logger.exception(
                f"Unexpected error during validation for challenge {challenge.id}"
            )
            
            return ValidationResult(
                status=ValidationStatus.ERROR,
                is_correct=False,
                points_awarded=0,
                error_message=f"Validation error: {str(e)}"
            )
    
    async def validate_and_score_submission(
        self,
        challenge: ChallengeProtocol,
        participant_id: str,
        participant_type: str,
        submitted_result: str,
        scoring_strategy: ScoringStrategyProtocol,
        challenge_start_time: Optional[datetime] = None
    ) -> ValidationResult:
        """
        Validate submission and calculate points using scoring strategy.
        
        Args:
            challenge: Challenge being submitted to
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            submitted_result: Participant's submitted answer
            scoring_strategy: Strategy for calculating points
            challenge_start_time: When the challenge started (for time-based scoring)
            
        Returns:
            ValidationResult with validation outcome and point calculation
        """
        # First validate the submission
        validation_result = await self.validate_submission(
            challenge=challenge,
            participant_id=participant_id,
            participant_type=participant_type,
            submitted_result=submitted_result
        )
        
        # If validation failed or was incorrect, return with 0 points
        if validation_result.status != ValidationStatus.CORRECT:
            return validation_result
        
        try:
            # Get all submissions for scoring context
            all_submissions = await self.submission_repository.get_submissions_by_challenge(
                challenge_id=challenge.id,
                limit=1000  # Get enough for comprehensive scoring
            )
            
            # Create a mock submission object for scoring
            from datetime import datetime, timezone
            mock_submission = type('MockSubmission', (), {
                'participant_id': participant_id,
                'participant_type': participant_type,
                'submission_timestamp': datetime.now(timezone.utc),
                'is_correct': True
            })()
            
            # Calculate points using scoring strategy
            scoring_result = scoring_strategy.calculate_points(
                submission=mock_submission,
                all_submissions=all_submissions,
                challenge_start_time=challenge_start_time
            )
            
            # Update validation result with scoring information
            validation_result.points_awarded = scoring_result.points_awarded
            validation_result.scoring_position = scoring_result.position
            validation_result.total_participants = scoring_result.total_participants
            validation_result.scoring_details = scoring_result.calculation_details
            
            logger.info(
                f"Scored submission for challenge {challenge.id}, "
                f"participant {participant_id}: {scoring_result.points_awarded} points, "
                f"position {scoring_result.position}/{scoring_result.total_participants}"
            )
            
            return validation_result
            
        except Exception as e:
            logger.exception(
                f"Unexpected error during scoring for challenge {challenge.id}"
            )
            
            # Return validation result with scoring error
            validation_result.error_message = f"Scoring error: {str(e)}"
            return validation_result
    
    def _normalize_result(self, result: str) -> str:
        """
        Normalize a result string for comparison.
        
        Removes leading/trailing whitespace and converts to lowercase.
        
        Args:
            result: Raw result string
            
        Returns:
            Normalized result string
        """
        if not result:
            return ""
        
        # Strip leading and trailing whitespace
        normalized = result.strip()
        
        # Convert to lowercase for case-insensitive comparison
        normalized = normalized.lower()
        
        return normalized
    
    async def batch_validate_submissions(
        self,
        challenge: ChallengeProtocol,
        submissions_data: List[Dict[str, str]]
    ) -> List[ValidationResult]:
        """
        Validate multiple submissions in batch for efficiency.
        
        Args:
            challenge: Challenge being submitted to
            submissions_data: List of dicts with participant_id, participant_type, submitted_result
            
        Returns:
            List of ValidationResult objects in same order as input
        """
        results = []
        
        for submission_data in submissions_data:
            try:
                result = await self.validate_submission(
                    challenge=challenge,
                    participant_id=submission_data["participant_id"],
                    participant_type=submission_data["participant_type"],
                    submitted_result=submission_data["submitted_result"]
                )
                results.append(result)
                
            except Exception as e:
                logger.exception(
                    f"Error in batch validation for participant {submission_data.get('participant_id')}"
                )
                
                error_result = ValidationResult(
                    status=ValidationStatus.ERROR,
                    is_correct=False,
                    points_awarded=0,
                    error_message=f"Batch validation error: {str(e)}"
                )
                results.append(error_result)
        
        logger.info(
            f"Batch validated {len(results)} submissions for challenge {challenge.id}"
        )
        
        return results
    
    async def check_duplicate_submission(
        self,
        challenge_id: UUID,
        participant_id: str,
        participant_type: str
    ) -> bool:
        """
        Check if participant already has a successful submission for this challenge.
        
        Args:
            challenge_id: Challenge UUID
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            
        Returns:
            True if participant already has a successful submission
        """
        existing_submission = await self.submission_repository.get_successful_submission(
            challenge_id=challenge_id,
            participant_id=participant_id,
            participant_type=participant_type
        )
        
        return existing_submission is not None
    
    async def get_validation_statistics(
        self,
        challenge_id: UUID,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Get validation statistics for a challenge.
        
        Args:
            challenge_id: Challenge UUID
            date_from: Optional start date filter
            date_to: Optional end date filter
            
        Returns:
            Dictionary with validation statistics
        """
        stats = await self.submission_repository.get_submission_statistics(
            challenge_id=challenge_id,
            date_from=date_from,
            date_to=date_to
        )
        
        logger.info(f"Retrieved validation statistics for challenge {challenge_id}")
        
        return stats
    
    async def analyze_common_incorrect_answers(
        self,
        challenge_id: UUID,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Analyze common incorrect answers for a challenge.
        
        Useful for understanding where participants struggle.
        
        Args:
            challenge_id: Challenge UUID
            limit: Maximum number of common answers to return
            
        Returns:
            List of common incorrect answers with frequency counts
        """
        # Get all incorrect submissions
        all_submissions = await self.submission_repository.get_submissions_by_challenge(
            challenge_id=challenge_id,
            limit=1000,
            correct_only=False
        )
        
        # Filter to incorrect submissions and count answers
        incorrect_answers = {}
        for submission in all_submissions:
            if not submission.is_correct:
                # Would need submitted_result field in submission model
                # This is a placeholder for the analysis logic
                normalized_answer = self._normalize_result(getattr(submission, 'submitted_result', ''))
                if normalized_answer:
                    incorrect_answers[normalized_answer] = incorrect_answers.get(normalized_answer, 0) + 1
        
        # Sort by frequency and return top answers
        sorted_answers = sorted(
            incorrect_answers.items(),
            key=lambda x: x[1],
            reverse=True
        )[:limit]
        
        result = [
            {
                "answer": answer,
                "frequency": count,
                "percentage": (count / len([s for s in all_submissions if not s.is_correct]) * 100)
                    if len([s for s in all_submissions if not s.is_correct]) > 0 else 0
            }
            for answer, count in sorted_answers
        ]
        
        logger.info(
            f"Analyzed {len(incorrect_answers)} unique incorrect answers "
            f"for challenge {challenge_id}"
        )
        
        return result
    
    async def validate_answer_format(
        self,
        submitted_result: str,
        expected_format: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Validate the format of a submitted answer.
        
        Can check for specific formats like numbers, JSON, etc.
        
        Args:
            submitted_result: The submitted answer
            expected_format: Optional format specification
            
        Returns:
            Dictionary with format validation results
        """
        validation_result = {
            "valid": True,
            "format_errors": [],
            "suggestions": []
        }
        
        # Basic checks
        if not submitted_result or not submitted_result.strip():
            validation_result["valid"] = False
            validation_result["format_errors"].append("Answer cannot be empty")
            return validation_result
        
        # Format-specific validation
        if expected_format:
            if expected_format == "number":
                try:
                    float(submitted_result.strip())
                except ValueError:
                    validation_result["valid"] = False
                    validation_result["format_errors"].append("Answer must be a valid number")
                    validation_result["suggestions"].append("Enter only numeric characters (0-9) and decimal points")
            
            elif expected_format == "integer":
                try:
                    int(submitted_result.strip())
                except ValueError:
                    validation_result["valid"] = False
                    validation_result["format_errors"].append("Answer must be a valid integer")
                    validation_result["suggestions"].append("Enter only whole numbers without decimal points")
            
            elif expected_format == "json":
                try:
                    import json
                    json.loads(submitted_result.strip())
                except json.JSONDecodeError:
                    validation_result["valid"] = False
                    validation_result["format_errors"].append("Answer must be valid JSON")
                    validation_result["suggestions"].append("Check for proper JSON syntax with quotes around strings")
        
        return validation_result