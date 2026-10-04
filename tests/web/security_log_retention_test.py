"""Tests for the 90-day security-log deletion in the hourly retention job."""

from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from skrift.db.models.worker import WorkerDeadLetterRecord
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.web.models import SecurityLog
from smarter_dev.web.security_log_retention import SECURITY_LOG_RETENTION
from smarter_dev.web.security_log_retention import delete_expired_security_logs
from smarter_dev.web.security_log_retention import security_log_cutoff

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
_CUTOFF = _NOW - SECURITY_LOG_RETENTION


def _log(timestamp: datetime, details: str) -> SecurityLog:
    return SecurityLog(
        action="api_request", success=True, details=details, timestamp=timestamp
    )


async def _remaining(session) -> list[str]:
    rows = await session.scalars(select(SecurityLog.details))
    return sorted(rows.all())


def test_window_matches_the_declared_policy():
    assert SECURITY_LOG_RETENTION == timedelta(days=90)
    assert security_log_cutoff(_NOW) == _NOW - timedelta(days=90)


@pytest.mark.asyncio
async def test_deletes_only_rows_strictly_older_than_the_window(db_session):
    db_session.add_all(
        [
            _log(_CUTOFF - timedelta(days=400), "ancient"),
            _log(_CUTOFF - timedelta(seconds=1), "just-past"),
            _log(_CUTOFF, "at-cutoff"),
            _log(_CUTOFF + timedelta(seconds=1), "just-inside"),
            _log(_NOW, "fresh"),
        ]
    )
    await db_session.commit()

    deleted = await delete_expired_security_logs(db_session, now=_NOW)

    assert deleted == 2
    assert await _remaining(db_session) == ["at-cutoff", "fresh", "just-inside"]


@pytest.mark.asyncio
async def test_batches_through_a_backlog_larger_than_one_batch(db_session):
    db_session.add_all(
        [_log(_CUTOFF - timedelta(days=1, minutes=i), f"old-{i}") for i in range(7)]
        + [_log(_NOW, "fresh")]
    )
    await db_session.commit()

    deleted = await delete_expired_security_logs(db_session, now=_NOW, batch_size=3)

    assert deleted == 7
    assert await _remaining(db_session) == ["fresh"]


@pytest.mark.asyncio
async def test_exact_multiple_of_batch_size_terminates(db_session):
    db_session.add_all(
        [_log(_CUTOFF - timedelta(days=1, minutes=i), f"old-{i}") for i in range(4)]
    )
    await db_session.commit()

    assert await delete_expired_security_logs(db_session, now=_NOW, batch_size=2) == 4
    assert await _remaining(db_session) == []


@pytest.mark.asyncio
async def test_rejects_a_batch_size_that_could_never_finish(db_session):
    with pytest.raises(ValueError):
        await delete_expired_security_logs(db_session, now=_NOW, batch_size=0)


@pytest.mark.asyncio
async def test_rerun_is_a_no_op(db_session):
    db_session.add_all([_log(_CUTOFF - timedelta(days=1), "old"), _log(_NOW, "fresh")])
    await db_session.commit()

    assert await delete_expired_security_logs(db_session, now=_NOW) == 1
    assert await delete_expired_security_logs(db_session, now=_NOW) == 0
    assert await _remaining(db_session) == ["fresh"]


@pytest.mark.asyncio
async def test_commits_its_own_work(db_session, test_engine):
    """A later rollback by the caller must not resurrect deleted rows."""
    db_session.add(_log(_CUTOFF - timedelta(days=1), "old"))
    await db_session.commit()

    await delete_expired_security_logs(db_session, now=_NOW)
    await db_session.rollback()

    async with test_engine.connect() as conn:
        count = await conn.scalar(select(func.count()).select_from(SecurityLog))
    assert count == 0


@pytest.mark.asyncio
async def test_hourly_sweep_entrypoint_deletes_expired_security_logs(
    db_session, monkeypatch
):
    """The CronJob runs scripts/retention_sweep.py; running it clears old logs
    and old worker dead letters."""
    spec = importlib.util.spec_from_file_location(
        "retention_sweep_script",
        Path(__file__).resolve().parents[2] / "scripts" / "retention_sweep.py",
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    @asynccontextmanager
    async def session_context():
        yield db_session

    monkeypatch.setattr(script, "get_db_session_context", session_context)
    old = datetime.now(UTC) - SECURITY_LOG_RETENTION - timedelta(days=1)
    db_session.add_all([_log(old, "old"), _log(datetime.now(UTC), "fresh")])
    await db_session.commit()

    db_session.add(
        WorkerDeadLetterRecord(
            entry_id="old-dead-letter",
            queue="agents",
            job_type="handlers.fire",
            cause="retries_exhausted",
            state="open",
            entry={},
            entry_created_at=old,
            entry_updated_at=old,
        )
    )
    await db_session.commit()

    assert await script.main() == 0
    assert await _remaining(db_session) == ["fresh"]
    assert (
        await db_session.scalar(select(func.count()).select_from(WorkerDeadLetterRecord))
        == 0
    )
