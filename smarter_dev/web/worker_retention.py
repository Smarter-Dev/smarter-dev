"""Scheduled deletion of Skrift worker rows that belong to finished work.

Skrift's own pruner (``WorkerPruner``) runs only under ``skrift workers
persister`` or ``skrift workers prune``, which no manifest runs, and its
SQLAlchemy backends implement few of its hooks: a ``worker_state`` row past its
expiry is deleted only when that key is read again, and an open dead letter
never. Those tables keep job payloads, agent run state (an agent's prompt)
and error text, so the hourly retention job
(``scripts/retention_sweep.py``) bounds them itself.

Live work is never deleted, whatever its age. Live work is what Skrift can
still run or resume:

- a job with a ``worker_queue`` row that was not dead-lettered (queued,
  claimed, paused with a wake time, or a handler timer due weeks ahead);
- an agent session whose hot run state (``runstate:<id>`` in
  ``worker_state``) has not expired, is not terminal and changed within
  :data:`SESSION_BACKSTOP` (6 hours). Skrift resumes a session only from that
  hot copy (``update_runstate`` raises ``KeyError`` without it). Every agent
  run here finishes within minutes, writing as it goes, and none waits for an
  approval, so a session idle for 6 hours was cut off: its caller is gone and
  nothing will read it;
- a job state that is not terminal and either belongs to a live session (an
  inline agent run paused for approval has no queue row) or changed within
  :data:`WORKER_RETENTION`.

Everything else is finished, dead-lettered or wedged beyond resuming. An
agent session's own rows (its run state, snapshots and event stream) go once
they are older than :data:`SESSION_BACKSTOP`: the code that ran a session
deletes it as soon as it has the result (``agent_session_cleanup.py``), so
these are sessions whose caller was cut off. The exception is a blogging
session's event stream, which the blogging pipeline's admin run timeline
reads; it is kept for :data:`WORKER_RETENTION` like the operational records.
Everything else goes once it is older than :data:`WORKER_RETENTION`:

- ``worker_state``: a row whose own ``expires_at`` has passed, which Skrift
  already treats as gone, and a job state or run state that is not live and
  has not changed within the window. Other keys with no expiry stay.
- ``worker_queue``: a dead-lettered row (it holds the job's payload).
- ``worker_dead_letters``: every entry, open or not.
- ``worker_events``: lifecycle events of jobs that are not live, and the
  event streams of sessions that are not live.
- ``worker_archive_events``: streams of sessions that are not live, and blobs
  that no live session's state or events name. Blobs are content-addressed
  and shared, so one written long ago can back a session still running.
- ``worker_archive_snapshots``: every snapshot of a session or job that is not
  live, and every snapshot but the newest of one that is. Skrift appends a
  snapshot per write; the newest per key is the state.

Skrift stores a model (``JobState``, ``RunState``) as
``{"__skrift_pydantic__": ..., "value": {...}}``, so a status is read from
``value -> 'value' ->> 'status'``.

Live work is read in pages by primary key too. Each table is read and
deleted in batches by primary key, each committed on
its own like the security-log delete, and every delete repeats its age
condition so a row rewritten since it was read stays. Re-running is always
safe.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from collections.abc import Callable
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
from sqlalchemy import and_
from sqlalchemy import delete
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

WORKER_RETENTION = timedelta(days=7)
SESSION_BACKSTOP = timedelta(hours=6)

_BATCH_SIZE = 500


def worker_retention_cutoff(now: datetime) -> datetime:
    """Rows written strictly before this are due for deletion."""
    return now - WORKER_RETENTION


def session_cutoff(now: datetime) -> datetime:
    """An agent session's rows written strictly before this are due."""
    return now - SESSION_BACKSTOP


_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "dead_lettered", "cancelled"})
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
_JOB_STATE_PREFIX = "workers:jobs:"
_RUNSTATE_PREFIX = "runstate:"
_RUN_STREAM_PREFIX = "agents:run:"
_BLOB_STREAM_PREFIX = "agents:blobs:"
_BLOB_ID = re.compile(r"sha256:[0-9a-f]{64}")
# Agents whose event streams a page reads after they finish: the blogging
# pipeline's admin run timeline (blogging_agent_admin.py).
_TIMELINE_AGENT_PREFIX = "blogging."


