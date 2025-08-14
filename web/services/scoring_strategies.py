"""
Scoring Strategy Pattern Implementation - Following SOLID principles.

This module implements the Strategy Pattern for flexible scoring algorithms
in the campaign challenges system. It provides both time-based and point-based
scoring strategies with comprehensive calculation logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Protocol
import logging

logger = logging.getLogger(__name__)


@dataclass
class ScoringResult:
    """
    Result of a scoring calculation.
    
    Contains the points awarded, position, and additional calculation details
    for transparency and debugging purposes.
    """
    points_awarded: int
    position: int
    total_participants: int
    calculation_details: Dict[str, Any] = field(default_factory=dict)


class SubmissionProtocol(Protocol):
    """Protocol defining the interface for submission objects."""
    participant_id: str
    submission_timestamp: datetime
    is_correct: bool


class ScoringStrategy(ABC):
    """
    Abstract base class for scoring strategies.
    
    Following the Strategy Pattern (Open/Closed Principle), this allows
    for different scoring algorithms to be implemented and swapped out
    without modifying existing code.
    """
    
    @abstractmethod
    def calculate_points(
        self,
        submission: SubmissionProtocol,
        all_submissions: List[SubmissionProtocol],
        challenge_start_time: Optional[datetime] = None
    ) -> ScoringResult:
        """
        Calculate points for a submission based on all submissions.
        
        Args:
            submission: The submission to calculate points for
            all_submissions: All submissions for this challenge
            challenge_start_time: When the challenge started (for time calculations)
            
        Returns:
            ScoringResult with points awarded and calculation details
            
        Raises:
            ValueError: If submission or all_submissions is None
        """
        pass
    
    @abstractmethod
    def get_strategy_name(self) -> str:
        """
        Get the name of this scoring strategy.
        
        Returns:
            String identifier for the strategy
        """
        pass
    
    @abstractmethod
    def get_strategy_description(self) -> str:
        """
        Get a human-readable description of this scoring strategy.
        
        Returns:
            Description of how this strategy calculates points
        """
        pass


class TimeBasedScoringStrategy(ScoringStrategy):
    """
    Time-based scoring strategy.
    
    Awards points based on completion order:
    - 1st place: 100 points
    - 2nd place: 75 points  
    - 3rd place: 50 points
    - 4th+ place: 25 points
    - Incorrect submissions: 0 points
    
    Following SRP: Only handles time-based scoring calculations.
    """
    
    def calculate_points(
        self,
        submission: SubmissionProtocol,
        all_submissions: List[SubmissionProtocol],
        challenge_start_time: Optional[datetime] = None
    ) -> ScoringResult:
        """
        Calculate points based on submission timing and correctness.
        
        Args:
            submission: The submission to calculate points for
            all_submissions: All submissions for this challenge
            challenge_start_time: When the challenge started
            
        Returns:
            ScoringResult with time-based points calculation
            
        Raises:
            ValueError: If submission or all_submissions is None
        """
        # Input validation
        if submission is None:
            raise ValueError("Submission cannot be None")
        
        if all_submissions is None:
            raise ValueError("All submissions list cannot be None")
        
        # Initialize calculation details
        calculation_details = {
            "scoring_method": "time_based"
        }
        
        # Add time taken if challenge start time is provided
        if challenge_start_time and submission.submission_timestamp:
            time_taken = submission.submission_timestamp - challenge_start_time
            calculation_details["time_taken_minutes"] = int(time_taken.total_seconds() / 60)
        
        # If submission is incorrect, award 0 points
        if not submission.is_correct:
            calculation_details["reason"] = "incorrect_submission"
            return ScoringResult(
                points_awarded=0,
                position=0,
                total_participants=self._count_correct_submissions(all_submissions),
                calculation_details=calculation_details
            )
        
        # Get only correct submissions for ranking
        correct_submissions = [s for s in all_submissions if s.is_correct]
        
        # Sort by submission timestamp (earliest first)
        sorted_submissions = sorted(
            correct_submissions,
            key=lambda s: s.submission_timestamp
        )
        
        # Find position of this submission
        position = 0
        for i, sub in enumerate(sorted_submissions, 1):
            if sub.participant_id == submission.participant_id:
                position = i
                break
        
        # Calculate points based on position
        points_awarded = self._calculate_time_based_points(position)
        
        calculation_details["position_details"] = {
            "finished_position": position,
            "out_of_total": len(correct_submissions)
        }
        
        return ScoringResult(
            points_awarded=points_awarded,
            position=position,
            total_participants=len(correct_submissions),
            calculation_details=calculation_details
        )
    
    def _calculate_time_based_points(self, position: int) -> int:
        """
        Calculate points based on finishing position.
        
        Args:
            position: Finishing position (1-based)
            
        Returns:
            Points awarded for the position
        """
        if position == 1:
            return 100
        elif position == 2:
            return 75
        elif position == 3:
            return 50
        else:
            return 25  # 4th place and beyond
    
    def _count_correct_submissions(self, all_submissions: List[SubmissionProtocol]) -> int:
        """
        Count the number of correct submissions.
        
        Args:
            all_submissions: All submissions to count
            
        Returns:
            Number of correct submissions
        """
        return len([s for s in all_submissions if s.is_correct])
    
    def get_strategy_name(self) -> str:
        """Get the strategy name."""
        return "time_based"
    
    def get_strategy_description(self) -> str:
        """Get the strategy description."""
        return (
            "Time-based scoring awards points based on fastest completion time. "
            "1st place: 100 points, 2nd place: 75 points, 3rd place: 50 points, "
            "4th+ place: 25 points. Incorrect submissions receive 0 points."
        )


class PointBasedScoringStrategy(ScoringStrategy):
    """
    Point-based scoring strategy.
    
    Awards points starting from a base amount and decreasing by a fixed step
    for each position. For example:
    - Starting points: 1000
    - Decrease step: 50
    - 1st place: 1000 points
    - 2nd place: 950 points
    - 3rd place: 900 points
    - etc.
    
    Minimum points awarded is 10 (never goes below this).
    Incorrect submissions receive 0 points.
    
    Following SRP: Only handles point-based scoring calculations.
    """
    
    def __init__(self, starting_points: int, points_decrease_step: int):
        """
        Initialize point-based scoring strategy.
        
        Args:
            starting_points: Points awarded to first place
            points_decrease_step: Points to decrease for each subsequent position
            
        Raises:
            ValueError: If starting_points or points_decrease_step is not positive
        """
        if starting_points <= 0:
            raise ValueError("Starting points must be positive")
        
        if points_decrease_step <= 0:
            raise ValueError("Points decrease step must be positive")
        
        self.starting_points = starting_points
        self.points_decrease_step = points_decrease_step
        self.minimum_points = 10
    
    def calculate_points(
        self,
        submission: SubmissionProtocol,
        all_submissions: List[SubmissionProtocol],
        challenge_start_time: Optional[datetime] = None
    ) -> ScoringResult:
        """
        Calculate points based on position with decreasing point values.
        
        Args:
            submission: The submission to calculate points for
            all_submissions: All submissions for this challenge
            challenge_start_time: When the challenge started (for time tracking)
            
        Returns:
            ScoringResult with point-based calculation
            
        Raises:
            ValueError: If submission or all_submissions is None
        """
        # Input validation
        if submission is None:
            raise ValueError("Submission cannot be None")
        
        if all_submissions is None:
            raise ValueError("All submissions list cannot be None")
        
        # Initialize calculation details
        calculation_details = {
            "scoring_method": "point_based",
            "starting_points": self.starting_points,
            "points_decrease_step": self.points_decrease_step,
            "minimum_points": self.minimum_points
        }
        
        # Add time taken if challenge start time is provided
        if challenge_start_time and submission.submission_timestamp:
            time_taken = submission.submission_timestamp - challenge_start_time
            calculation_details["time_taken_minutes"] = int(time_taken.total_seconds() / 60)
        
        # If submission is incorrect, award 0 points
        if not submission.is_correct:
            calculation_details["reason"] = "incorrect_submission"
            return ScoringResult(
                points_awarded=0,
                position=0,
                total_participants=self._count_correct_submissions(all_submissions),
                calculation_details=calculation_details
            )
        
        # Get only correct submissions for ranking
        correct_submissions = [s for s in all_submissions if s.is_correct]
        
        # Sort by submission timestamp (earliest first)
        sorted_submissions = sorted(
            correct_submissions,
            key=lambda s: s.submission_timestamp
        )
        
        # Find position of this submission
        position = 0
        for i, sub in enumerate(sorted_submissions, 1):
            if sub.participant_id == submission.participant_id:
                position = i
                break
        
        # Calculate points based on position
        points_awarded = self._calculate_point_based_points(position)
        
        calculation_details["position_details"] = {
            "finished_position": position,
            "out_of_total": len(correct_submissions),
            "calculated_points": self.starting_points - (position - 1) * self.points_decrease_step
        }
        
        # Check if minimum points were enforced
        if points_awarded == self.minimum_points and calculation_details["position_details"]["calculated_points"] < self.minimum_points:
            calculation_details["minimum_enforced"] = True
        
        return ScoringResult(
            points_awarded=points_awarded,
            position=position,
            total_participants=len(correct_submissions),
            calculation_details=calculation_details
        )
    
    def _calculate_point_based_points(self, position: int) -> int:
        """
        Calculate points based on position with decreasing values.
        
        Args:
            position: Finishing position (1-based)
            
        Returns:
            Points awarded for the position (minimum 10 points)
        """
        calculated_points = self.starting_points - (position - 1) * self.points_decrease_step
        
        # Enforce minimum points
        return max(calculated_points, self.minimum_points)
    
    def _count_correct_submissions(self, all_submissions: List[SubmissionProtocol]) -> int:
        """
        Count the number of correct submissions.
        
        Args:
            all_submissions: All submissions to count
            
        Returns:
            Number of correct submissions
        """
        return len([s for s in all_submissions if s.is_correct])
    
    def get_strategy_name(self) -> str:
        """Get the strategy name."""
        return "point_based"
    
    def get_strategy_description(self) -> str:
        """Get the strategy description."""
        return (
            f"Point-based scoring starts with {self.starting_points} points for 1st place "
            f"and decreases by {self.points_decrease_step} points for each subsequent position. "
            f"Minimum {self.minimum_points} points awarded. Incorrect submissions receive 0 points."
        )


def create_scoring_strategy(
    scoring_type: str,
    starting_points: Optional[int] = None,
    points_decrease_step: Optional[int] = None
) -> ScoringStrategy:
    """
    Factory function to create scoring strategies.
    
    Following the Factory Pattern for flexible strategy creation.
    
    Args:
        scoring_type: Type of scoring ("time_based" or "point_based")
        starting_points: Starting points for point-based scoring
        points_decrease_step: Point decrease step for point-based scoring
        
    Returns:
        Appropriate ScoringStrategy instance
        
    Raises:
        ValueError: If scoring_type is invalid or required parameters are missing
    """
    if scoring_type == "time_based":
        return TimeBasedScoringStrategy()
    
    elif scoring_type == "point_based":
        if starting_points is None or points_decrease_step is None:
            raise ValueError(
                "Point-based scoring requires starting_points and points_decrease_step"
            )
        return PointBasedScoringStrategy(starting_points, points_decrease_step)
    
    else:
        raise ValueError(f"Unknown scoring type: {scoring_type}")