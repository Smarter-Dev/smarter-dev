"""Time-trigger scheduling for handlers (pure, testable functions).

Timers fire once; schedules recur. To keep a toy simple and dependency-free we
support a small, explicit settings vocabulary rather than full cron:

- timer:    ``{"delay_seconds": int}``  or ``{"fire_at": "<ISO-8601 UTC>"}``
- schedule: ``{"interval_seconds": int}``  or ``{"daily_time": "HH:MM"}`` (UTC)

The chatbot passes timing in ``settings`` for time triggers; the author/runtime
translate it through here. All functions take ``now`` explicitly so they can be
tested deterministically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Min-interval floors — a recurring handler may not fire more often than this,
# and tighter when it spawns an agent (each fire is far more expensive).
MIN_INTERVAL_SECONDS = 60
MIN_INTERVAL_WITH_AGENT_SECONDS = 300


class ScheduleError(ValueError):
    """Raised when time-trigger settings are malformed or below the floor."""


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_interval(settings: dict, *, uses_agent: bool) -> None:
    """Raise ScheduleError if a recurring interval is below the floor."""
    interval = settings.get("interval_seconds")
    if interval is None:
        return
    floor = MIN_INTERVAL_WITH_AGENT_SECONDS if uses_agent else MIN_INTERVAL_SECONDS
    if int(interval) < floor:
        raise ScheduleError(
            f"interval_seconds must be at least {floor}"
            + (" because this handler spawns an agent" if uses_agent else "")
        )


def _daily_next(daily_time: str, now: datetime) -> datetime:
    hour, _, minute = daily_time.partition(":")
    target = now.replace(
        hour=int(hour), minute=int(minute), second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=1)
    return target


def first_fire_at(trigger_type: str, settings: dict, now: datetime) -> datetime:
    """Compute the first (or only) fire instant for a time trigger."""
    if trigger_type == "timer":
        if "delay_seconds" in settings:
            return now + timedelta(seconds=int(settings["delay_seconds"]))
        if "fire_at" in settings:
            return _parse_iso(str(settings["fire_at"]))
        raise ScheduleError("timer requires delay_seconds or fire_at")
    if trigger_type == "schedule":
        if "interval_seconds" in settings:
            return now + timedelta(seconds=int(settings["interval_seconds"]))
        if "daily_time" in settings:
            return _daily_next(str(settings["daily_time"]), now)
        raise ScheduleError("schedule requires interval_seconds or daily_time")
    raise ScheduleError(f"{trigger_type} is not a time trigger")


def next_fire_at(settings: dict, now: datetime) -> datetime | None:
    """Compute the next fire for a recurring schedule, or None if not recurring."""
    if "interval_seconds" in settings:
        return now + timedelta(seconds=int(settings["interval_seconds"]))
    if "daily_time" in settings:
        return _daily_next(str(settings["daily_time"]), now)
    return None
