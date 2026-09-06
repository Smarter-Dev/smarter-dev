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


# -- the recurring chain a scheduled fire keeps alive --------------------------

from uuid import uuid4  # noqa: E402

import smarter_dev.web.handler_schedule as handler_schedule  # noqa: E402
from smarter_dev.web.handler_schedule import RecurringFireChain  # noqa: E402

RECURRING_SETTINGS = {"interval_seconds": 300, "start_at": "2099-01-01T00:00:00Z"}


class _HandlerModel:
    """Stand-in for the tier's ORM model — only its identity is used."""


class _Record:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.scheduled_job_id = None


class _Session:
    def __init__(self, record):
        self._record = record
        self.committed = False
        self.asked_for = None

    async def get(self, model, id_):
        self.asked_for = (model, id_)
        return self._record

    async def commit(self):
        self.committed = True


class _SessionCtx:
    def __init__(self, record):
        self.session = _Session(record)

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def _chain_under_test(monkeypatch, record) -> tuple[RecurringFireChain, list, _SessionCtx]:
    submits: list = []

    async def fake_submit(payload, scheduled_for=None, job_id=None):
        submits.append((payload, scheduled_for, job_id))

    session_ctx = _SessionCtx(record)
    monkeypatch.setattr(handler_schedule, "worker_submit", fake_submit)
    monkeypatch.setattr(handler_schedule, "get_db_session_context", session_ctx)
    chain = RecurringFireChain(
        handler_model=_HandlerModel,
        build_fire_payload=lambda handler_id: {"handler_id": handler_id},
    )
    return chain, submits, session_ctx


@pytest.mark.parametrize(
    ("trigger_type", "trigger_context", "rearms"),
    [
        ("schedule", {"trigger_type": "schedule"}, True),
        ("schedule", {}, True),
        # A schedule handler that self-arms a timer must not fork a second
        # perpetual chain when the timer re-fires it.
        ("schedule", {"trigger_type": "timer"}, False),
        ("message", {"trigger_type": "message"}, False),
        ("timer", {"trigger_type": "timer"}, False),
    ],
)
async def test_only_a_genuine_scheduled_fire_re_arms(
    monkeypatch, trigger_type, trigger_context, rearms
):
    chain, submits, _ = _chain_under_test(monkeypatch, _Record())
    await chain.rearm_after_fire(
        handler_id=uuid4(),
        trigger_type=trigger_type,
        trigger_context=trigger_context,
        handler_settings=RECURRING_SETTINGS,
    )
    assert bool(submits) is rearms


async def test_a_re_arm_enqueues_the_tier_payload_at_the_next_occurrence(monkeypatch):
    chain, submits, session_ctx = _chain_under_test(monkeypatch, _Record())
    handler_id = uuid4()
    await chain.rearm_after_fire(
        handler_id=handler_id,
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings=RECURRING_SETTINGS,
    )
    payload, scheduled_for, job_id = submits[0]
    assert payload == {"handler_id": str(handler_id)}
    assert scheduled_for.isoformat() == "2099-01-01T00:00:00+00:00"
    # The enqueued job id is stamped on the tier's row so a later disable or
    # edit can cancel the occurrence this fire just armed.
    assert session_ctx.session.asked_for == (_HandlerModel, handler_id)
    assert session_ctx.session._record.scheduled_job_id == job_id
    assert session_ctx.session.committed is True


async def test_a_one_shot_schedule_ends_the_chain(monkeypatch):
    # Settings with no recurring key (next_fire_at returns None): nothing is
    # enqueued and the row is left alone.
    chain, submits, session_ctx = _chain_under_test(monkeypatch, _Record())
    await chain.rearm_after_fire(
        handler_id=uuid4(),
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings={},
    )
    assert submits == []
    assert session_ctx.session.asked_for is None


async def test_a_handler_disabled_mid_fire_is_not_stamped(monkeypatch):
    chain, submits, session_ctx = _chain_under_test(monkeypatch, _Record(enabled=False))
    await chain.rearm_after_fire(
        handler_id=uuid4(),
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings=RECURRING_SETTINGS,
    )
    assert len(submits) == 1
    assert session_ctx.session._record.scheduled_job_id is None
    assert session_ctx.session.committed is False


async def test_a_deleted_handler_is_not_stamped(monkeypatch):
    chain, submits, session_ctx = _chain_under_test(monkeypatch, None)
    await chain.rearm_after_fire(
        handler_id=uuid4(),
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings=RECURRING_SETTINGS,
    )
    assert len(submits) == 1
    assert session_ctx.session.committed is False
