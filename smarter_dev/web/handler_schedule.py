"""Time-trigger scheduling for handlers (pure, testable functions).

Timers fire once; schedules recur. To keep a toy simple and dependency-free we
support a small, explicit settings vocabulary rather than full cron:

- timer:    ``{"delay_seconds": int}``  or ``{"fire_at": "<ISO-8601 UTC>"}``
- schedule: ``{"interval_seconds": int}`` or ``{"daily_time": "HH:MM"}`` (UTC),
  optionally with ``{"start_at": "<ISO-8601 UTC>"}``

The chatbot passes timing in ``settings`` for time triggers; the author/runtime
translate it through here. All functions take ``now`` explicitly so they can be
tested deterministically.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

# Min-interval floors — a recurring handler may not fire more often than this,
# and tighter when it spawns an agent (each fire is far more expensive).
MIN_INTERVAL_SECONDS = 60
MIN_INTERVAL_WITH_AGENT_SECONDS = 300

# Bounds on a script-armed one-shot timer (schedule_timer). The floor matches the
# recurring interval floor; the ceiling caps how far out an orphaned timer can
# linger before it drains to a no-op (a deleted handler's refire returns
# "missing"). Out-of-bounds is an author bug — rejected, not clamped.
MIN_TIMER_DELAY_SECONDS = 60
MAX_TIMER_DELAY_SECONDS = 30 * 86400


class ScheduleError(ValueError):
    """Raised when time-trigger settings are malformed or below the floor."""


def _parse_iso(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_start_at(value: object) -> datetime:
    """Parse an explicit UTC schedule anchor.

    Unlike the legacy timer ``fire_at`` parser, ``start_at`` never guesses a
    timezone: authoring agents are given the host UTC clock and must emit an
    explicit ``Z``/``+00:00`` timestamp.
    """
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ScheduleError("start_at must be an ISO-8601 UTC timestamp") from exc
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ScheduleError("start_at must include an explicit UTC offset")
    return dt.astimezone(UTC)


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


def validate_timer_delay(delay_seconds: int) -> int:
    """Return ``delay_seconds`` as an int if within timer bounds, else raise.

    Pure: no clock, no side effects. A delay below the floor or above the ceiling
    is an author bug — rejected loudly (never silently clamped), exactly like the
    recurring interval floor.
    """
    delay = int(delay_seconds)
    if delay < MIN_TIMER_DELAY_SECONDS:
        raise ScheduleError(
            f"timer delay_seconds must be at least {MIN_TIMER_DELAY_SECONDS}"
        )
    if delay > MAX_TIMER_DELAY_SECONDS:
        raise ScheduleError(
            f"timer delay_seconds must be at most {MAX_TIMER_DELAY_SECONDS}"
        )
    return delay


def _daily_parts(daily_time: object) -> tuple[int, int]:
    value = str(daily_time)
    parts = value.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
        raise ScheduleError("daily_time must be HH:MM UTC")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ScheduleError("daily_time must be HH:MM UTC")
    return hour, minute


def _daily_next(
    daily_time: object, now: datetime, start_at: datetime | None = None
) -> datetime:
    hour, minute = _daily_parts(daily_time)
    boundary = max(now, start_at) if start_at is not None else now
    target = boundary.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now or (start_at is not None and target < start_at):
        target += timedelta(days=1)
    return target


def _interval_next(
    interval_seconds: int | str, now: datetime, start_at: datetime
) -> datetime:
    interval = int(interval_seconds)
    if start_at > now:
        return start_at
    elapsed = (now - start_at).total_seconds()
    steps = int(elapsed // interval) + 1
    return start_at + timedelta(seconds=steps * interval)


def _schedule_start(settings: dict) -> datetime | None:
    if "start_at" not in settings:
        return None
    return _parse_start_at(settings["start_at"])


def validate_time_trigger_settings(
    trigger_type: str, settings: dict, *, uses_agent: bool
) -> None:
    """Validate a timer/schedule settings object before it is persisted."""
    if trigger_type == "timer":
        if "start_at" in settings:
            raise ScheduleError("start_at is only valid on recurring schedules")
        present = [key for key in ("delay_seconds", "fire_at") if key in settings]
        if len(present) != 1:
            raise ScheduleError(
                "timer requires exactly one of delay_seconds or fire_at"
            )
        if present[0] == "delay_seconds":
            try:
                int(settings["delay_seconds"])
            except (TypeError, ValueError) as exc:
                raise ScheduleError("delay_seconds must be an integer") from exc
        else:
            _parse_iso(str(settings["fire_at"]))
        return

    if trigger_type != "schedule":
        raise ScheduleError(f"{trigger_type} is not a time trigger")

    present = [key for key in ("interval_seconds", "daily_time") if key in settings]
    if len(present) != 1:
        raise ScheduleError(
            "schedule requires exactly one of interval_seconds or daily_time"
        )
    if present[0] == "interval_seconds":
        try:
            int(settings["interval_seconds"])
        except (TypeError, ValueError) as exc:
            raise ScheduleError("interval_seconds must be an integer") from exc
        validate_interval(settings, uses_agent=uses_agent)
    else:
        _daily_parts(settings["daily_time"])
    _schedule_start(settings)


def first_fire_at(trigger_type: str, settings: dict, now: datetime) -> datetime:
    """Compute the first (or only) fire instant for a time trigger."""
    now = now.astimezone(UTC)
    if trigger_type == "timer":
        if "delay_seconds" in settings:
            return now + timedelta(seconds=int(settings["delay_seconds"]))
        if "fire_at" in settings:
            return _parse_iso(str(settings["fire_at"]))
        raise ScheduleError("timer requires delay_seconds or fire_at")
    if trigger_type == "schedule":
        start_at = _schedule_start(settings)
        if "interval_seconds" in settings:
            if start_at is not None:
                return _interval_next(settings["interval_seconds"], now, start_at)
            return now + timedelta(seconds=int(settings["interval_seconds"]))
        if "daily_time" in settings:
            return _daily_next(settings["daily_time"], now, start_at)
        raise ScheduleError("schedule requires interval_seconds or daily_time")
    raise ScheduleError(f"{trigger_type} is not a time trigger")


def next_fire_at(settings: dict, now: datetime) -> datetime | None:
    """Compute the next fire for a recurring schedule, or None if not recurring."""
    now = now.astimezone(UTC)
    start_at = _schedule_start(settings)
    if "interval_seconds" in settings:
        if start_at is not None:
            return _interval_next(settings["interval_seconds"], now, start_at)
        return now + timedelta(seconds=int(settings["interval_seconds"]))
    if "daily_time" in settings:
        return _daily_next(settings["daily_time"], now, start_at)
    return None
