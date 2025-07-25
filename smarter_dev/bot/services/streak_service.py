"""Streak calculation service for the bytes economy system.

This module contains the core business logic for calculating daily claim streaks.
It follows the Single Responsibility Principle by focusing solely on streak
calculations and the Open/Closed Principle by allowing extension through
configuration without modification.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, NamedTuple, Optional

from smarter_dev.shared.date_provider import DateProvider, get_date_provider


class StreakCalculationResult(NamedTuple):
    """Result of streak calculation with all relevant data.
    
    This immutable result object contains all information needed
    for streak processing, following the principle of explicit
    data structures over primitive obsession.
    """
    new_streak_count: int
    can_claim: bool
    streak_bonus: int
    reward_amount: int
    next_claim_date: date
    is_streak_broken: bool
    days_since_last_claim: Optional[int]


class StreakService:
    """Service for calculating daily claim streaks and bonuses.
    
    This service encapsulates all streak-related business logic and follows
    SOLID principles:
    - Single Responsibility: Only handles streak calculations
    - Open/Closed: Extensible through configuration
    - Liskov Substitution: Can be substituted with other streak implementations
    - Interface Segregation: Minimal, focused interface
    - Dependency Inversion: Depends on DateProvider abstraction
    """
    
    def __init__(self, date_provider: Optional[DateProvider] = None):
        """Initialize the streak service.
        
        Args:
            date_provider: Date provider for time operations, defaults to UTC provider
        """
        self._date_provider = date_provider or get_date_provider()
    
    def calculate_streak_result(
        self,
        last_daily: Optional[date],
        current_streak: int,
        daily_amount: int,
        streak_bonuses: Dict[str, int],
        current_date: Optional[date] = None
    ) -> StreakCalculationResult:
        """Calculate complete streak result for daily claim.
        
        This is the main entry point for streak calculations, providing
        all information needed to process a daily claim.
        
        Args:
            last_daily: Date of last daily claim, None if never claimed
            current_streak: Current streak count
            daily_amount: Base daily reward amount
            streak_bonuses: Dict mapping streak days to multipliers (e.g. {"7": 2, "14": 3})
            current_date: Current date, defaults to today
            
        Returns:
            StreakCalculationResult: Complete calculation result
        """
        if current_date is None:
            current_date = self._date_provider.today()
        
        # Check if user can claim today
        can_claim = self.can_claim_today(last_daily, current_date)
        
        # Calculate new streak count
        new_streak = self.calculate_streak_count(last_daily, current_streak, current_date)
        
        # Determine if streak was broken
        is_broken = self._is_streak_broken(last_daily, current_date)
        
        # Calculate days since last claim
        days_since = self._calculate_days_since_last_claim(last_daily, current_date)
        
        # Calculate streak bonus
        streak_bonus = self.calculate_streak_bonus(new_streak, streak_bonuses)
        
        # Calculate reward amount
        reward_amount = daily_amount * streak_bonus
        
        # Calculate next claim date
        next_claim_date = current_date + timedelta(days=1)
        
        return StreakCalculationResult(
            new_streak_count=new_streak,
            can_claim=can_claim,
            streak_bonus=streak_bonus,
            reward_amount=reward_amount,
            next_claim_date=next_claim_date,
            is_streak_broken=is_broken,
            days_since_last_claim=days_since
        )
    
    def can_claim_today(
        self, 
        last_daily: Optional[date], 
        current_date: Optional[date] = None
    ) -> bool:
        """Check if user can claim daily reward today.
        
        Args:
            last_daily: Date of last daily claim, None if never claimed
            current_date: Current date, defaults to today
            
        Returns:
            bool: True if user can claim today, False if already claimed
        """
        if current_date is None:
            current_date = self._date_provider.today()
        
        # New users can always claim
        if last_daily is None:
            return True
        
        # Can't claim if already claimed today
        if last_daily == current_date:
            return False
        
        # Can claim if last claim was before today
        return last_daily < current_date
    
    def calculate_streak_count(
        self,
        last_daily: Optional[date],
        current_streak: int,
        current_date: Optional[date] = None
    ) -> int:
        """Calculate the new streak count based on claim history.
        
        Streak calculation rules:
        - New user (last_daily is None): streak = 1
        - Claimed yesterday: streak = current_streak + 1
        - Gap of 1+ days: streak = 1 (reset)
        - Future last_daily (data corruption): streak = 1 (reset)
        
        Args:
            last_daily: Date of last daily claim
            current_streak: Current streak count
            current_date: Current date, defaults to today
            
        Returns:
            int: New streak count (always >= 1)
        """
        if current_date is None:
            current_date = self._date_provider.today()
        
        # New user starts with streak of 1
        if last_daily is None:
            return 1
        
        # Calculate yesterday's date
        yesterday = current_date - timedelta(days=1)
        
        # Continue streak if claimed yesterday
        if last_daily == yesterday:
            return current_streak + 1
        
        # Reset streak if gap or data corruption (future date)
        if last_daily < yesterday or last_daily >= current_date:
            return 1
        
        # Should not reach here, but reset to be safe
        return 1
    
    def calculate_streak_bonus(
        self, 
        streak_count: int, 
        streak_bonuses: Dict[str, int]
    ) -> int:
        """Calculate the streak bonus multiplier.
        
        Applies bonus multipliers on days divisible by milestone intervals.
        This allows streaks to work in perpetuity.
        Example: streak_bonuses = {"7": 2, "14": 4, "30": 10}
        - streak 6: bonus = 1 (no bonus)
        - streak 7: bonus = 2 (divisible by 7)
        - streak 14: bonus = 4 (divisible by 14, higher than 7's bonus)
        - streak 21: bonus = 2 (divisible by 7)
        - streak 28: bonus = 4 (divisible by 14)
        - streak 30: bonus = 10 (divisible by 30)
        - streak 42: bonus = 4 (divisible by both 7 and 14, takes highest)
        
        Args:
            streak_count: Current streak count
            streak_bonuses: Dict mapping milestone intervals to multipliers
            
        Returns:
            int: Bonus multiplier (minimum 1)
        """
        if not streak_bonuses or streak_count <= 0:
            return 1
        
        # Find the highest applicable bonus for divisible milestones
        applicable_bonus = 1
        
        try:
            for milestone_str, multiplier in streak_bonuses.items():
                milestone = int(milestone_str)
                # Check if streak count is divisible by this milestone
                if streak_count % milestone == 0 and multiplier > applicable_bonus:
                    applicable_bonus = multiplier
        except (ValueError, TypeError):
            # Handle malformed bonus configuration gracefully
            pass
        
        return max(1, applicable_bonus)  # Ensure minimum bonus of 1
    
    def _is_streak_broken(
        self, 
        last_daily: Optional[date], 
        current_date: date
    ) -> bool:
        """Determine if the streak was broken (gap > 1 day).
        
        Args:
            last_daily: Date of last daily claim
            current_date: Current date
            
        Returns:
            bool: True if streak was broken, False if continuing or new
        """
        if last_daily is None:
            return False  # New user, no streak to break
        
        yesterday = current_date - timedelta(days=1)
        
        # Streak is broken if last claim was before yesterday
        # or if there's data corruption (future date)
        return last_daily < yesterday or last_daily >= current_date
    
    def _calculate_days_since_last_claim(
        self, 
        last_daily: Optional[date], 
        current_date: date
    ) -> Optional[int]:
        """Calculate days since last claim.
        
        Args:
            last_daily: Date of last daily claim
            current_date: Current date
            
        Returns:
            Optional[int]: Days since last claim, None if never claimed
        """
        if last_daily is None:
            return None
        
        # Handle data corruption (future dates) gracefully
        if last_daily >= current_date:
            return 0
        
        delta = current_date - last_daily
        return delta.days
    
    def validate_streak_data(
        self, 
        last_daily: Optional[date], 
        streak_count: int,
        current_date: Optional[date] = None
    ) -> bool:
        """Validate streak data for consistency.
        
        This method can be used to detect data corruption or
        inconsistencies in streak records.
        
        Args:
            last_daily: Date of last daily claim
            streak_count: Current streak count
            current_date: Current date, defaults to today
            
        Returns:
            bool: True if data is consistent, False if corrupted
        """
        if current_date is None:
            current_date = self._date_provider.today()
        
        # Negative streak count is invalid
        if streak_count < 0:
            return False
        
        # Future last_daily is invalid
        if last_daily is not None and last_daily > current_date:
            return False
        
        # If user has never claimed, streak should be 0
        if last_daily is None and streak_count > 0:
            return False
        
        # If last claim was today, that's unusual but not invalid
        # (could happen during testing or edge cases)
        
        return True