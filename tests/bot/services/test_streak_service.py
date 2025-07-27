"""Comprehensive tests for StreakService.

This test suite ensures 100% reliability of streak calculations for production
deployment. Tests cover all edge cases, boundary conditions, and potential
data corruption scenarios.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict

import pytest

from smarter_dev.bot.services.streak_service import StreakService, StreakCalculationResult
from smarter_dev.shared.date_provider import MockDateProvider


class TestStreakService:
    """Test suite for StreakService core functionality."""
    
    @pytest.fixture
    def mock_date_provider(self) -> MockDateProvider:
        """Create a mock date provider for deterministic testing."""
        return MockDateProvider(fixed_date=date(2024, 1, 15))
    
    @pytest.fixture
    def streak_service(self, mock_date_provider: MockDateProvider) -> StreakService:
        """Create a StreakService with mock date provider."""
        return StreakService(date_provider=mock_date_provider)
    
    @pytest.fixture
    def standard_bonuses(self) -> Dict[str, int]:
        """Standard streak bonus configuration."""
        return {"7": 2, "14": 3, "30": 5}
    
    def test_new_user_first_claim(self, streak_service: StreakService, standard_bonuses: Dict[str, int]):
        """Test streak calculation for new user's first claim."""
        result = streak_service.calculate_streak_result(
            last_daily=None,
            current_streak=0,
            daily_amount=10,
            streak_bonuses=standard_bonuses
        )
        
        assert result.new_streak_count == 1
        assert result.can_claim is True
        assert result.streak_bonus == 1
        assert result.reward_amount == 10
        assert result.is_streak_broken is False
        assert result.days_since_last_claim is None
    
    def test_consecutive_days_streak_continues(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider,
        standard_bonuses: Dict[str, int]
    ):
        """Test streak continues when claiming consecutive days."""
        # Set current date to Jan 15, 2024
        current_date = date(2024, 1, 15)
        yesterday = current_date - timedelta(days=1)
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=yesterday,  # Claimed yesterday
            current_streak=5,
            daily_amount=10,
            streak_bonuses=standard_bonuses
        )
        
        assert result.new_streak_count == 6
        assert result.can_claim is True
        assert result.streak_bonus == 1  # Not at 7-day threshold yet
        assert result.reward_amount == 10
        assert result.is_streak_broken is False
        assert result.days_since_last_claim == 1
    
    def test_gap_of_one_day_resets_streak(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider,
        standard_bonuses: Dict[str, int]
    ):
        """Test streak resets after missing one day."""
        current_date = date(2024, 1, 15)
        two_days_ago = current_date - timedelta(days=2)
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=two_days_ago,  # Missed yesterday
            current_streak=10,
            daily_amount=10,
            streak_bonuses=standard_bonuses
        )
        
        assert result.new_streak_count == 1  # Reset to 1
        assert result.can_claim is True
        assert result.streak_bonus == 1
        assert result.reward_amount == 10
        assert result.is_streak_broken is True
        assert result.days_since_last_claim == 2
    
    def test_gap_of_multiple_days_resets_streak(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider,
        standard_bonuses: Dict[str, int]
    ):
        """Test streak resets after missing multiple days."""
        current_date = date(2024, 1, 15)
        week_ago = current_date - timedelta(days=7)
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=week_ago,
            current_streak=30,  # Had a long streak
            daily_amount=10,
            streak_bonuses=standard_bonuses
        )
        
        assert result.new_streak_count == 1
        assert result.can_claim is True
        assert result.streak_bonus == 1
        assert result.reward_amount == 10
        assert result.is_streak_broken is True
        assert result.days_since_last_claim == 7
    
    def test_same_day_multiple_claims_blocked(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider,
        standard_bonuses: Dict[str, int]
    ):
        """Test that multiple claims on same day are blocked."""
        current_date = date(2024, 1, 15)
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=current_date,  # Already claimed today
            current_streak=5,
            daily_amount=10,
            streak_bonuses=standard_bonuses
        )
        
        assert result.can_claim is False
        # Other values are still calculated for completeness
        assert result.new_streak_count == 1  # Would reset due to "future" claim
        assert result.is_streak_broken is True


