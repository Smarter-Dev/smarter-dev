"""Skrift's SQLAlchemy state store, emptying a job's state once the job finishes.

Skrift keeps a job's state (``workers:jobs:<id>`` in ``worker_state``) for 7
days after the job ends, and that state carries the job's payload and its
handler's return value. Those can hold what a member wrote: a Resources
question in the payload, a Chat reply in the result, whatever a handler script
carried to a timer. By the time Skrift records a job as finished, its handler
has already written what the product reads to the product's own tables, and
nothing in this codebase reads a finished job's payload or result back.

So when a job's state is written as finished (completed, dead-lettered or
cancelled), its payload, result and paused state are emptied in the same
write. The row keeps the job's id, type, queue, status, attempt count, errors
and timings. A job Skrift will still retry is written as ``submitted`` and
keeps everything. A dead-lettered job's payload stays in its dead letter and
its dead-lettered queue row, which is what an operator replays it from; the
hourly retention job deletes both after 7 days
(:mod:`smarter_dev.web.worker_retention`).

One consequence: Skrift treats a submission that reuses a job id as the same
job only if its payload matches the stored one, so resubmitting a finished
job's id now raises ``JobIdConflict`` instead of returning its handle. Nothing
here does that; every submit uses a fresh id, and an agent's outbox submits
only an id with no state at all.

``app.yaml`` names this class as ``workers.backends.state_store``.
"""

from __future__ import annotations

import inspect
from typing import Any

from skrift.workers.models import JobState
from skrift.workers.models import JobStatus
from skrift.workers.sqlalchemy import SQLAlchemyStateStore

JOB_STATE_PREFIX = "workers:jobs:"

FINISHED_JOB_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.DEAD_LETTERED, JobStatus.CANCELLED}
)


def without_finished_job_content(key: str, value: Any) -> Any:
    """``value`` as stored: a finished job's state with its content emptied."""
    if (
        not key.startswith(JOB_STATE_PREFIX)
        or not isinstance(value, JobState)
        or value.status not in FINISHED_JOB_STATUSES
    ):
        return value
    return value.model_copy(
        update={
            "job": value.job.model_copy(update={"payload": {}}),
            "result": None,
            "paused_state": {},
        }
    )


class FinishedJobStateStore(SQLAlchemyStateStore):
    """Writes a finished job's state without its payload, result or paused state."""

    async def set(self, key: str, value: Any, *, ttl: Any = None) -> None:
        await super().set(key, without_finished_job_content(key, value), ttl=ttl)

    async def update(self, key: str, fn: Any, *, ttl: Any = None) -> Any:
        async def emptied(current: Any) -> Any:
            value = fn(current)
            if inspect.isawaitable(value):
                value = await value
            return without_finished_job_content(key, value)

        return await super().update(key, emptied, ttl=ttl)
