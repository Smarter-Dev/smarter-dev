"""Delete a Skrift agent session's stored copy once its caller has the result.

A Skrift agent run (the Resources stages, the chat-title agent, the blogging
stages) keeps its own copy of everything it saw and said in the worker tables:
the session's run state (``runstate:<id>`` in ``worker_state``) holds the
prompt, any earlier messages passed in, every tool call and its result and the
output; ``worker_archive_snapshots`` holds copies of that state; and the
session's event stream (``agents:run:<id>`` in ``worker_events``) records the
same as events. Skrift keeps them until the hourly retention job deletes them,
7 days after the session last changed.

The caller is the only reader: it awaits the session's result and its usage,
writes what the product needs to its own tables, and never reads the session
again. So once it has, it deletes all of it here. Nothing here runs a session
again either: a Resources retry, the only retry of an agent run, starts fresh
sessions.

The job that ran a session keeps no content either: its state is emptied as it
finishes (:mod:`smarter_dev.web.worker_state_store`). A failure here is logged
and left to the hourly retention job.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from skrift.db.models.worker import WorkerArchiveEventRecord
from skrift.db.models.worker import WorkerArchiveSnapshotRecord
from skrift.db.models.worker import WorkerEventRecord
from skrift.db.models.worker import WorkerStateRecord
from sqlalchemy import delete
from sqlalchemy import select

from smarter_dev.shared.database import get_db_session_context

logger = logging.getLogger(__name__)

_RUNSTATE_PREFIX = "runstate:"
_RUN_STREAM_PREFIX = "agents:run:"


async def _sessions_rooted_at(session, root_session_ids: list[str]) -> set[str]:
    """Every session whose root is one of these: a sub-agent records its root."""
    rows = await session.execute(
        select(
            WorkerStateRecord.key,
            WorkerStateRecord.value[("value", "root_session_id")].as_string(),
        ).where(WorkerStateRecord.key.startswith(_RUNSTATE_PREFIX))
    )
    roots = set(root_session_ids)
    return {
        key.removeprefix(_RUNSTATE_PREFIX) for key, root in rows.all() if root in roots
    }


async def forget_agent_sessions(
    session_ids: Iterable[str],
    *,
    with_sub_agents: bool = False,
    keep_events: bool = False,
) -> None:
    """Delete the sessions' run state, snapshots and, unless kept, event streams.

    ``with_sub_agents`` also deletes every session dispatched under them, found
    by its root session. ``keep_events`` is for sessions whose event streams a
    page still shows (the blogging pipeline's run timeline); those streams go
    with the hourly retention job instead.
    """
    ids = sorted({session_id for session_id in session_ids if session_id})
    if not ids:
        return
    try:
        async with get_db_session_context() as session:
            if with_sub_agents:
                ids = sorted(set(ids) | await _sessions_rooted_at(session, ids))
            state_keys = [_RUNSTATE_PREFIX + session_id for session_id in ids]
            streams = [_RUN_STREAM_PREFIX + session_id for session_id in ids]
            await session.execute(
                delete(WorkerStateRecord).where(WorkerStateRecord.key.in_(state_keys))
            )
            await session.execute(
                delete(WorkerArchiveSnapshotRecord).where(
                    WorkerArchiveSnapshotRecord.key.in_(state_keys)
                )
            )
            if not keep_events:
                await session.execute(
                    delete(WorkerEventRecord).where(
                        WorkerEventRecord.stream.in_(streams)
                    )
                )
                await session.execute(
                    delete(WorkerArchiveEventRecord).where(
                        WorkerArchiveEventRecord.stream.in_(streams)
                    )
                )
            await session.commit()
    except Exception:
        logger.exception(
            "Could not delete %d finished agent session(s); the hourly retention job will",
            len(ids),
        )
