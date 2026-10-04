"""Scheduled deletion of Skrift worker rows that belong to finished work.

Skrift's own pruner (``WorkerPruner``) runs only under ``skrift workers
persister`` or ``skrift workers prune``, which no manifest runs, and its
SQLAlchemy backends implement few of its hooks: a ``worker_state`` row past its
expiry is deleted only when that key is read again, and an open dead letter
never. Those tables keep job payloads, agent run state (a Resources question,
an agent's prompt) and error text, so the hourly retention job
(``scripts/retention_sweep.py``) bounds them itself:

- ``worker_state``: a row whose own ``expires_at`` has passed, which Skrift
  already treats as gone. Rows with no expiry stay.
- ``worker_queue``: a dead-lettered row older than :data:`WORKER_RETENTION`.
  Pending jobs are never touched, however old: a handler timer can be due
  weeks ahead.
- ``worker_dead_letters``: every entry older than the window, open or not.
- ``worker_events``, ``worker_archive_events`` and
  ``worker_archive_snapshots``: rows older than the window, except those of
  live work. Live work is a job still queued, claimed, running or paused (a
  ``worker_queue`` row that was not dead-lettered, or a job state that is not
  terminal), and an agent session whose run state is not terminal (queued,
  running, awaiting approval or paused, read from the hot copy or, when that
  has expired, from its latest snapshot). A live session keeps its event
  stream, its latest snapshot and every stored blob its state or events
  name. A snapshot that is not the latest for its key is history and goes.

Each table is deleted in bounded batches, each committed on its own, like the
security-log delete; re-running is always safe.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from skrift.db.models.worker import WorkerArchiveEventRecord
from skrift.db.models.worker import WorkerArchiveSnapshotRecord
from skrift.db.models.worker import WorkerDeadLetterRecord
from skrift.db.models.worker import WorkerEventRecord
from skrift.db.models.worker import WorkerQueueRecord
from skrift.db.models.worker import WorkerStateRecord
from sqlalchemy import delete
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

WORKER_RETENTION = timedelta(days=7)

_BATCH_SIZE = 500


def worker_retention_cutoff(now: datetime) -> datetime:
    """Rows written strictly before this are due for deletion."""
    return now - WORKER_RETENTION


_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "dead_lettered", "cancelled"})
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
_JOB_STATE_PREFIX = "workers:jobs:"
_RUNSTATE_PREFIX = "runstate:"
_RUN_STREAM_PREFIX = "agents:run:"
_BLOB_STREAM_PREFIX = "agents:blobs:"
_BLOB_ID = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class LiveWork:
    """What the 7-day deletes must leave alone, read once per sweep."""

    job_ids: frozenset[str]
    run_streams: frozenset[str]
    blob_streams: frozenset[str]
    kept_snapshot_ids: frozenset[Any]


async def find_live_work(session: AsyncSession, now: datetime) -> LiveWork:
    """The jobs and agent sessions still in flight, and what they reference."""
    job_ids = set(
        (
            await session.scalars(
                select(WorkerQueueRecord.job_id).where(
                    WorkerQueueRecord.dead_lettered.is_(False)
                )
            )
        ).all()
    )
    hot_statuses = (
        await session.execute(
            select(
                WorkerStateRecord.key,
                WorkerStateRecord.value["status"].as_string(),
            ).where(
                or_(
                    WorkerStateRecord.key.startswith(_JOB_STATE_PREFIX),
                    WorkerStateRecord.key.startswith(_RUNSTATE_PREFIX),
                ),
                or_(
                    WorkerStateRecord.expires_at.is_(None),
                    WorkerStateRecord.expires_at > now,
                ),
            )
        )
    ).all()
    run_status: dict[str, str | None] = {}
    for key, status in hot_statuses:
        if key.startswith(_JOB_STATE_PREFIX):
            if status not in _TERMINAL_JOB_STATUSES:
                job_ids.add(key.removeprefix(_JOB_STATE_PREFIX))
        else:
            run_status[key] = status

    # Skrift appends a snapshot per write; the newest one per key is the state.
    latest: dict[str, tuple[datetime, Any, str | None]] = {}
    for snapshot_id, key, snapshot_at, status in (
        await session.execute(
            select(
                WorkerArchiveSnapshotRecord.id,
                WorkerArchiveSnapshotRecord.key,
                WorkerArchiveSnapshotRecord.snapshot_at,
                WorkerArchiveSnapshotRecord.value["status"].as_string(),
            )
        )
    ).all():
        if key not in latest or snapshot_at > latest[key][0]:
            latest[key] = (snapshot_at, snapshot_id, status)
    for key, (_, _, status) in latest.items():
        if key.startswith(_RUNSTATE_PREFIX):
            run_status.setdefault(key, status)

    live_runstate_keys = {
        key
        for key, status in run_status.items()
        if status not in _TERMINAL_RUN_STATUSES
    }
    run_streams = {
        _RUN_STREAM_PREFIX + key.removeprefix(_RUNSTATE_PREFIX)
        for key in live_runstate_keys
    }
    # A snapshot that is not a run state is Skrift's own bookkeeping: its
    # newest copy stays like a live session's.
    kept_snapshot_ids = {
        snapshot_id
        for key, (_, snapshot_id, _) in latest.items()
        if key in live_runstate_keys or not key.startswith(_RUNSTATE_PREFIX)
    }
    blob_streams = await _blobs_named_by(session, live_runstate_keys, run_streams)
    return LiveWork(
        job_ids=frozenset(job_ids),
        run_streams=frozenset(run_streams),
        blob_streams=frozenset(blob_streams),
        kept_snapshot_ids=frozenset(kept_snapshot_ids),
    )


async def _blobs_named_by(
    session: AsyncSession, runstate_keys: set[str], run_streams: set[str]
) -> set[str]:
    """Blob streams a live session's state or events point at.

    Blobs are content-addressed and shared, so one written long ago can back a
    session that is still running.
    """
    if not runstate_keys:
        return set()
    values: list[Any] = [
        *(
            await session.scalars(
                select(WorkerStateRecord.value).where(
                    WorkerStateRecord.key.in_(runstate_keys)
                )
            )
        ).all(),
        *(
            await session.scalars(
                select(WorkerArchiveSnapshotRecord.value).where(
                    WorkerArchiveSnapshotRecord.key.in_(runstate_keys)
                )
            )
        ).all(),
        *(
            await session.scalars(
                select(WorkerEventRecord.event).where(
                    WorkerEventRecord.stream.in_(run_streams)
                )
            )
        ).all(),
    ]
    return {
        _BLOB_STREAM_PREFIX + blob_id
        for value in values
        for blob_id in _BLOB_ID.findall(json.dumps(value, default=str))
    }


def _due_conditions(now: datetime, live: LiveWork) -> dict[str, tuple[Any, list[Any]]]:
    cutoff = worker_retention_cutoff(now)
    return {
        "worker_state": (
            WorkerStateRecord,
            [
                WorkerStateRecord.expires_at.is_not(None),
                WorkerStateRecord.expires_at <= now,
            ],
        ),
        "worker_queue": (
            WorkerQueueRecord,
            [
                WorkerQueueRecord.dead_lettered.is_(True),
                WorkerQueueRecord.updated_at < cutoff,
            ],
        ),
        "worker_dead_letters": (
            WorkerDeadLetterRecord,
            [WorkerDeadLetterRecord.entry_created_at < cutoff],
        ),
        "worker_events": (
            WorkerEventRecord,
            [
                WorkerEventRecord.created_at < cutoff,
                or_(
                    WorkerEventRecord.job_id.is_(None),
                    WorkerEventRecord.job_id.not_in(live.job_ids),
                ),
                WorkerEventRecord.stream.not_in(live.run_streams),
            ],
        ),
        "worker_archive_events": (
            WorkerArchiveEventRecord,
            [
                WorkerArchiveEventRecord.created_at < cutoff,
                WorkerArchiveEventRecord.stream.not_in(
                    live.run_streams | live.blob_streams
                ),
            ],
        ),
        "worker_archive_snapshots": (
            WorkerArchiveSnapshotRecord,
            [
                WorkerArchiveSnapshotRecord.snapshot_at < cutoff,
                WorkerArchiveSnapshotRecord.id.not_in(live.kept_snapshot_ids),
            ],
        ),
    }


async def _delete_due(
    session: AsyncSession, model: Any, conditions: list[Any], batch_size: int
) -> int:
    deleted = 0
    while True:
        due = select(model.id).where(*conditions).limit(batch_size).scalar_subquery()
        result = await session.execute(
            delete(model)
            .where(model.id.in_(due))
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        batch = result.rowcount or 0
        deleted += batch
        if batch < batch_size:
            return deleted


async def delete_expired_worker_rows(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int = _BATCH_SIZE,
) -> dict[str, int]:
    """Delete every worker row past its window. Returns counts by table."""
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")
    now = now or datetime.now(UTC)
    live = await find_live_work(session, now)
    counts = {
        table: await _delete_due(session, model, conditions, batch_size)
        for table, (model, conditions) in _due_conditions(now, live).items()
    }
    logger.info(
        "worker tables: %s deleted (cutoff %s)",
        ", ".join(f"{table}={count}" for table, count in counts.items()),
        worker_retention_cutoff(now).isoformat(),
    )
    return counts
