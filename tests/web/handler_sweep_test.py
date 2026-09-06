"""Stalled-schedule detection.

The sweep's whole job is deciding, from the handler row and its run history,
whether a recurring chain has stopped. Both directions matter and they are not
symmetric: failing to revive a dead chain costs a silently missing feature,
while reviving a merely-late chain forks a second perpetual chain for the same
handler — strictly worse. So the tests below lean hard on the "don't touch a
late-but-alive chain" side.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import smarter_dev.web.handler_recurrence as handler_recurrence
import smarter_dev.web.handler_sweep as handler_sweep
from smarter_dev.web.handler_fire_payloads import AdminHandlerFirePayload
from smarter_dev.web.handler_fire_payloads import HandlerFirePayload
from smarter_dev.web.handler_sweep import (
    DAILY_PERIOD_SECONDS,
    MIN_GRACE_SECONDS,
    STALE_PERIOD_MULTIPLIER,
    StalledChain,
    grace_seconds,
    is_stalled,
    rearm_chain,
    schedule_period_seconds,
)
from smarter_dev.web.models import AdminHandler, ChannelHandler, HandlerRun

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
CREATED = NOW - timedelta(days=30)

# The real Discord.me reminder that died on 2026-07-30.
SIX_HOURLY = {"start_at": "2026-07-25T17:59:30Z", "interval_seconds": 21600}
DAILY = {"daily_time": "01:00"}


# -- period + grace ------------------------------------------------------------


def test_interval_period_is_the_interval():
    assert schedule_period_seconds(SIX_HOURLY) == 21600


def test_daily_period_is_a_day():
    assert schedule_period_seconds(DAILY) == DAILY_PERIOD_SECONDS


def test_non_recurring_settings_have_no_period():
    assert schedule_period_seconds({}) is None
    assert schedule_period_seconds({"delay_seconds": 60}) is None


def test_unparseable_interval_has_no_period():
    """Garbage settings must not crash the sweep — they just aren't sweepable."""
    assert schedule_period_seconds({"interval_seconds": "soon"}) is None


def test_grace_scales_with_the_period():
    assert grace_seconds(21600) == 21600 * STALE_PERIOD_MULTIPLIER


def test_grace_never_drops_below_the_floor():
    """A 60s schedule must not be re-armed over three minutes of queue backlog."""
    assert grace_seconds(60) == MIN_GRACE_SECONDS


# -- healthy chains are left alone --------------------------------------------


def test_a_chain_that_just_fired_is_healthy():
    assert is_stalled(SIX_HOURLY, NOW - timedelta(minutes=5), CREATED, NOW) is None


def test_a_chain_one_period_behind_is_still_healthy():
    """Ordinary drift: a fire ran long, the worker was busy, a pod rolled."""
    assert is_stalled(SIX_HOURLY, NOW - timedelta(hours=6), CREATED, NOW) is None


def test_a_chain_inside_the_grace_window_is_still_healthy():
    # period 6h + grace 6h = 12h of silence tolerated (one whole missed fire).
    assert is_stalled(SIX_HOURLY, NOW - timedelta(hours=11), CREATED, NOW) is None


def test_a_non_recurring_handler_is_never_stalled():
    assert is_stalled({}, NOW - timedelta(days=365), CREATED, NOW) is None


# -- broken chains are caught --------------------------------------------------


def test_a_chain_past_the_grace_window_is_stalled():
    """Two consecutive missed fires — drift alone never gets here."""
    assert is_stalled(SIX_HOURLY, NOW - timedelta(hours=13), CREATED, NOW) is not None


def test_the_real_discord_me_outage_is_caught():
    """The actual incident: last fire 2026-07-30 17:59 UTC, still dead on 08-03."""
    last = datetime(2026, 7, 30, 17, 59, 30, tzinfo=timezone.utc)
    overdue = is_stalled(SIX_HOURLY, last, CREATED, NOW)
    assert overdue is not None
    assert overdue > timedelta(days=3)


def test_a_dead_daily_chain_is_caught():
    """A daily announcement must not sit dead for days before we notice."""
    assert is_stalled(DAILY, NOW - timedelta(days=3), CREATED, NOW) is not None


def test_a_daily_chain_one_day_late_is_not_yet_touched():
    """One missed daily fire is drift; two is broken."""
    assert is_stalled(DAILY, NOW - timedelta(days=1, hours=12), CREATED, NOW) is None


def test_a_handler_that_never_fired_falls_back_to_created_at():
    """An install whose very first job was lost still needs reviving."""
    never_fired = None
    fresh = NOW - timedelta(minutes=1)
    assert is_stalled(SIX_HOURLY, never_fired, fresh, NOW) is None

    old = NOW - timedelta(days=5)
    assert is_stalled(SIX_HOURLY, never_fired, old, NOW) is not None