def _model_field(column: Any, *path: str) -> Any:
    """A field of a model Skrift stored wrapped in ``{"value": ...}``."""
    return column[("value", *path)].as_string()


def _unexpired(now: datetime) -> Any:
    return or_(WorkerStateRecord.expires_at.is_(None), WorkerStateRecord.expires_at > now)


@dataclass(frozen=True)
class LiveWork:
    """What the deletes must leave alone, read once per sweep."""

    job_ids: frozenset[str]
    session_ids: frozenset[str]
    blob_streams: frozenset[str]
    kept_snapshot_ids: frozenset[Any]
    timeline_streams: frozenset[str] = frozenset()

    def keeps_stream(self, stream: str) -> bool:
        if stream.startswith(_RUN_STREAM_PREFIX):
            return stream.removeprefix(_RUN_STREAM_PREFIX) in self.session_ids
        return stream in self.blob_streams

    def keeps_event(self, stream: str, created_at: datetime, now: datetime) -> bool:
        """A session's event stream goes at the session backstop, unless a page
        reads it; then it, like any other stream, goes after the full window."""
        if self.keeps_stream(stream):
            return True
        if stream.startswith(_RUN_STREAM_PREFIX) and stream not in self.timeline_streams:
            return False
        return _aware(created_at) >= worker_retention_cutoff(now)

    def keeps_state_key(self, key: str) -> bool:
        if key.startswith(_JOB_STATE_PREFIX):
            return key.removeprefix(_JOB_STATE_PREFIX) in self.job_ids
        if key.startswith(_RUNSTATE_PREFIX):
            return key.removeprefix(_RUNSTATE_PREFIX) in self.session_ids
        return True


async def _pages(
    session: AsyncSession,
    model: Any,
    columns: tuple[Any, ...],
    *conditions: Any,
    batch_size: int,
) -> AsyncIterator[Row[Any]]:
    """Every row matching ``conditions``, read ``batch_size`` at a time by id."""
    after = None
    while True:
        query = select(model.id, *columns).where(*conditions)
        if after is not None:
            query = query.where(model.id > after)
        rows = (await session.execute(query.order_by(model.id).limit(batch_size))).all()
        for row in rows:
            yield row
        if len(rows) < batch_size:
            return
        after = rows[-1].id


