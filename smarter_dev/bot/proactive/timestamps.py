"""UTC timestamps for proactive-agent messages and notifications."""

from datetime import UTC
from datetime import datetime


def as_utc(value: datetime) -> datetime:
    """Treat legacy naive timestamps as UTC, never as the host's timezone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_timestamp(value: datetime) -> str:
    """Render an ISO 8601 timestamp with an explicit UTC designator."""
    return as_utc(value).isoformat().replace("+00:00", "Z")
