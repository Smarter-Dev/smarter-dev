"""Delete a Skrift agent session's stored copy once its caller has the result.

A Skrift agent run (the Resources stages, the chat-title agent, the blogging
stages) keeps its own copy of everything it saw and said in the worker tables:
the session's run state (``runstate:<id>`` in ``worker_state``) holds the
prompt, any earlier messages passed in, every tool call and its result and the
output; ``worker_archive_snapshots`` holds copies of that state; and the
session's event stream (``agents:run:<id>`` in ``worker_events``) records the
same as events. Skrift would keep them until the hourly retention job deleted
them, 5 hours after the session last changed (gone by 6 hours, as the job
runs hourly).

The caller is the only reader: it awaits the session's result and its usage,
writes what the product needs to its own tables, and never reads the session
again. So once it has, it deletes all of it here. Nothing here runs a session
again either: a Resources retry, the only retry of an agent run, starts fresh
sessions.

The job that ran a session keeps no content either: its state is emptied as it
finishes (:mod:`smarter_dev.web.worker_state_store`). A failure here is logged
and left to the hourly retention job, which deletes a session 5 hours after its
last write.

A session's run state is also the only record of its token usage. A caller
that does not record usage itself passes ``usage`` so each model turn becomes a
``usage_cost_rows`` row, in the same transaction, before the session goes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from skrift.agents.models import RunState
from skrift.db.models.worker import WorkerArchiveEventRecord
from skrift.db.models.worker import WorkerArchiveSnapshotRecord
from skrift.db.models.worker import WorkerEventRecord
from skrift.db.models.worker import WorkerStateRecord
from sqlalchemy import delete
from sqlalchemy import select

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.model_catalog import MODEL_CATALOG

logger = logging.getLogger(__name__)

_RUNSTATE_PREFIX = "runstate:"
_RUN_STREAM_PREFIX = "agents:run:"


@dataclass(frozen=True)
class UsageOwner:
    """Who a session's model turns are charged to, as ``usage_cost_rows``."""

    product_mode: str
    operation_type: str
    user_id: UUID | None = None
    conversation_id: UUID | None = None


def _catalog_model(configured: str | None):
    if not configured:
        return None
    wire = configured.split(":", 1)[-1]
    return next(
        (
            model
            for model in MODEL_CATALOG
            if wire == model.model_id or wire.startswith(model.model_id)
        ),
        None,
    )


async def _record_usage(session, state_keys: list[str], owner: UsageOwner) -> None:
    """One cost row per model turn, keyed by session and turn so a re-run adds none."""
    from smarter_dev.web.chat.usage import record_usage

    values = (
        await session.scalars(
            select(WorkerStateRecord.value).where(WorkerStateRecord.key.in_(state_keys))
        )
    ).all()
    for value in values:
        state = RunState.model_validate(value.get("value", value))
        for turn_id, usage in state.turn_usage.items():
            model = _catalog_model(usage.configured_model or usage.model_name)
            if model is None:
                logger.warning(
                    "Agent session %s: no catalog price for %r; its usage is not recorded",
                    state.session_id,
                    usage.configured_model or usage.model_name,
                )
                continue
            await record_usage(
                session,
                operation_key=f"skrift:{state.session_id}:{turn_id}"[:200],
                product_mode=owner.product_mode,
                operation_type=owner.operation_type,
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                user_id=owner.user_id,
                conversation_id=owner.conversation_id,
                details={"agent": state.agent_name},
            )


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
    usage: UsageOwner | None = None,
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
            if usage is not None:
                await _record_usage(session, state_keys, usage)
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
