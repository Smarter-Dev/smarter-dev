"""Tests for time-trigger scheduling (pure functions)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from smarter_dev.web.handler_schedule import (
    MIN_INTERVAL_SECONDS,
    MIN_INTERVAL_WITH_AGENT_SECONDS,
    ScheduleError,
    first_fire_at,
    next_fire_at,
    validate_interval,
)

NOW = datetime(2026, 6, 26, 9, 0, tzinfo=timezone.utc)


def test_timer_delay_seconds():
    assert first_fire_at("timer", {"delay_seconds": 3600}, NOW) == NOW + timedelta(hours=1)


def test_timer_fire_at_iso():
    fire = first_fire_at("timer", {"fire_at": "2026-06-27T08:00:00+00:00"}, NOW)
    assert fire == datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)


def test_timer_requires_timing():
    with pytest.raises(ScheduleError):
        first_fire_at("timer", {}, NOW)


def test_schedule_interval_first_and_next():
    assert first_fire_at("schedule", {"interval_seconds": 300}, NOW) == NOW + timedelta(seconds=300)
    assert next_fire_at({"interval_seconds": 300}, NOW) == NOW + timedelta(seconds=300)


def test_schedule_daily_time_rolls_to_tomorrow_when_past():
    # 08:00 is before 09:00 now → next is tomorrow 08:00.
    fire = first_fire_at("schedule", {"daily_time": "08:00"}, NOW)
    assert fire == datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)


def test_schedule_daily_time_today_when_future():
    fire = first_fire_at("schedule", {"daily_time": "10:30"}, NOW)
    assert fire == datetime(2026, 6, 26, 10, 30, tzinfo=timezone.utc)


def test_interval_floor_enforced():
    validate_interval({"interval_seconds": MIN_INTERVAL_SECONDS}, uses_agent=False)
    with pytest.raises(ScheduleError):
        validate_interval({"interval_seconds": MIN_INTERVAL_SECONDS - 1}, uses_agent=False)


def test_interval_floor_tighter_with_agent():
    with pytest.raises(ScheduleError):
        validate_interval(
            {"interval_seconds": MIN_INTERVAL_WITH_AGENT_SECONDS - 1}, uses_agent=True
        )
    validate_interval(
        {"interval_seconds": MIN_INTERVAL_WITH_AGENT_SECONDS}, uses_agent=True
    )


def test_non_recurring_settings_have_no_next():
    assert next_fire_at({"delay_seconds": 60}, NOW) is None
