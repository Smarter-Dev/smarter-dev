"""Date provider abstraction for testable UTC date operations.

This module provides an abstraction layer for date operations to enable
dependency injection and comprehensive testing. All date operations use
UTC to avoid timezone confusion with Discord API timestamps.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from typing import Optional


class DateProvider(ABC):
    """Abstract interface for date operations.
    
    This interface follows the Dependency Inversion Principle, allowing
    the application to depend on abstractions rather than concrete
    implementations. This enables easy testing and future flexibility.
    """
    
    @abstractmethod
    def today(self) -> date:
        """Get the current date in UTC.
        
        Returns:
            date: Current UTC date
        """
        pass
    
    @abstractmethod
    def utcnow(self) -> datetime:
        """Get the current datetime in UTC.
        
        Returns:
            datetime: Current UTC datetime with timezone info
        """
        pass


class UTCDateProvider(DateProvider):
    """Production implementation of DateProvider using UTC.
    
    This implementation provides real UTC dates and times, suitable
    for production use with Discord API timestamps.
    """
    
    def today(self) -> date:
        """Get the current date in UTC.
        
        Returns:
            date: Current UTC date
        """
        return datetime.now(timezone.utc).date()
    
    def utcnow(self) -> datetime:
        """Get the current datetime in UTC.
        
        Returns:
            datetime: Current UTC datetime with timezone info
        """
        return datetime.now(timezone.utc)


class MockDateProvider(DateProvider):
    """Test implementation of DateProvider for controlled testing.
    
    This implementation allows tests to control the current date/time,
    enabling deterministic testing of time-sensitive functionality.
    """
    
    def __init__(self, fixed_date: Optional[date] = None, fixed_datetime: Optional[datetime] = None):
        """Initialize with optional fixed dates.
        
        Args:
            fixed_date: Fixed date to return from today(), defaults to 2024-01-15
            fixed_datetime: Fixed datetime to return from utcnow(), defaults to UTC version of fixed_date
        """
        self._fixed_date = fixed_date or date(2024, 1, 15)
        self._fixed_datetime = fixed_datetime or datetime.combine(
            self._fixed_date, 
            datetime.min.time()
        ).replace(tzinfo=timezone.utc)
    
    def today(self) -> date:
        """Get the fixed test date.
        
        Returns:
            date: Fixed date set during initialization
        """
        return self._fixed_date
    
    def utcnow(self) -> datetime:
        """Get the fixed test datetime.
        
        Returns:
            datetime: Fixed datetime set during initialization
        """
        return self._fixed_datetime
    
    def set_date(self, new_date: date) -> None:
        """Update the fixed date for testing.
        
        Args:
            new_date: New date to return from today()
        """
        self._fixed_date = new_date
        self._fixed_datetime = datetime.combine(
            new_date, 
            datetime.min.time()
        ).replace(tzinfo=timezone.utc)
    
    def set_datetime(self, new_datetime: datetime) -> None:
        """Update the fixed datetime for testing.
        
        Args:
            new_datetime: New datetime to return from utcnow()
        """
        if new_datetime.tzinfo is None:
            new_datetime = new_datetime.replace(tzinfo=timezone.utc)
        self._fixed_datetime = new_datetime
        self._fixed_date = new_datetime.date()
    
    def advance_days(self, days: int) -> None:
        """Advance the current date by specified number of days.
        
        Args:
            days: Number of days to advance (can be negative)
        """
        from datetime import timedelta
        new_date = self._fixed_date + timedelta(days=days)
        self.set_date(new_date)


# Global date provider instance
_date_provider: DateProvider = UTCDateProvider()


def get_date_provider() -> DateProvider:
    """Get the current date provider instance.
    
    Returns:
        DateProvider: Current date provider (production or test)
    """
    return _date_provider


def set_date_provider(provider: DateProvider) -> None:
    """Set the date provider instance (mainly for testing).
    
    Args:
        provider: Date provider implementation to use
    """
    global _date_provider
    _date_provider = provider


def reset_date_provider() -> None:
    """Reset to the default production date provider."""
    global _date_provider
    _date_provider = UTCDateProvider()