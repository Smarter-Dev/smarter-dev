"""Test cases for Scoring Strategy implementations."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
from unittest.mock import Mock

from web.services.scoring_strategies import (
    ScoringStrategy,
    TimeBasedScoringStrategy,
    PointBasedScoringStrategy,
    ScoringResult,
    create_scoring_strategy
)


class TestScoringResult:
    """Test cases for ScoringResult data structure."""
    
    def test_scoring_result_creation(self):
        """Test ScoringResult creation with all fields."""
        result = ScoringResult(
            points_awarded=100,
            position=1,
            total_participants=10,
            calculation_details={"method": "time_based", "time_taken": 120}
        )
        
        assert result.points_awarded == 100
        assert result.position == 1
        assert result.total_participants == 10
        assert result.calculation_details["method"] == "time_based"
        assert result.calculation_details["time_taken"] == 120
    
    def test_scoring_result_with_minimal_data(self):
        """Test ScoringResult with minimal required data."""
        result = ScoringResult(
            points_awarded=50,
            position=2,
            total_participants=5
        )
        
        assert result.points_awarded == 50
        assert result.position == 2
        assert result.total_participants == 5
        assert result.calculation_details == {}


class TestTimeBasedScoringStrategy:
    """Test cases for Time-Based Scoring Strategy."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.strategy = TimeBasedScoringStrategy()
        self.base_time = datetime.now(timezone.utc)
        
        # Create mock submissions for testing
        self.submissions = [
            Mock(
                participant_id="player1",
                submission_timestamp=self.base_time + timedelta(minutes=5),
                is_correct=True
            ),
            Mock(
                participant_id="player2", 
                submission_timestamp=self.base_time + timedelta(minutes=10),
                is_correct=True
            ),
            Mock(
                participant_id="player3",
                submission_timestamp=self.base_time + timedelta(minutes=15),
                is_correct=True
            ),
            Mock(
                participant_id="player4",
                submission_timestamp=self.base_time + timedelta(minutes=20),
                is_correct=False  # Incorrect submission
            )
        ]
    
    def test_calculate_points_first_place(self):
        """Test points calculation for first place."""
        result = self.strategy.calculate_points(
            submission=self.submissions[0],
            all_submissions=self.submissions,
            challenge_start_time=self.base_time
        )
        
        assert result.points_awarded == 100  # First place gets 100 points
        assert result.position == 1
        assert result.total_participants == 3  # Only correct submissions count
        assert result.calculation_details["time_taken_minutes"] == 5
        assert result.calculation_details["scoring_method"] == "time_based"
    
    def test_calculate_points_second_place(self):
        """Test points calculation for second place."""
        result = self.strategy.calculate_points(
            submission=self.submissions[1],
            all_submissions=self.submissions,
            challenge_start_time=self.base_time
        )
        
        assert result.points_awarded == 75  # Second place gets 75 points
        assert result.position == 2
        assert result.total_participants == 3
        assert result.calculation_details["time_taken_minutes"] == 10
    
    def test_calculate_points_third_place(self):
        """Test points calculation for third place."""
        result = self.strategy.calculate_points(
            submission=self.submissions[2],
            all_submissions=self.submissions,
            challenge_start_time=self.base_time
        )
        
        assert result.points_awarded == 50  # Third place gets 50 points
        assert result.position == 3
        assert result.total_participants == 3
        assert result.calculation_details["time_taken_minutes"] == 15
    
    def test_calculate_points_incorrect_submission(self):
        """Test points calculation for incorrect submission."""
        result = self.strategy.calculate_points(
            submission=self.submissions[3],
            all_submissions=self.submissions,
            challenge_start_time=self.base_time
        )
        
        assert result.points_awarded == 0  # Incorrect submission gets 0 points
        assert result.position == 0  # Not ranked
        assert result.total_participants == 3  # Only correct submissions count
        assert result.calculation_details["time_taken_minutes"] == 20
        assert result.calculation_details["reason"] == "incorrect_submission"
    
    def test_calculate_points_single_participant(self):
        """Test points calculation with single participant."""
        single_submission = [self.submissions[0]]
        
        result = self.strategy.calculate_points(
            submission=self.submissions[0],
            all_submissions=single_submission,
            challenge_start_time=self.base_time
        )
        
        assert result.points_awarded == 100  # First place gets 100 points
        assert result.position == 1
        assert result.total_participants == 1
    
    def test_calculate_points_no_challenge_start_time(self):
        """Test points calculation without challenge start time."""
        result = self.strategy.calculate_points(
            submission=self.submissions[0],
            all_submissions=self.submissions
        )
        
        assert result.points_awarded == 100  # Still gets first place points
        assert result.position == 1
        assert result.total_participants == 3
        # Should not include time_taken_minutes without start time
        assert "time_taken_minutes" not in result.calculation_details
    
    def test_get_strategy_name(self):
        """Test strategy name getter."""
        assert self.strategy.get_strategy_name() == "time_based"
    
    def test_get_strategy_description(self):
        """Test strategy description getter."""
        description = self.strategy.get_strategy_description()
        assert "fastest completion time" in description.lower()
        assert "100 points" in description
        assert "75 points" in description
        assert "50 points" in description


