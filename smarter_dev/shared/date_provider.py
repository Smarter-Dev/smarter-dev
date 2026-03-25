"""Date provider abstraction for testable date operations.

This module provides an abstraction layer for date operations to enable
dependency injection and comprehensive testing. The "today" calculation
uses a configurable timezone (defaulting to UTC) so that quest dates
align with the operator's local calendar day.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


class DateProvider(ABC):
    """Abstract interface for date operations.

    This interface follows the Dependency Inversion Principle, allowing
    the application to depend on abstractions rather than concrete
    implementations. This enables easy testing and future flexibility.
    """

    @abstractmethod
    def today(self) -> date:
        """Get the current date in the configured timezone.

        Returns:
            date: Current date in the configured timezone
        """
        pass

    @abstractmethod
    def utcnow(self) -> datetime:
        """Get the current datetime in UTC.

        Returns:
            datetime: Current UTC datetime with timezone info
        """
        pass


class TZDateProvider(DateProvider):
    """Production implementation of DateProvider using a configurable timezone.

    "today" is calculated in the configured timezone so that quest active
    dates match the operator's local calendar day.  All absolute timestamps
    (utcnow) remain in UTC.
    """

    def __init__(self, tz_name: str = "UTC"):
        self._tz = ZoneInfo(tz_name)

    @property
    def tz(self) -> ZoneInfo:
        """The configured timezone."""
        return self._tz

    def today(self) -> date:
        """Get the current date in the configured timezone.

        Returns:
            date: Current date in the configured timezone
        """
        return datetime.now(self._tz).date()

    def utcnow(self) -> datetime:
        """Get the current datetime in UTC.

        Returns:
            datetime: Current UTC datetime with timezone info
        """
        return datetime.now(timezone.utc)


# Keep the old name as an alias for backwards compat during transition
UTCDateProvider = TZDateProvider


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
        new_date = self._fixed_date + timedelta(days=days)
        self.set_date(new_date)


# Global date provider instance — lazily configured from settings on first access
_date_provider: DateProvider | None = None
_configured: bool = False


def get_date_provider() -> DateProvider:
    """Get the current date provider instance.

    On first call, configures from application settings automatically.

    Returns:
        DateProvider: Current date provider (production or test)
    """
    global _date_provider, _configured
    if _date_provider is None and not _configured:
        configure_from_settings()
    assert _date_provider is not None
    return _date_provider


def set_date_provider(provider: DateProvider) -> None:
    """Set the date provider instance (mainly for testing).

    Args:
        provider: Date provider implementation to use
    """
    global _date_provider, _configured
    _date_provider = provider
    _configured = True


def configure_from_settings() -> None:
    """Configure the global date provider from application settings.

    Reads ``quest_timezone`` from the shared config and installs a
    :class:`TZDateProvider` with that timezone.  Safe to call multiple times.
    """
    global _date_provider, _configured
    _configured = True
    try:
        from smarter_dev.shared.config import get_settings
        settings = get_settings()
        _date_provider = TZDateProvider(settings.quest_timezone)
    except Exception:
        # Fallback to UTC if settings aren't available (e.g. during testing)
        _date_provider = TZDateProvider()


def reset_date_provider() -> None:
    """Reset to the default production date provider (UTC)."""
    global _date_provider, _configured
    _date_provider = TZDateProvider()
    _configured = True