class TestStreakBonusCalculation:
    """Test suite for streak bonus calculations."""
    
    @pytest.fixture
    def streak_service(self) -> StreakService:
        """Create a basic StreakService for bonus testing."""
        return StreakService()
    
    def test_bonus_thresholds_exact_match(self, streak_service: StreakService):
        """Test bonus calculation at exact threshold values."""
        bonuses = {"7": 2, "14": 3, "30": 5}
        
        # Test exact threshold matches
        assert streak_service.calculate_streak_bonus(7, bonuses) == 2
        assert streak_service.calculate_streak_bonus(14, bonuses) == 3
        assert streak_service.calculate_streak_bonus(30, bonuses) == 5
    
    def test_bonus_thresholds_exceed(self, streak_service: StreakService):
        """Test bonus calculation - divisible milestones get bonuses."""
        bonuses = {"7": 2, "14": 3, "30": 5}
        
        # Test that bonuses apply on divisible milestone days
        assert streak_service.calculate_streak_bonus(10, bonuses) == 1  # No bonus (not divisible)
        assert streak_service.calculate_streak_bonus(21, bonuses) == 2  # Divisible by 7
        assert streak_service.calculate_streak_bonus(28, bonuses) == 3  # Divisible by 14 (higher than 7)
        assert streak_service.calculate_streak_bonus(60, bonuses) == 5  # Divisible by 30
        assert streak_service.calculate_streak_bonus(70, bonuses) == 2  # Divisible by 7
    
    def test_multiple_threshold_crossing(self, streak_service: StreakService):
        """Test bonus calculation - divisible milestone days get bonuses."""
        bonuses = {"7": 2, "14": 4, "30": 10}
        
        # Test progression through thresholds - bonuses on divisible days
        assert streak_service.calculate_streak_bonus(6, bonuses) == 1   # No bonus
        assert streak_service.calculate_streak_bonus(7, bonuses) == 2   # Divisible by 7
        assert streak_service.calculate_streak_bonus(8, bonuses) == 1   # No bonus (not divisible)
        assert streak_service.calculate_streak_bonus(13, bonuses) == 1  # No bonus (not divisible)
        assert streak_service.calculate_streak_bonus(14, bonuses) == 4  # Divisible by 14 (higher than 7)
        assert streak_service.calculate_streak_bonus(15, bonuses) == 1  # No bonus (not divisible)
        assert streak_service.calculate_streak_bonus(21, bonuses) == 2  # Divisible by 7
        assert streak_service.calculate_streak_bonus(28, bonuses) == 4  # Divisible by 14
        assert streak_service.calculate_streak_bonus(30, bonuses) == 10 # Divisible by 30
        assert streak_service.calculate_streak_bonus(42, bonuses) == 4  # Divisible by both 7 and 14, takes highest
    
    def test_empty_bonus_config(self, streak_service: StreakService):
        """Test bonus calculation with empty configuration."""
        assert streak_service.calculate_streak_bonus(10, {}) == 1
        assert streak_service.calculate_streak_bonus(100, {}) == 1
    
    def test_malformed_bonus_config(self, streak_service: StreakService):
        """Test bonus calculation with malformed configuration."""
        malformed_bonuses = {
            "invalid": 2,      # Non-numeric key
            "7": "invalid",    # Non-numeric value
            7: 3,              # Numeric key (should be string)
            "14": 3.5,         # Float value
            "21": -1           # Negative value
        }
        
        # Should handle gracefully and return minimum bonus
        result = streak_service.calculate_streak_bonus(25, malformed_bonuses)
        assert result >= 1  # Should not crash and return at least 1
    
    def test_very_high_streak_bonuses(self, streak_service: StreakService):
        """Test bonus calculation with very high streak counts - only exact milestones."""
        bonuses = {"100": 10, "365": 50, "1000": 100}
        
        assert streak_service.calculate_streak_bonus(100, bonuses) == 10   # Exact milestone
        assert streak_service.calculate_streak_bonus(365, bonuses) == 50   # Exact milestone
        assert streak_service.calculate_streak_bonus(1000, bonuses) == 100 # Exact milestone
        assert streak_service.calculate_streak_bonus(500, bonuses) == 1    # Between milestones
        assert streak_service.calculate_streak_bonus(1500, bonuses) == 1   # Past all milestones
        assert streak_service.calculate_streak_bonus(99, bonuses) == 1     # Before first milestone
    
    def test_non_sequential_bonus_config(self, streak_service: StreakService):
        """Test bonus calculation with non-sequential configuration - only exact milestones."""
        bonuses = {"7": 2, "21": 5, "90": 10}  # Skipping 14, 30, etc.
        
        assert streak_service.calculate_streak_bonus(7, bonuses) == 2   # Exact milestone
        assert streak_service.calculate_streak_bonus(21, bonuses) == 5  # Exact milestone
        assert streak_service.calculate_streak_bonus(90, bonuses) == 10 # Exact milestone
        assert streak_service.calculate_streak_bonus(14, bonuses) == 1  # Between milestones
        assert streak_service.calculate_streak_bonus(30, bonuses) == 1  # Between milestones