class TestPointBasedScoringStrategy:
    """Test cases for Point-Based Scoring Strategy."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.strategy = PointBasedScoringStrategy(
            starting_points=1000,
            points_decrease_step=50
        )
        self.base_time = datetime.now(timezone.utc)
        
        # Create mock submissions for testing
        self.submissions = [
            Mock(
                participant_id="player1",
                submission_timestamp=self.base_time + timedelta(minutes=5),
                is_correct=True
            ),
            Mock(
                participant_id="player2",
                submission_timestamp=self.base_time + timedelta(minutes=10),
                is_correct=True
            ),
            Mock(
                participant_id="player3",
                submission_timestamp=self.base_time + timedelta(minutes=15),
                is_correct=True
            ),
            Mock(
                participant_id="player4",
                submission_timestamp=self.base_time + timedelta(minutes=20),
                is_correct=False
            )
        ]
    
    def test_calculate_points_first_place(self):
        """Test points calculation for first place."""
        result = self.strategy.calculate_points(
            submission=self.submissions[0],
            all_submissions=self.submissions,
            challenge_start_time=self.base_time
        )
        
        assert result.points_awarded == 1000  # First place gets starting points
        assert result.position == 1
        assert result.total_participants == 3
        assert result.calculation_details["starting_points"] == 1000
        assert result.calculation_details["points_decrease_step"] == 50
        assert result.calculation_details["scoring_method"] == "point_based"
    
    def test_calculate_points_second_place(self):
        """Test points calculation for second place."""
        result = self.strategy.calculate_points(
            submission=self.submissions[1],
            all_submissions=self.submissions,
            challenge_start_time=self.base_time
        )
        
        assert result.points_awarded == 950  # Starting points - (position-1) * decrease_step
        assert result.position == 2
        assert result.total_participants == 3
    
    def test_calculate_points_third_place(self):
        """Test points calculation for third place."""
        result = self.strategy.calculate_points(
            submission=self.submissions[2],
            all_submissions=self.submissions,
            challenge_start_time=self.base_time
        )
        
        assert result.points_awarded == 900  # 1000 - (3-1) * 50 = 900
        assert result.position == 3
        assert result.total_participants == 3
    
    def test_calculate_points_minimum_points(self):
        """Test points calculation with minimum points enforcement."""
        # Create many submissions to test minimum points
        many_submissions = []
        for i in range(25):  # 25 correct submissions
            many_submissions.append(Mock(
                participant_id=f"player{i}",
                submission_timestamp=self.base_time + timedelta(minutes=i),
                is_correct=True
            ))
        
        # Test last place participant
        result = self.strategy.calculate_points(
            submission=many_submissions[24],  # 25th place
            all_submissions=many_submissions,
            challenge_start_time=self.base_time
        )
        
        # Should get minimum of 10 points instead of negative
        # 1000 - (25-1) * 50 = 1000 - 1200 = -200, but minimum is 10
        assert result.points_awarded == 10
        assert result.position == 25
        assert result.total_participants == 25
        assert result.calculation_details["minimum_enforced"] is True
    
    def test_calculate_points_incorrect_submission(self):
        """Test points calculation for incorrect submission."""
        result = self.strategy.calculate_points(
            submission=self.submissions[3],
            all_submissions=self.submissions,
            challenge_start_time=self.base_time
        )
        
        assert result.points_awarded == 0
        assert result.position == 0
        assert result.total_participants == 3
        assert result.calculation_details["reason"] == "incorrect_submission"
    
    def test_calculate_points_custom_parameters(self):
        """Test points calculation with custom parameters."""
        custom_strategy = PointBasedScoringStrategy(
            starting_points=500,
            points_decrease_step=25
        )
        
        result = custom_strategy.calculate_points(
            submission=self.submissions[1],  # Second place
            all_submissions=self.submissions,
            challenge_start_time=self.base_time
        )
        
        assert result.points_awarded == 475  # 500 - (2-1) * 25 = 475
        assert result.calculation_details["starting_points"] == 500
        assert result.calculation_details["points_decrease_step"] == 25
    
    def test_get_strategy_name(self):
        """Test strategy name getter."""
        assert self.strategy.get_strategy_name() == "point_based"
    
    def test_get_strategy_description(self):
        """Test strategy description getter."""
        description = self.strategy.get_strategy_description()
        assert "1000 points for 1st place" in description
        assert "decreases by 50" in description
        assert "position" in description.lower()


class TestScoringStrategyValidation:
    """Test cases for scoring strategy validation and edge cases."""
    
    def test_time_based_strategy_empty_submissions(self):
        """Test time-based strategy with empty submissions list."""
        strategy = TimeBasedScoringStrategy()
        submission = Mock(participant_id="player1", is_correct=True)
        
        result = strategy.calculate_points(
            submission=submission,
            all_submissions=[submission]
        )
        
        assert result.points_awarded == 100  # First place
        assert result.position == 1
        assert result.total_participants == 1
    
    def test_point_based_strategy_validation_errors(self):
        """Test point-based strategy parameter validation."""
        # Invalid starting points
        with pytest.raises(ValueError, match="Starting points must be positive"):
            PointBasedScoringStrategy(starting_points=0, points_decrease_step=50)
        
        # Invalid points decrease step
        with pytest.raises(ValueError, match="Points decrease step must be positive"):
            PointBasedScoringStrategy(starting_points=1000, points_decrease_step=0)
    
    def test_calculate_points_none_submission(self):
        """Test error handling for None submission."""
        strategy = TimeBasedScoringStrategy()
        
        with pytest.raises(ValueError, match="Submission cannot be None"):
            strategy.calculate_points(
                submission=None,
                all_submissions=[]
            )
    
    def test_calculate_points_none_submissions_list(self):
        """Test error handling for None submissions list."""
        strategy = TimeBasedScoringStrategy()
        submission = Mock(participant_id="player1", is_correct=True)
        
        with pytest.raises(ValueError, match="All submissions list cannot be None"):
            strategy.calculate_points(
                submission=submission,
                all_submissions=None
            )
    
    def test_time_based_strategy_tie_handling(self):
        """Test time-based strategy with tied submission times."""
        strategy = TimeBasedScoringStrategy()
        base_time = datetime.now(timezone.utc)
        
        # Two submissions at exactly the same time
        tied_submissions = [
            Mock(
                participant_id="player1",
                submission_timestamp=base_time + timedelta(minutes=5),
                is_correct=True
            ),
            Mock(
                participant_id="player2",
                submission_timestamp=base_time + timedelta(minutes=5),  # Same time
                is_correct=True
            )
        ]
        
        result1 = strategy.calculate_points(
            submission=tied_submissions[0],
            all_submissions=tied_submissions,
            challenge_start_time=base_time
        )
        
        result2 = strategy.calculate_points(
            submission=tied_submissions[1],
            all_submissions=tied_submissions,
            challenge_start_time=base_time
        )
        
        # With same timestamp, order depends on list order (deterministic behavior)
        # First submission in list gets better position
        assert result1.points_awarded == 100  # First in list gets 1st place
        assert result2.points_awarded == 75   # Second in list gets 2nd place
        assert result1.position == 1
        assert result2.position == 2


class TestScoringStrategyFactory:
    """Test cases for scoring strategy factory function."""
    
    def test_create_time_based_strategy(self):
        """Test creating time-based strategy."""
        strategy = create_scoring_strategy("time_based")
        
        assert isinstance(strategy, TimeBasedScoringStrategy)
        assert strategy.get_strategy_name() == "time_based"
    
    def test_create_point_based_strategy(self):
        """Test creating point-based strategy."""
        strategy = create_scoring_strategy(
            "point_based",
            starting_points=500,
            points_decrease_step=25
        )
        
        assert isinstance(strategy, PointBasedScoringStrategy)
        assert strategy.get_strategy_name() == "point_based"
        assert strategy.starting_points == 500
        assert strategy.points_decrease_step == 25
    
    def test_create_strategy_invalid_type(self):
        """Test creating strategy with invalid type."""
        with pytest.raises(ValueError, match="Unknown scoring type"):
            create_scoring_strategy("invalid_type")
    
    def test_create_point_based_strategy_missing_parameters(self):
        """Test creating point-based strategy with missing parameters."""
        with pytest.raises(ValueError, match="Point-based scoring requires"):
            create_scoring_strategy("point_based")
        
        with pytest.raises(ValueError, match="Point-based scoring requires"):
            create_scoring_strategy("point_based", starting_points=1000)
        
        with pytest.raises(ValueError, match="Point-based scoring requires"):
            create_scoring_strategy("point_based", points_decrease_step=50)