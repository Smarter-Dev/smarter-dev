"""Tests for the hourly deletion of Skrift worker rows that belong to finished work.

Rows are written through Skrift's own SQLAlchemy backends, so they carry the
shape production stores (models wrapped as ``{"__skrift_pydantic__", "value"}``);
only their timestamps are moved back afterwards.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from skrift.agents.models import RunState
from skrift.db.models.worker import WorkerArchiveEventRecord
from skrift.db.models.worker import WorkerArchiveSnapshotRecord
from skrift.db.models.worker import WorkerDeadLetterRecord
from skrift.db.models.worker import WorkerEventRecord
from skrift.db.models.worker import WorkerQueueRecord
from skrift.db.models.worker import WorkerStateRecord
from skrift.workers.models import DeadJobEntry
from skrift.workers.models import DeadLetterCause
from skrift.workers.models import JobEnvelope
from skrift.workers.models import JobState
from skrift.workers.models import JobStatus
from skrift.workers.sqlalchemy import SQLAlchemyArchive
from skrift.workers.sqlalchemy import SQLAlchemyDeadLetterStore
from skrift.workers.sqlalchemy import SQLAlchemyEventLog
from skrift.workers.sqlalchemy import SQLAlchemyQueue
from skrift.workers.sqlalchemy import SQLAlchemyStateStore
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smarter_dev.web.worker_retention import WORKER_RETENTION
from smarter_dev.web.worker_retention import delete_expired_worker_rows

_NOW = datetime.now(UTC)
_OLD = _NOW - WORKER_RETENTION - timedelta(days=30)
_WEEKS_AGO = _NOW - timedelta(days=20)
_RECENT = _NOW - timedelta(hours=1)
_BLOB = "sha256:" + "a" * 64
_OTHER_BLOB = "sha256:" + "b" * 64
_WEEK = WORKER_RETENTION.total_seconds()


class _Skrift:
    """Skrift's production backends over the test database."""

    def __init__(self, engine) -> None:
        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        self.state = SQLAlchemyStateStore(session_maker=session_maker)
        self.queue = SQLAlchemyQueue(session_maker=session_maker)
        self.events = SQLAlchemyEventLog(session_maker=session_maker)
        self.archive = SQLAlchemyArchive(session_maker=session_maker)
        self.dead_letters = SQLAlchemyDeadLetterStore(session_maker=session_maker)
        self.session_maker = session_maker

    async def job_state(
        self,
        job_id: str,
        status: JobStatus,
        *,
        changed_at: datetime,
        ttl: float | None = None,
        session_id: str | None = None,
    ) -> None:
        payload = {"session_id": session_id} if session_id else {"context": "member words"}
        job = JobEnvelope(id=job_id, type="agents.run", queue="agents", payload=payload)
        await self.state.set(f"workers:jobs:{job_id}", JobState(job=job, status=status), ttl=ttl)
        await self.backdate_state(f"workers:jobs:{job_id}", changed_at, ttl)

    async def runstate(
        self,
        session_id: str,
        status: str,
        *,
        changed_at: datetime,
        ttl: float | None,
        blob: str = "",
    ) -> None:
        state = RunState(
            session_id=session_id,
            agent_name="resources",
            status=status,
            messages=[{"blob": blob}] if blob else [],
        )
        await self.state.set(f"runstate:{session_id}", state, ttl=ttl)
        await self.backdate_state(f"runstate:{session_id}", changed_at, ttl)

    async def snapshot(self, session_id: str, status: str, at: datetime, blob: str = "") -> None:
        state = RunState(
            session_id=session_id,
            agent_name="resources",
            status=status,
            messages=[{"blob": blob}] if blob else [],
        )
        await self.archive.upsert_state_snapshot(f"runstate:{session_id}", state, timestamp=at)

    async def event(self, stream: str, at: datetime, *, job_id: str | None = None) -> None:
        event = {"type": "job.completed", "error": "member words"}
        if job_id is not None:
            event["job_id"] = job_id
        position = await self.events.append(stream, event)
        await self._backdate(
            WorkerEventRecord,
            (WorkerEventRecord.stream == stream) & (WorkerEventRecord.position == position),
            created_at=at,
        )

    async def blob(self, blob_id: str, at: datetime) -> None:
        stream = f"agents:blobs:{blob_id}"
        await self.archive.bulk_insert_events([(stream, 0, {"data": "member words"})])
        await self._backdate(
            WorkerArchiveEventRecord, WorkerArchiveEventRecord.stream == stream, created_at=at
        )

    async def backdate_state(self, key: str, changed_at: datetime, ttl: float | None) -> None:
        await self._backdate(
            WorkerStateRecord,
            WorkerStateRecord.key == key,
            updated_at=changed_at,
            expires_at=changed_at + timedelta(seconds=ttl) if ttl is not None else None,
        )

    async def _backdate(self, model, condition, **values) -> None:
        async with self.session_maker() as session:
            await session.execute(update(model).where(condition).values(**values))
            await session.commit()


