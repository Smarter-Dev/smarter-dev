"""Tests for the hourly deletion of Skrift worker rows that belong to finished work."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from skrift.db.models.worker import WorkerArchiveEventRecord
from skrift.db.models.worker import WorkerArchiveSnapshotRecord
from skrift.db.models.worker import WorkerDeadLetterRecord
from skrift.db.models.worker import WorkerEventRecord
from skrift.db.models.worker import WorkerQueueRecord
from skrift.db.models.worker import WorkerStateRecord
from sqlalchemy import select

from smarter_dev.web.worker_retention import WORKER_RETENTION
from smarter_dev.web.worker_retention import delete_expired_worker_rows

_NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
_OLD = _NOW - WORKER_RETENTION - timedelta(days=30)
_RECENT = _NOW - timedelta(hours=1)
_BLOB = "sha256:" + "a" * 64
_OTHER_BLOB = "sha256:" + "b" * 64


def _queue_row(job_id: str, *, dead_lettered: bool, at: datetime):
    return WorkerQueueRecord(
        job_id=job_id,
        queue="agents",
        job={"id": job_id, "payload": {}},
        visible_at=at,
        dead_lettered=dead_lettered,
        created_at=at,
        updated_at=at,
    )


def _event(stream: str, position: int, at: datetime, job_id: str | None = None):
    return WorkerEventRecord(
        stream=stream, position=position, job_id=job_id, event={}, created_at=at
    )


def _runstate(status: str, *blob_ids: str) -> dict:
    return {"status": status, "messages": [{"blob": blob_id} for blob_id in blob_ids]}


async def _remaining(session, column) -> list:
    return sorted((await session.scalars(select(column))).all())


@pytest.mark.asyncio
async def test_state_goes_only_once_its_own_expiry_has_passed(db_session):
    db_session.add_all(
        [
            WorkerStateRecord(key="expired", value={}, expires_at=_NOW - timedelta(seconds=1)),
            WorkerStateRecord(key="unexpired", value={}, expires_at=_NOW + timedelta(days=1)),
            WorkerStateRecord(key="no-expiry", value={}, created_at=_OLD),
        ]
    )
    await db_session.commit()

    counts = await delete_expired_worker_rows(db_session, now=_NOW)

    assert counts["worker_state"] == 1
    assert await _remaining(db_session, WorkerStateRecord.key) == ["no-expiry", "unexpired"]


@pytest.mark.asyncio
async def test_old_dead_lettered_jobs_go_and_a_long_pending_timer_stays(db_session):
    db_session.add_all(
        [
            _queue_row("dead-old", dead_lettered=True, at=_OLD),
            _queue_row("dead-recent", dead_lettered=True, at=_RECENT),
            _queue_row("timer-due-next-month", dead_lettered=False, at=_OLD),
            WorkerDeadLetterRecord(
                entry_id="dlq-old",
                queue="agents",
                job_type="handlers.fire",
                cause="retries_exhausted",
                state="open",
                entry={},
                entry_created_at=_OLD,
                entry_updated_at=_OLD,
            ),
            WorkerDeadLetterRecord(
                entry_id="dlq-recent",
                queue="agents",
                job_type="handlers.fire",
                cause="retries_exhausted",
                state="open",
                entry={},
                entry_created_at=_RECENT,
                entry_updated_at=_RECENT,
            ),
        ]
    )
    await db_session.commit()

    await delete_expired_worker_rows(db_session, now=_NOW)

    assert await _remaining(db_session, WorkerQueueRecord.job_id) == [
        "dead-recent",
        "timer-due-next-month",
    ]
    assert await _remaining(db_session, WorkerDeadLetterRecord.entry_id) == ["dlq-recent"]


@pytest.mark.asyncio
async def test_a_long_lived_job_and_session_keep_their_rows_past_the_window(db_session):
    db_session.add_all(
        [
            # A job queued long ago and still waiting, and one whose state says
            # it is paused: both are live.
            _queue_row("queued-job", dead_lettered=False, at=_OLD),
            WorkerStateRecord(
                key="workers:jobs:paused-job", value={"status": "paused"}, created_at=_OLD
            ),
            WorkerStateRecord(
                key="workers:jobs:done-job",
                value={"status": "completed"},
                expires_at=_NOW + timedelta(days=1),
            ),
            _event("workers:lifecycle", 1, _OLD, job_id="queued-job"),
            _event("workers:lifecycle", 2, _OLD, job_id="paused-job"),
            _event("workers:lifecycle", 3, _OLD, job_id="done-job"),
            _event("workers:lifecycle", 4, _RECENT, job_id="done-job"),
            # A session paused for weeks whose hot copy expired: its newest
            # snapshot is the state, and it is still resumable.
            WorkerArchiveSnapshotRecord(
                key="runstate:paused", value=_runstate("running"), snapshot_at=_OLD - timedelta(days=1)
            ),
            WorkerArchiveSnapshotRecord(
                key="runstate:paused", value=_runstate("paused", _BLOB), snapshot_at=_OLD
            ),
            _event("agents:run:paused", 1, _OLD),
            WorkerArchiveEventRecord(stream=f"agents:blobs:{_BLOB}", position=0, event={}, created_at=_OLD),
            # A session that finished long ago.
            WorkerArchiveSnapshotRecord(
                key="runstate:finished", value=_runstate("completed", _OTHER_BLOB), snapshot_at=_OLD
            ),
            _event("agents:run:finished", 1, _OLD),
            WorkerArchiveEventRecord(
                stream=f"agents:blobs:{_OTHER_BLOB}", position=0, event={}, created_at=_OLD
            ),
            # A session running now whose hot copy outranks an old snapshot.
            WorkerStateRecord(
                key="runstate:running",
                value=_runstate("running"),
                expires_at=_NOW + timedelta(days=1),
            ),
            WorkerArchiveSnapshotRecord(
                key="runstate:running", value=_runstate("completed"), snapshot_at=_OLD
            ),
            _event("agents:run:running", 1, _OLD),
        ]
    )
    await db_session.commit()

    await delete_expired_worker_rows(db_session, now=_NOW)

    events = (
        await db_session.execute(select(WorkerEventRecord.stream, WorkerEventRecord.job_id))
    ).all()
    assert sorted(events, key=str) == sorted(
        [
            ("workers:lifecycle", "queued-job"),
            ("workers:lifecycle", "paused-job"),
            ("workers:lifecycle", "done-job"),
            ("agents:run:paused", None),
            ("agents:run:running", None),
        ],
        key=str,
    )
    recent_done = await db_session.scalar(
        select(WorkerEventRecord.position).where(WorkerEventRecord.job_id == "done-job")
    )
    assert recent_done == 4
    snapshots = (
        await db_session.execute(
            select(
                WorkerArchiveSnapshotRecord.key,
                WorkerArchiveSnapshotRecord.value["status"].as_string(),
            )
        )
    ).all()
    # The paused session keeps only its newest snapshot; the finished one
    # keeps none.
    assert sorted(snapshots) == [
        ("runstate:paused", "paused"),
        ("runstate:running", "completed"),
    ]
    assert await _remaining(db_session, WorkerArchiveEventRecord.stream) == [
        f"agents:blobs:{_BLOB}"
    ]


@pytest.mark.asyncio
async def test_bookkeeping_snapshots_keep_their_newest_copy(db_session):
    db_session.add_all(
        [
            WorkerArchiveSnapshotRecord(
                key="workers:queue_wait_history", value=[1], snapshot_at=_OLD - timedelta(days=1)
            ),
            WorkerArchiveSnapshotRecord(
                key="workers:queue_wait_history", value=[2], snapshot_at=_OLD
            ),
        ]
    )
    await db_session.commit()

    await delete_expired_worker_rows(db_session, now=_NOW)

    assert await _remaining(db_session, WorkerArchiveSnapshotRecord.value) == [[2]]