class TestDateBoundaryEdgeCases:
    """Test suite for date boundary edge cases."""
    
    @pytest.fixture
    def mock_date_provider(self) -> MockDateProvider:
        """Create a mock date provider for boundary testing."""
        return MockDateProvider()
    
    @pytest.fixture
    def streak_service(self, mock_date_provider: MockDateProvider) -> StreakService:
        """Create a StreakService with mock date provider."""
        return StreakService(date_provider=mock_date_provider)
    
    def test_month_boundary_streak_continue(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test streak continues across month boundaries."""
        # Test January 31 -> February 1
        current_date = date(2024, 2, 1)
        yesterday = date(2024, 1, 31)
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=yesterday,
            current_streak=5,
            daily_amount=10,
            streak_bonuses={"7": 2}
        )
        
        assert result.new_streak_count == 6
        assert result.can_claim is True
        assert result.is_streak_broken is False
    
    def test_year_boundary_streak_continue(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test streak continues across year boundaries."""
        # Test December 31 -> January 1
        current_date = date(2025, 1, 1)
        yesterday = date(2024, 12, 31)
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=yesterday,
            current_streak=10,
            daily_amount=10,
            streak_bonuses={"7": 2}
        )
        
        assert result.new_streak_count == 11
        assert result.can_claim is True
        assert result.is_streak_broken is False
    
    def test_leap_year_feb_28_to_29(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test streak continues across leap year Feb 28 -> 29."""
        # 2024 is a leap year
        current_date = date(2024, 2, 29)
        yesterday = date(2024, 2, 28)
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=yesterday,
            current_streak=15,
            daily_amount=10,
            streak_bonuses={"7": 2, "14": 3}
        )
        
        assert result.new_streak_count == 16
        assert result.can_claim is True
        assert result.is_streak_broken is False
        assert result.streak_bonus == 3  # Should get 14-day bonus
    
    def test_leap_year_to_non_leap_year(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test date calculations across leap/non-leap year transitions."""
        # Test Feb 28, 2025 (non-leap year) after Feb 29, 2024 (leap year)
        current_date = date(2025, 2, 28)
        many_days_ago = date(2024, 2, 29)  # Valid leap year date
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=many_days_ago,
            current_streak=100,
            daily_amount=10,
            streak_bonuses={"30": 5}
        )
        
        # Should reset due to long gap
        assert result.new_streak_count == 1
        assert result.is_streak_broken is True
        assert result.days_since_last_claim > 300  # About a year