@pytest.fixture
async def skrift(test_engine, db_session):
    return _Skrift(test_engine)


async def _remaining(session, column) -> list:
    return sorted((await session.scalars(select(column))).all(), key=str)


@pytest.mark.asyncio
async def test_a_finished_job_and_session_lose_every_row(skrift, db_session):
    await skrift.job_state("done-job", JobStatus.COMPLETED, changed_at=_OLD, ttl=_WEEK)
    await skrift.event("workers:lifecycle", _OLD, job_id="done-job")
    await skrift.runstate("finished", "completed", changed_at=_OLD, ttl=86_400, blob=_BLOB)
    await skrift.snapshot("finished", "running", _OLD - timedelta(days=1))
    await skrift.snapshot("finished", "completed", _OLD, blob=_BLOB)
    await skrift.event("agents:run:finished", _OLD)
    await skrift.blob(_BLOB, _OLD)

    # Work that ran for weeks and finished an hour ago: the state stays until
    # its own expiry, but nothing written before the window is kept for it.
    await skrift.job_state("just-done", JobStatus.COMPLETED, changed_at=_RECENT, ttl=_WEEK)
    await skrift.event("workers:lifecycle", _OLD, job_id="just-done")
    await skrift.runstate("just-finished", "completed", changed_at=_RECENT, ttl=86_400)
    await skrift.snapshot("just-finished", "running", _OLD)
    await skrift.event("agents:run:just-finished", _OLD)

    counts = await delete_expired_worker_rows(db_session, now=_NOW)

    assert counts == {
        "worker_state": 2,
        "worker_queue": 0,
        "worker_dead_letters": 0,
        "worker_events": 4,
        "worker_archive_events": 1,
        "worker_archive_snapshots": 3,
    }
    assert await _remaining(db_session, WorkerStateRecord.key) == [
        "runstate:just-finished",
        "workers:jobs:just-done",
    ]


@pytest.mark.asyncio
async def test_state_goes_once_its_own_expiry_has_passed(skrift, db_session):
    await skrift.state.set("expired", {"n": 1}, ttl=60)
    await skrift.backdate_state("expired", _NOW - timedelta(minutes=2), 60)
    await skrift.state.set("unexpired", {"n": 1}, ttl=86_400)
    await skrift.state.set("no-expiry", {"n": 1})
    await skrift.backdate_state("no-expiry", _OLD, None)

    counts = await delete_expired_worker_rows(db_session, now=_NOW)

    assert counts["worker_state"] == 1
    assert await _remaining(db_session, WorkerStateRecord.key) == ["no-expiry", "unexpired"]