async def find_live_work(
    session: AsyncSession, now: datetime, *, batch_size: int = _BATCH_SIZE
) -> LiveWork:
    """The jobs and agent sessions Skrift can still run or resume, and what they reference."""
    cutoff = worker_retention_cutoff(now)
    job_ids = {
        row.job_id
        async for row in _pages(
            session,
            WorkerQueueRecord,
            (WorkerQueueRecord.job_id,),
            WorkerQueueRecord.dead_lettered.is_(False),
            batch_size=batch_size,
        )
    }
    session_ids = {
        row.key.removeprefix(_RUNSTATE_PREFIX)
        async for row in _pages(
            session,
            WorkerStateRecord,
            (
                WorkerStateRecord.key,
                _model_field(WorkerStateRecord.value, "status").label("status"),
                WorkerStateRecord.expires_at,
                WorkerStateRecord.updated_at,
            ),
            WorkerStateRecord.key.startswith(_RUNSTATE_PREFIX),
            _unexpired(now),
            batch_size=batch_size,
        )
        if row.status not in _TERMINAL_RUN_STATUSES
        and _aware(row.updated_at) >= session_cutoff(now)
    }
    async for row in _pages(
        session,
        WorkerStateRecord,
        (
            WorkerStateRecord.key,
            _model_field(WorkerStateRecord.value, "status").label("status"),
            _model_field(WorkerStateRecord.value, "job", "payload", "session_id").label(
                "job_session_id"
            ),
            WorkerStateRecord.updated_at,
        ),
        WorkerStateRecord.key.startswith(_JOB_STATE_PREFIX),
        _unexpired(now),
        batch_size=batch_size,
    ):
        if row.status in _TERMINAL_JOB_STATUSES:
            continue
        if _aware(row.updated_at) >= cutoff or row.job_session_id in session_ids:
            job_ids.add(row.key.removeprefix(_JOB_STATE_PREFIX))

    live = LiveWork(
        job_ids=frozenset(job_ids),
        session_ids=frozenset(session_ids),
        blob_streams=frozenset(),
        kept_snapshot_ids=frozenset(),
    )
    # Only the newest snapshot of live work is its state; the rest is history.
    latest: dict[str, tuple[datetime, Any]] = {}
    async for row in _pages(
        session,
        WorkerArchiveSnapshotRecord,
        (WorkerArchiveSnapshotRecord.key, WorkerArchiveSnapshotRecord.snapshot_at),
        batch_size=batch_size,
    ):
        if live.keeps_state_key(row.key) and (
            row.key not in latest or _aware(row.snapshot_at) > latest[row.key][0]
        ):
            latest[row.key] = (_aware(row.snapshot_at), row.id)
    return LiveWork(
        job_ids=live.job_ids,
        session_ids=live.session_ids,
        blob_streams=frozenset(await _blobs_named_by(session, session_ids)),
        kept_snapshot_ids=frozenset(snapshot_id for _, snapshot_id in latest.values()),
        timeline_streams=frozenset(await _timeline_streams(session)),
    )


async def _timeline_streams(session: AsyncSession) -> set[str]:
    """Event streams of sessions a page reads after they finish.

    Skrift records the agent's name on the event that starts a run.
    """
    streams: set[str] = set()
    for model, column in (
        (WorkerEventRecord, WorkerEventRecord.event),
        (WorkerArchiveEventRecord, WorkerArchiveEventRecord.event),
    ):
        rows = await session.scalars(
            select(model.stream)
            .where(
                model.stream.startswith(_RUN_STREAM_PREFIX),
                column[("payload", "agent_name")]
                .as_string()
                .startswith(_TIMELINE_AGENT_PREFIX),
            )
            .distinct()
        )
        streams.update(rows.all())
    return streams


def _aware(value: datetime) -> datetime:
    """SQLite hands timestamps back naive; they were written as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _blobs_named_by(session: AsyncSession, session_ids: set[str]) -> set[str]:
    """Blob streams a live session's state or events point at."""
    blob_streams: set[str] = set()
    session_list = sorted(session_ids)
    for start in range(0, len(session_list), _BATCH_SIZE):
        chunk = session_list[start : start + _BATCH_SIZE]
        runstate_keys = [_RUNSTATE_PREFIX + session_id for session_id in chunk]
        run_streams = [_RUN_STREAM_PREFIX + session_id for session_id in chunk]
        for query in (
            select(WorkerStateRecord.value).where(WorkerStateRecord.key.in_(runstate_keys)),
            select(WorkerArchiveSnapshotRecord.value).where(
                WorkerArchiveSnapshotRecord.key.in_(runstate_keys)
            ),
            select(WorkerEventRecord.event).where(WorkerEventRecord.stream.in_(run_streams)),
        ):
            for value in (await session.scalars(query)).all():
                blob_streams.update(
                    _BLOB_STREAM_PREFIX + blob_id
                    for blob_id in _BLOB_ID.findall(json.dumps(value, default=str))
                )
    return blob_streams


@dataclass(frozen=True)
class _Due:
    """A table's deletion rule: an SQL condition, then a check against live work."""

    model: Any
    condition: Any
    columns: tuple[Any, ...] = ()
    is_due: Callable[[Row[Any]], bool] = lambda row: True


