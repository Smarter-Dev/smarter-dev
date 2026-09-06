"""Revive recurring handler schedules whose job chain has broken.

A recurring schedule has no scheduler behind it. It exists at runtime as a
single in-flight worker job, and the ONLY thing that enqueues the next
occurrence is the successful completion of the current one
(``handler_recurrence.RecurringFireChain``). That makes the chain a linked list
with no head pointer: break one link — a dead-lettered job, an evicted pod, a
deploy that makes every fire raise — and the schedule stops forever, silently,
even though the handler row is still ``enabled`` and still carries everything
needed to compute the next fire.

This module supplies the missing head pointer. The handler row is the source of
truth; the queued job is only a cache of the next occurrence. A sweep asks one
question per enabled schedule handler — *has this fired recently enough for its
own cadence?* — and re-arms the ones that haven't.

Deliberately NOT built on worker job-state lookup. Terminal job state expires
(``terminal_job_state_ttl``, 7 days by default), and the state store is Redis
under the distributed preset, so "no state for this job id" is ambiguous: it
means either "the chain is broken" or "the chain is fine and the record is old".
Answering from ``handler_runs`` instead is unambiguous, survives a Redis flush,
and is the same signal a human would use to decide the schedule is stuck.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from skrift.workers import get_handle
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.handler_recurrence import RECURRING_CHAINS
from smarter_dev.web.handler_run_audit import record_rearmed_run
from smarter_dev.web.handler_schedule import ScheduleError, next_fire_at
from smarter_dev.web.models import HandlerRun

logger = logging.getLogger(__name__)

# Grace beyond one missed fire before a chain counts as broken. A schedule
# legitimately drifts — a fire takes 20s, the worker is busy, a pod rolls — and
# re-arming a chain that is merely late risks a second perpetual chain for one
# handler, which is worse than a late fire. But one whole extra period of
# silence is already abnormal: at 1x a chain is declared broken only after
# missing TWO consecutive fires, which drift alone doesn't produce.
#
# Deliberately not higher. A larger multiple reads as "safer" but scales with
# the period, so it punishes exactly the schedules that can least afford it —
# at 3x, a daily announcement would sit dead for four days before anyone
# noticed, which is the outage this sweep exists to end.
#
# The fork risk this guards against is further covered in rearm_chain, which
# cancels the stale job id before submitting a replacement.
STALE_PERIOD_MULTIPLIER = 1
# Absolute floor on the grace period, so tight schedules (the 60s interval
# minimum) aren't re-armed over a few minutes of ordinary queue backlog.
MIN_GRACE_SECONDS = 15 * 60
# A daily_time schedule's nominal period.
DAILY_PERIOD_SECONDS = 86400


@dataclass(frozen=True)
class StalledChain:
    """One enabled schedule handler whose chain has stopped firing."""

    handler_id: UUID
    kind: str  # "standard" | "admin"
    name: str
    settings: dict
    last_fired_at: datetime | None
    overdue_by: timedelta

    @property
    def label(self) -> str:
        return f"{self.name} ({self.kind}, {self.handler_id})"


def schedule_period_seconds(settings: dict) -> int | None:
    """The nominal period of a recurring schedule, or None if not recurring."""
    if "interval_seconds" in settings:
        try:
            return int(settings["interval_seconds"])
        except (TypeError, ValueError):
            return None
    if "daily_time" in settings:
        return DAILY_PERIOD_SECONDS
    return None


def grace_seconds(period_seconds: int) -> int:
    """How long past a missed fire before the chain counts as broken."""
    return max(period_seconds * STALE_PERIOD_MULTIPLIER, MIN_GRACE_SECONDS)


def is_stalled(
    settings: dict,
    last_fired_at: datetime | None,
    created_at: datetime,
    now: datetime,
) -> timedelta | None:
    """Return how overdue a chain is, or None if it's healthy / not recurring.

    ``created_at`` is the fallback reference point for a handler that has never
    fired at all — an install whose very first job was lost still needs reviving.
    """
    period = schedule_period_seconds(settings)
    if period is None or period <= 0:
        return None
    reference = last_fired_at or created_at
    silent_for = now - reference
    allowed = timedelta(seconds=period + grace_seconds(period))
    if silent_for <= allowed:
        return None
    return silent_for - timedelta(seconds=period)


async def _last_fire_times(
    session: AsyncSession, handler_ids: list[UUID]
) -> dict[UUID, datetime]:
    """Most recent fire per handler, counting only real fires.

    ``rearmed`` rows are written by this sweep itself, so counting them would
    make a chain look alive purely because we keep re-arming it — the sweep
    would heal a handler once, then never notice it never actually fired again.
    """
    if not handler_ids:
        return {}
    rows = await session.execute(
        select(HandlerRun.handler_id, func.max(HandlerRun.fired_at))
        .where(
            HandlerRun.handler_id.in_(handler_ids),
            HandlerRun.outcome != "rearmed",
        )
        .group_by(HandlerRun.handler_id)
    )
    return dict(rows.all())


async def find_stalled_chains(
    session: AsyncSession, now: datetime | None = None
) -> list[StalledChain]:
    """Every enabled recurring schedule (every tier) that has stopped firing."""
    now = now or datetime.now(timezone.utc)

    scheduled: list[tuple[object, str]] = []
    for kind, tier_chain in RECURRING_CHAINS.items():
        for record in await tier_chain.load_enabled_schedule_handlers(session):
            scheduled.append((record, kind))

    last_fired = await _last_fire_times(session, [record.id for record, _ in scheduled])

    stalled: list[StalledChain] = []
    for record, kind in scheduled:
        settings = dict(record.settings or {})
        overdue = is_stalled(
            settings, last_fired.get(record.id), record.created_at, now
        )
        if overdue is None:
            continue
        stalled.append(
            StalledChain(
                handler_id=record.id,
                kind=kind,
                name=record.name or str(record.id),
                settings=settings,
                last_fired_at=last_fired.get(record.id),
                overdue_by=overdue,
            )
        )
    return stalled


async def rearm_chain(
    session: AsyncSession,
    chain: StalledChain,
    now: datetime | None = None,
) -> datetime | None:
    """Enqueue a fresh next occurrence for a stalled chain and record it.

    Returns the instant the chain will next fire, or None if it couldn't be
    re-armed. Cancels the stale job id first: it is almost certainly dead
    already, but if it somehow isn't, letting it run would leave two live chains
    for one handler — the one outcome worse than a stalled schedule.
    """
    now = now or datetime.now(timezone.utc)
    tier_chain = RECURRING_CHAINS[chain.kind]
    record = await tier_chain.load_enabled_handler(session, chain.handler_id)
    if record is None:
        return None

    try:
        nxt = next_fire_at(chain.settings, now)
    except ScheduleError:
        logger.exception("cannot re-arm %s: unusable settings", chain.label)
        return None
    if nxt is None:
        return None

    if record.scheduled_job_id:
        try:
            await get_handle(record.scheduled_job_id).cancel()
        except Exception:  # noqa: BLE001 — best-effort; it is normally long dead
            logger.debug("stale job %s not cancellable", record.scheduled_job_id)

    await tier_chain.arm_occurrence(record, nxt)
    record_rearmed_run(
        session,
        handler_id=chain.handler_id,
        handler_kind=chain.kind,
        last_fired_at=chain.last_fired_at,
        overdue_by=chain.overdue_by,
        next_occurrence=nxt,
        now=now,
    )
    return nxt


async def sweep_schedule_chains(
    session: AsyncSession, now: datetime | None = None
) -> dict:
    """Find every stalled recurring schedule and re-arm it. Returns a summary."""
    now = now or datetime.now(timezone.utc)
    stalled = await find_stalled_chains(session, now)
    rearmed: list[str] = []
    failed: list[str] = []

    for chain in stalled:
        try:
            nxt = await rearm_chain(session, chain, now)
        except Exception:  # noqa: BLE001 — one bad handler mustn't stop the sweep
            logger.exception("failed to re-arm %s", chain.label)
            failed.append(chain.label)
            continue
        if nxt is None:
            failed.append(chain.label)
            continue
        logger.warning(
            "re-armed stalled schedule %s: last fired %s, overdue by %s, next fire %s",
            chain.label,
            chain.last_fired_at,
            chain.overdue_by,
            nxt,
        )
        rearmed.append(chain.label)

    await session.commit()
    return {"stalled": len(stalled), "rearmed": rearmed, "failed": failed}