@pytest.mark.asyncio
async def test_old_dead_lettered_jobs_go_and_a_long_pending_timer_stays(skrift, db_session):
    for job_id in ("dead-old", "dead-recent"):
        await skrift.queue.submit(JobEnvelope(id=job_id, type="handlers.fire", queue="agents"))
        claimed = await skrift.queue.claim(["agents"], visibility_timeout=60)
        await skrift.queue.nack("agents", job_id, claimed.token, dead_letter=True)
    await skrift._backdate(
        WorkerQueueRecord, WorkerQueueRecord.job_id == "dead-old", updated_at=_OLD
    )
    # A timer submitted weeks ago for next month: never claimed, still live.
    await skrift.queue.submit(
        JobEnvelope(
            id="timer",
            type="handlers.fire",
            queue="agents",
            scheduled_for=_NOW + timedelta(days=30),
        )
    )
    await skrift._backdate(
        WorkerQueueRecord, WorkerQueueRecord.job_id == "timer", created_at=_OLD, updated_at=_OLD
    )
    await skrift.job_state("timer", JobStatus.SUBMITTED, changed_at=_OLD)
    await skrift.event("workers:lifecycle", _OLD, job_id="timer")
    for entry_id, at in (("dlq-old", _OLD), ("dlq-recent", _RECENT)):
        await skrift.dead_letters.create(
            DeadJobEntry(
                id=entry_id,
                job=JobEnvelope(type="handlers.fire"),
                queue="agents",
                job_type="handlers.fire",
                cause=DeadLetterCause.RETRIES_EXHAUSTED,
                created_at=at,
                updated_at=at,
                latest_error="member words",
            )
        )

    await delete_expired_worker_rows(db_session, now=_NOW)

    assert await _remaining(db_session, WorkerQueueRecord.job_id) == ["dead-recent", "timer"]
    assert await _remaining(db_session, WorkerDeadLetterRecord.entry_id) == ["dlq-recent"]
    assert await _remaining(db_session, WorkerStateRecord.key) == ["workers:jobs:timer"]
    assert await _remaining(db_session, WorkerEventRecord.job_id) == ["timer"]


@pytest.mark.asyncio
async def test_a_session_paused_for_weeks_keeps_what_it_needs_to_resume(skrift, db_session):
    # Started weeks ago, waiting on an approval; its last write was two days
    # ago, so its hot copy is unexpired and Skrift can resume it. The inline
    # job that runs it has no queue row and has not changed since it paused.
    await skrift.snapshot("paused", "running", _OLD - timedelta(days=1))
    await skrift.snapshot("paused", "awaiting_approval", _OLD, blob=_BLOB)
    await skrift.runstate(
        "paused", "awaiting_approval", changed_at=_NOW - timedelta(days=2), ttl=_WEEK
    )
    await skrift.event("agents:run:paused", _OLD)
    await skrift.blob(_BLOB, _OLD)
    await skrift.job_state("paused-job", JobStatus.PAUSED, changed_at=_WEEKS_AGO, session_id="paused")
    await skrift.event("workers:lifecycle", _WEEKS_AGO, job_id="paused-job")

    counts = await delete_expired_worker_rows(db_session, now=_NOW)

    assert counts["worker_state"] == 0
    assert counts["worker_events"] == 0
    assert counts["worker_archive_events"] == 0
    statuses = (
        await db_session.execute(
            select(
                WorkerArchiveSnapshotRecord.key,
                WorkerArchiveSnapshotRecord.value[("value", "status")].as_string(),
            )
        )
    ).all()
    # The newest snapshot is its state; the older one is history.
    assert statuses == [("runstate:paused", "awaiting_approval")]


