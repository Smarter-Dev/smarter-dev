"""Scheduled deletion of security-log rows past their 90-day window.

Nothing writes ``security_logs`` since #81 (security events are logs, see
:mod:`smarter_dev.web.security_logger`); this sweep ages out the rows written
before then, one per bytes API call with the Discord ids in request paths,
until the table is dropped. The 90-day window is the one the old
``SecurityLogger.cleanup_old_logs`` declared and nothing called; the hourly
retention job (``scripts/retention_sweep.py``) enforces it.

Rows are deleted, not scrubbed: a security log with the identifying columns
blanked has no audit value left. The delete runs in bounded batches, each
committed on its own, so the first run's backlog never becomes one long
transaction and a run that dies partway keeps what it finished. Deleting is
monotonic, so re-running is always safe.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import SecurityLog

logger = logging.getLogger(__name__)

SECURITY_LOG_RETENTION = timedelta(days=90)

# Rows deleted per statement. Small enough that one batch is a short lock on a
# table the API writes to on every request.
_BATCH_SIZE = 1000


def security_log_cutoff(now: datetime) -> datetime:
    """Rows strictly older than this are due for deletion."""
    return now - SECURITY_LOG_RETENTION


async def delete_expired_security_logs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int = _BATCH_SIZE,
) -> int:
    """Delete every security log older than the retention window.

    Commits after each batch. Returns the number of rows deleted.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")
    cutoff = security_log_cutoff(now or datetime.now(UTC))
    deleted = 0
    while True:
        due = (
            select(SecurityLog.id)
            .where(SecurityLog.timestamp < cutoff)
            .limit(batch_size)
            .scalar_subquery()
        )
        result = await session.execute(
            delete(SecurityLog)
            .where(SecurityLog.id.in_(due))
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        batch = result.rowcount or 0
        deleted += batch
        if batch < batch_size:
            break

    logger.info("security_logs: %d deleted (cutoff %s)", deleted, cutoff.isoformat())
    return deleted
