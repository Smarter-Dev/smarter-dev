"""Tests for time-trigger scheduling (pure functions)."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from smarter_dev.web.handler_schedule import MAX_TIMER_DELAY_SECONDS
from smarter_dev.web.handler_schedule import MIN_INTERVAL_SECONDS
from smarter_dev.web.handler_schedule import MIN_INTERVAL_WITH_AGENT_SECONDS
from smarter_dev.web.handler_schedule import MIN_TIMER_DELAY_SECONDS
from smarter_dev.web.handler_schedule import ScheduleError
from smarter_dev.web.handler_schedule import first_fire_at
from smarter_dev.web.handler_schedule import next_fire_at
from smarter_dev.web.handler_schedule import validate_interval
from smarter_dev.web.handler_schedule import validate_time_trigger_settings
from smarter_dev.web.handler_schedule import validate_timer_delay

NOW = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)


def test_timer_delay_seconds():
    assert first_fire_at("timer", {"delay_seconds": 3600}, NOW) == NOW + timedelta(
        hours=1
    )


def test_timer_fire_at_iso():
    fire = first_fire_at("timer", {"fire_at": "2026-06-27T08:00:00+00:00"}, NOW)
    assert fire == datetime(2026, 6, 27, 8, 0, tzinfo=UTC)


def test_timer_requires_timing():
    with pytest.raises(ScheduleError):
        first_fire_at("timer", {}, NOW)


def test_schedule_interval_first_and_next():
    assert first_fire_at("schedule", {"interval_seconds": 300}, NOW) == NOW + timedelta(
        seconds=300
    )
    assert next_fire_at({"interval_seconds": 300}, NOW) == NOW + timedelta(seconds=300)


def test_interval_schedule_starts_at_future_utc_anchor():
    settings = {
        "interval_seconds": 3600,
        "start_at": "2026-06-26T10:15:00Z",
    }
    assert first_fire_at("schedule", settings, NOW) == datetime(
        2026, 6, 26, 10, 15, tzinfo=UTC
    )


def test_interval_schedule_keeps_alignment_after_anchor_passes():
    settings = {
        "interval_seconds": 3600,
        "start_at": "2026-06-26T08:00:00+00:00",
    }
    now = datetime(2026, 6, 26, 9, 17, tzinfo=UTC)
    expected = datetime(2026, 6, 26, 10, 0, tzinfo=UTC)
    assert first_fire_at("schedule", settings, now) == expected
    assert next_fire_at(settings, now) == expected


def test_daily_schedule_start_at_is_a_lower_bound():
    settings = {
        "daily_time": "10:00",
        "start_at": "2026-06-27T12:00:00Z",
    }
    assert first_fire_at("schedule", settings, NOW) == datetime(
        2026, 6, 28, 10, 0, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "start_at",
    ["2026-06-27T10:00:00", "2026-06-27T10:00:00-04:00", "not-a-time"],
)
def test_schedule_start_at_requires_explicit_utc(start_at):
    with pytest.raises(ScheduleError):
        validate_time_trigger_settings(
            "schedule",
            {"interval_seconds": 300, "start_at": start_at},
            uses_agent=False,
        )


def test_start_at_is_rejected_on_timer():
    with pytest.raises(ScheduleError, match="only valid on recurring schedules"):
        validate_time_trigger_settings(
            "timer",
            {"delay_seconds": 300, "start_at": "2026-06-27T10:00:00Z"},
            uses_agent=False,
        )


def test_schedule_daily_time_rolls_to_tomorrow_when_past():
    # 08:00 is before 09:00 now → next is tomorrow 08:00.
    fire = first_fire_at("schedule", {"daily_time": "08:00"}, NOW)
    assert fire == datetime(2026, 6, 27, 8, 0, tzinfo=UTC)


def test_schedule_daily_time_today_when_future():
    fire = first_fire_at("schedule", {"daily_time": "10:30"}, NOW)
    assert fire == datetime(2026, 6, 26, 10, 30, tzinfo=UTC)


def test_interval_floor_enforced():
    validate_interval({"interval_seconds": MIN_INTERVAL_SECONDS}, uses_agent=False)
    with pytest.raises(ScheduleError):
        validate_interval(
            {"interval_seconds": MIN_INTERVAL_SECONDS - 1}, uses_agent=False
        )


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


# -- script-armed timer delay bounds (schedule_timer, E3) ----------------------


def test_validate_timer_delay_accepts_bounds():
    assert validate_timer_delay(MIN_TIMER_DELAY_SECONDS) == MIN_TIMER_DELAY_SECONDS
    assert validate_timer_delay(MAX_TIMER_DELAY_SECONDS) == MAX_TIMER_DELAY_SECONDS
    assert MIN_TIMER_DELAY_SECONDS == 60
    assert MAX_TIMER_DELAY_SECONDS == 30 * 86400


def test_validate_timer_delay_below_floor_raises():
    with pytest.raises(ScheduleError):
        validate_timer_delay(MIN_TIMER_DELAY_SECONDS - 1)


def test_validate_timer_delay_above_ceiling_raises():
    with pytest.raises(ScheduleError):
        validate_timer_delay(MAX_TIMER_DELAY_SECONDS + 1)