class TestDataCorruptionHandling:
    """Test suite for handling corrupted data scenarios."""
    
    @pytest.fixture
    def mock_date_provider(self) -> MockDateProvider:
        """Create a mock date provider for corruption testing."""
        return MockDateProvider(fixed_date=date(2024, 1, 15))
    
    @pytest.fixture
    def streak_service(self, mock_date_provider: MockDateProvider) -> StreakService:
        """Create a StreakService with mock date provider."""
        return StreakService(date_provider=mock_date_provider)
    
    def test_future_date_handling(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test handling of corrupted future last_daily dates."""
        current_date = date(2024, 1, 15)
        future_date = date(2024, 1, 16)  # Tomorrow
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=future_date,  # Corrupted: future date
            current_streak=10,
            daily_amount=10,
            streak_bonuses={"7": 2}
        )
        
        # Should reset streak and block claim
        assert result.new_streak_count == 1
        assert result.can_claim is False
        assert result.is_streak_broken is True
        assert result.days_since_last_claim == 0
    
    def test_far_future_date_handling(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test handling of far future corrupted dates."""
        current_date = date(2024, 1, 15)
        far_future = date(2025, 1, 15)  # One year in future
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=far_future,
            current_streak=100,
            daily_amount=10,
            streak_bonuses={"30": 5}
        )
        
        assert result.new_streak_count == 1
        assert result.can_claim is False
        assert result.is_streak_broken is True
    
    def test_far_past_date_handling(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test handling of very old last_daily dates."""
        current_date = date(2024, 1, 15)
        far_past = date(2020, 1, 15)  # Four years ago
        
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_result(
            last_daily=far_past,
            current_streak=1000,  # Unrealistic streak
            daily_amount=10,
            streak_bonuses={"30": 5}
        )
        
        assert result.new_streak_count == 1
        assert result.can_claim is True
        assert result.is_streak_broken is True
        assert result.days_since_last_claim > 1400  # About 4 years


class TestStreakValidation:
    """Test suite for streak data validation."""
    
    @pytest.fixture
    def streak_service(self) -> StreakService:
        """Create a basic StreakService for validation testing."""
        return StreakService()
    
    def test_validate_consistent_data(self, streak_service: StreakService):
        """Test validation of consistent streak data."""
        current_date = date(2024, 1, 15)
        yesterday = date(2024, 1, 14)
        
        # Valid scenarios
        assert streak_service.validate_streak_data(None, 0, current_date) is True
        assert streak_service.validate_streak_data(yesterday, 5, current_date) is True
        assert streak_service.validate_streak_data(current_date, 1, current_date) is True
    
    def test_validate_negative_streak_count(self, streak_service: StreakService):
        """Test validation rejects negative streak counts."""
        current_date = date(2024, 1, 15)
        yesterday = date(2024, 1, 14)
        
        assert streak_service.validate_streak_data(yesterday, -1, current_date) is False
        assert streak_service.validate_streak_data(None, -5, current_date) is False
    
    def test_validate_future_last_daily(self, streak_service: StreakService):
        """Test validation rejects future last_daily dates."""
        current_date = date(2024, 1, 15)
        future_date = date(2024, 1, 16)
        
        assert streak_service.validate_streak_data(future_date, 5, current_date) is False
    
    def test_validate_never_claimed_with_streak(self, streak_service: StreakService):
        """Test validation rejects streak > 0 with no claims."""
        current_date = date(2024, 1, 15)
        
        assert streak_service.validate_streak_data(None, 5, current_date) is False
        assert streak_service.validate_streak_data(None, 1, current_date) is False


class TestCanClaimToday:
    """Test suite for can_claim_today method."""
    
    @pytest.fixture
    def mock_date_provider(self) -> MockDateProvider:
        """Create a mock date provider for claim testing."""
        return MockDateProvider(fixed_date=date(2024, 1, 15))
    
    @pytest.fixture
    def streak_service(self, mock_date_provider: MockDateProvider) -> StreakService:
        """Create a StreakService with mock date provider."""
        return StreakService(date_provider=mock_date_provider)
    
    def test_new_user_can_claim(self, streak_service: StreakService):
        """Test new user can always claim."""
        assert streak_service.can_claim_today(None) is True
    
    def test_already_claimed_today_cannot_claim(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test user cannot claim if already claimed today."""
        current_date = date(2024, 1, 15)
        mock_date_provider.set_date(current_date)
        
        assert streak_service.can_claim_today(current_date) is False
    
    def test_claimed_yesterday_can_claim(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test user can claim if last claim was yesterday."""
        current_date = date(2024, 1, 15)
        yesterday = date(2024, 1, 14)
        mock_date_provider.set_date(current_date)
        
        assert streak_service.can_claim_today(yesterday) is True
    
    def test_claimed_days_ago_can_claim(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test user can claim if last claim was days ago."""
        current_date = date(2024, 1, 15)
        days_ago = date(2024, 1, 10)
        mock_date_provider.set_date(current_date)
        
        assert streak_service.can_claim_today(days_ago) is True


class TestCalculateStreakCount:
    """Test suite for calculate_streak_count method."""
    
    @pytest.fixture
    def mock_date_provider(self) -> MockDateProvider:
        """Create a mock date provider for streak count testing."""
        return MockDateProvider(fixed_date=date(2024, 1, 15))
    
    @pytest.fixture
    def streak_service(self, mock_date_provider: MockDateProvider) -> StreakService:
        """Create a StreakService with mock date provider."""
        return StreakService(date_provider=mock_date_provider)
    
    def test_new_user_gets_streak_one(self, streak_service: StreakService):
        """Test new user gets streak count of 1."""
        result = streak_service.calculate_streak_count(None, 0)
        assert result == 1
    
    def test_consecutive_claim_increments_streak(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test consecutive daily claim increments streak."""
        current_date = date(2024, 1, 15)
        yesterday = date(2024, 1, 14)
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_count(yesterday, 5)
        assert result == 6
    
    def test_gap_resets_streak_to_one(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test gap in claims resets streak to 1."""
        current_date = date(2024, 1, 15)
        two_days_ago = date(2024, 1, 13)
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_count(two_days_ago, 10)
        assert result == 1
    
    def test_future_date_resets_streak(
        self, 
        streak_service: StreakService, 
        mock_date_provider: MockDateProvider
    ):
        """Test future last_daily resets streak."""
        current_date = date(2024, 1, 15)
        future_date = date(2024, 1, 16)
        mock_date_provider.set_date(current_date)
        
        result = streak_service.calculate_streak_count(future_date, 20)
        assert result == 1


# Property-based tests using hypothesis (if available)
try:
    from hypothesis import given, strategies as st
    from hypothesis import assume
    
    class TestStreakServiceProperties:
        """Property-based tests for StreakService."""
        
        @given(
            streak_count=st.integers(min_value=1, max_value=1000),
            daily_amount=st.integers(min_value=1, max_value=1000),
            bonus_multiplier=st.integers(min_value=1, max_value=10)
        )
        def test_reward_calculation_properties(self, streak_count, daily_amount, bonus_multiplier):
            """Test mathematical properties of reward calculation."""
            service = StreakService()
            bonuses = {"1": bonus_multiplier}
            
            result = service.calculate_streak_result(
                last_daily=None,
                current_streak=0,
                daily_amount=daily_amount,
                streak_bonuses=bonuses
            )
            
            # Reward should always be positive
            assert result.reward_amount > 0
            # Reward should equal daily_amount * bonus
            assert result.reward_amount == daily_amount * bonus_multiplier
            # Streak count should always be positive
            assert result.new_streak_count > 0
        
        @given(
            current_date=st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
            days_back=st.integers(min_value=0, max_value=1000)
        )
        def test_date_calculation_properties(self, current_date, days_back):
            """Test properties of date calculations."""
            service = StreakService()
            mock_provider = MockDateProvider(fixed_date=current_date)
            service._date_provider = mock_provider
            
            last_daily = current_date - timedelta(days=days_back) if days_back > 0 else None
            
            # Calculate result
            result = service.calculate_streak_result(
                last_daily=last_daily,
                current_streak=5,
                daily_amount=10,
                streak_bonuses={"7": 2}
            )
            
            # Next claim date should always be tomorrow
            assert result.next_claim_date == current_date + timedelta(days=1)
            
            # If gap > 1 day, streak should reset
            if days_back > 1:
                assert result.new_streak_count == 1
                assert result.is_streak_broken is True
            elif days_back == 1:  # Yesterday
                assert result.new_streak_count == 6  # 5 + 1
                assert result.is_streak_broken is False

except ImportError:
    # Hypothesis not available, skip property-based tests
    pass