@pytest.mark.asyncio
async def test_work_skrift_can_no_longer_resume_goes_after_the_window(skrift, db_session):
    # A session whose hot copy expired while it was running: Skrift cannot
    # resume it, so the paused inline job that ran it is wedged too.
    await skrift.runstate("wedged", "running", changed_at=_OLD, ttl=_WEEK)
    await skrift.snapshot("wedged", "running", _OLD, blob=_OTHER_BLOB)
    await skrift.event("agents:run:wedged", _OLD)
    await skrift.blob(_OTHER_BLOB, _OLD)
    await skrift.job_state("wedged-job", JobStatus.PAUSED, changed_at=_OLD, session_id="wedged")
    # A job left running by a worker that died: no queue row, no expiry.
    await skrift.job_state("orphan", JobStatus.RUNNING, changed_at=_OLD)
    await skrift.event("workers:lifecycle", _OLD, job_id="orphan")
    # The same, but it changed inside the window, so it stays for now.
    await skrift.job_state("recent", JobStatus.RUNNING, changed_at=_RECENT)
    await skrift.event("workers:lifecycle", _OLD, job_id="recent")

    await delete_expired_worker_rows(db_session, now=_NOW)

    assert await _remaining(db_session, WorkerStateRecord.key) == ["workers:jobs:recent"]
    assert await _remaining(db_session, WorkerEventRecord.job_id) == ["recent"]
    assert await _remaining(db_session, WorkerArchiveSnapshotRecord.key) == []
    assert await _remaining(db_session, WorkerArchiveEventRecord.stream) == []


@pytest.mark.asyncio
async def test_a_session_with_no_expiry_is_live_only_while_it_changes(skrift, db_session):
    # Written before Skrift slid a TTL onto the hot copy: no expiry at all.
    # One untouched for weeks is wedged and goes with its job and history;
    # one written inside the window may still resume, and stays.
    await skrift.runstate("stuck", "running", changed_at=_OLD, ttl=None)
    await skrift.snapshot("stuck", "running", _OLD, blob=_OTHER_BLOB)
    await skrift.event("agents:run:stuck", _OLD)
    await skrift.blob(_OTHER_BLOB, _OLD)
    await skrift.job_state("stuck-job", JobStatus.PAUSED, changed_at=_OLD, session_id="stuck")
    await skrift.runstate("waiting", "awaiting_approval", changed_at=_RECENT, ttl=None)
    await skrift.snapshot("waiting", "awaiting_approval", _OLD, blob=_BLOB)
    await skrift.event("agents:run:waiting", _OLD)
    await skrift.blob(_BLOB, _OLD)
    await skrift.job_state("waiting-job", JobStatus.PAUSED, changed_at=_OLD, session_id="waiting")

    await delete_expired_worker_rows(db_session, now=_NOW, batch_size=2)

    assert await _remaining(db_session, WorkerStateRecord.key) == [
        "runstate:waiting",
        "workers:jobs:waiting-job",
    ]
    assert await _remaining(db_session, WorkerArchiveSnapshotRecord.key) == ["runstate:waiting"]
    assert await _remaining(db_session, WorkerEventRecord.stream) == ["agents:run:waiting"]
    assert await _remaining(db_session, WorkerArchiveEventRecord.stream) == [
        f"agents:blobs:{_BLOB}"
    ]


@pytest.mark.asyncio
async def test_batches_walk_past_rows_that_stay(skrift, db_session):
    # More live rows than a batch must not stop the walk before the due ones.
    await skrift.runstate("live", "running", changed_at=_RECENT, ttl=_WEEK)
    for _ in range(5):
        await skrift.event("agents:run:live", _OLD)
    for _ in range(3):
        await skrift.event("agents:run:gone", _OLD)

    counts = await delete_expired_worker_rows(db_session, now=_NOW, batch_size=2)

    assert counts["worker_events"] == 3
    assert await _remaining(db_session, WorkerEventRecord.stream) == ["agents:run:live"] * 5


@pytest.mark.asyncio
async def test_bookkeeping_snapshots_keep_their_newest_copy(skrift, db_session):
    await skrift.archive.upsert_state_snapshot(
        "workers:queue_wait_history", [1], timestamp=_OLD - timedelta(days=1)
    )
    await skrift.archive.upsert_state_snapshot("workers:queue_wait_history", [2], timestamp=_OLD)

    await delete_expired_worker_rows(db_session, now=_NOW)

    assert await _remaining(db_session, WorkerArchiveSnapshotRecord.value) == [[2]]