def test_overdue_is_measured_against_the_expected_fire():
    """Reported lateness is silence minus one period — the miss, not the gap."""
    last = NOW - timedelta(hours=30)
    overdue = is_stalled(SIX_HOURLY, last, CREATED, NOW)
    assert overdue == timedelta(hours=24)


@pytest.mark.parametrize("hours", [0, 1, 6, 11])
def test_no_false_positives_across_the_healthy_range(hours):
    assert is_stalled(SIX_HOURLY, NOW - timedelta(hours=hours), CREATED, NOW) is None


# -- re-arming goes through the tier's chain and the audit owner --------------


class _Handle:
    cancelled: list[str] = []

    def __init__(self, job_id: str):
        self._job_id = job_id

    async def cancel(self):
        _Handle.cancelled.append(self._job_id)


def _patch_rearm(monkeypatch) -> list:
    submits: list = []

    async def fake_submit(payload, scheduled_for=None, job_id=None):
        submits.append((payload, scheduled_for, job_id))

    monkeypatch.setattr(handler_recurrence, "worker_submit", fake_submit)
    monkeypatch.setattr(handler_sweep, "get_handle", _Handle)
    _Handle.cancelled = []
    return submits


async def _seed(engine, model, **fields) -> object:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        record = model(**fields)
        session.add(record)
        await session.commit()
        return record


def _stalled(record, kind: str) -> StalledChain:
    return StalledChain(
        handler_id=record.id,
        kind=kind,
        name=record.name,
        settings=SIX_HOURLY,
        last_fired_at=NOW - timedelta(days=4),
        overdue_by=timedelta(days=3, hours=18),
    )


async def _rearm(engine, chain: StalledChain):
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        next_occurrence = await rearm_chain(session, chain, NOW)
        await session.commit()
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        runs = list(
            await session.scalars(
                select(HandlerRun).where(HandlerRun.handler_id == chain.handler_id)
            )
        )
    return next_occurrence, runs


async def test_a_stalled_standard_chain_is_re_armed_through_its_tier_chain(
    monkeypatch, test_engine
):
    submits = _patch_rearm(monkeypatch)
    record = await _seed(
        test_engine,
        ChannelHandler,
        id=uuid4(),
        guild_id="G1",
        channel_id="C1",
        name="six-hourly",
        trigger_type="schedule",
        settings=SIX_HOURLY,
        description="d",
        script="pass\n",
        created_by="U1",
        scheduled_job_id="dead-job",
    )

    next_occurrence, runs = await _rearm(test_engine, _stalled(record, "standard"))

    payload, scheduled_for, job_id = submits[0]
    assert isinstance(payload, HandlerFirePayload)
    assert payload.handler_id == str(record.id)
    assert payload.trigger_context == {"trigger_type": "schedule"}
    assert scheduled_for == next_occurrence
    assert _Handle.cancelled == ["dead-job"]
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        assert (await session.get(ChannelHandler, record.id)).scheduled_job_id == job_id
    assert len(runs) == 1
    assert runs[0].outcome == "rearmed"
    assert runs[0].handler_kind == "standard"
    assert runs[0].trigger_context == {"trigger_type": "sweep"}
    assert "re-armed for " + next_occurrence.isoformat() in runs[0].error


async def test_a_stalled_admin_chain_is_re_armed_with_the_admin_payload(
    monkeypatch, test_engine
):
    submits = _patch_rearm(monkeypatch)
    record = await _seed(
        test_engine,
        AdminHandler,
        id=uuid4(),
        guild_id="G1",
        name="six-hourly-admin",
        trigger_type="schedule",
        settings=SIX_HOURLY,
        channel_ids=[],
        description="d",
        script="pass\n",
        created_by_admin="A1",
    )

    _, runs = await _rearm(test_engine, _stalled(record, "admin"))

    payload, _, _ = submits[0]
    assert isinstance(payload, AdminHandlerFirePayload)
    assert payload.admin_handler_id == str(record.id)
    assert _Handle.cancelled == []
    assert runs[0].handler_kind == "admin"


async def test_a_disabled_handler_is_not_re_armed(monkeypatch, test_engine):
    submits = _patch_rearm(monkeypatch)
    record = await _seed(
        test_engine,
        ChannelHandler,
        id=uuid4(),
        guild_id="G1",
        channel_id="C1",
        name="off",
        trigger_type="schedule",
        settings=SIX_HOURLY,
        description="d",
        script="pass\n",
        created_by="U1",
        enabled=False,
    )
    next_occurrence, runs = await _rearm(test_engine, _stalled(record, "standard"))
    assert next_occurrence is None
    assert submits == []
    assert runs == []