def _due_rules(now: datetime, live: LiveWork) -> dict[str, _Due]:
    cutoff = worker_retention_cutoff(now)
    sessions_cutoff = session_cutoff(now)
    return {
        "worker_state": _Due(
            WorkerStateRecord,
            or_(
                and_(
                    WorkerStateRecord.expires_at.is_not(None),
                    WorkerStateRecord.expires_at <= now,
                ),
                and_(
                    WorkerStateRecord.key.startswith(_JOB_STATE_PREFIX),
                    WorkerStateRecord.updated_at < cutoff,
                ),
                and_(
                    WorkerStateRecord.key.startswith(_RUNSTATE_PREFIX),
                    WorkerStateRecord.updated_at < sessions_cutoff,
                ),
            ),
            (WorkerStateRecord.key, WorkerStateRecord.expires_at),
            lambda row: (
                (row.expires_at is not None and _aware(row.expires_at) <= now)
                or not live.keeps_state_key(row.key)
            ),
        ),
        "worker_queue": _Due(
            WorkerQueueRecord,
            and_(
                WorkerQueueRecord.dead_lettered.is_(True),
                WorkerQueueRecord.updated_at < cutoff,
            ),
        ),
        "worker_dead_letters": _Due(
            WorkerDeadLetterRecord,
            WorkerDeadLetterRecord.entry_created_at < cutoff,
        ),
        "worker_events": _Due(
            WorkerEventRecord,
            or_(
                and_(
                    WorkerEventRecord.stream.startswith(_RUN_STREAM_PREFIX),
                    WorkerEventRecord.created_at < sessions_cutoff,
                ),
                WorkerEventRecord.created_at < cutoff,
            ),
            (WorkerEventRecord.stream, WorkerEventRecord.job_id, WorkerEventRecord.created_at),
            lambda row: (
                (row.job_id is None or row.job_id not in live.job_ids)
                and not live.keeps_event(row.stream, row.created_at, now)
            ),
        ),
        "worker_archive_events": _Due(
            WorkerArchiveEventRecord,
            or_(
                and_(
                    WorkerArchiveEventRecord.stream.startswith(_RUN_STREAM_PREFIX),
                    WorkerArchiveEventRecord.created_at < sessions_cutoff,
                ),
                WorkerArchiveEventRecord.created_at < cutoff,
            ),
            (WorkerArchiveEventRecord.stream, WorkerArchiveEventRecord.created_at),
            lambda row: not live.keeps_event(row.stream, row.created_at, now),
        ),
        "worker_archive_snapshots": _Due(
            WorkerArchiveSnapshotRecord,
            or_(
                and_(
                    WorkerArchiveSnapshotRecord.key.startswith(_RUNSTATE_PREFIX),
                    WorkerArchiveSnapshotRecord.snapshot_at < sessions_cutoff,
                ),
                WorkerArchiveSnapshotRecord.snapshot_at < cutoff,
            ),
            (),
            lambda row: row.id not in live.kept_snapshot_ids,
        ),
    }


async def _delete_due(session: AsyncSession, rule: _Due, batch_size: int) -> int:
    """Walk the rows matching the rule's condition by id and delete the due ones."""
    model = rule.model
    deleted = 0
    after = None
    while True:
        query = select(model.id, *rule.columns).where(rule.condition)
        if after is not None:
            query = query.where(model.id > after)
        rows = (await session.execute(query.order_by(model.id).limit(batch_size))).all()
        if not rows:
            return deleted
        after = rows[-1].id
        due_ids = [row.id for row in rows if rule.is_due(row)]
        if due_ids:
            result = await session.execute(
                delete(model)
                .where(model.id.in_(due_ids), rule.condition)
                .execution_options(synchronize_session=False)
            )
            deleted += result.rowcount or 0
        await session.commit()
        if len(rows) < batch_size:
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
    live = await find_live_work(session, now, batch_size=batch_size)
    counts = {
        table: await _delete_due(session, rule, batch_size)
        for table, rule in _due_rules(now, live).items()
    }
    logger.info(
        "worker tables: %s deleted (cutoff %s)",
        ", ".join(f"{table}={count}" for table, count in counts.items()),
        worker_retention_cutoff(now).isoformat(),
    )
    return counts
