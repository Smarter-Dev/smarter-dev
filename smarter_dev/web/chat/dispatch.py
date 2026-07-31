"""Transactional worker outbox used by Chat and Resources.

Application rows and their dispatch record commit together.  Queue submission is
then retried with a fresh worker job id on every attempt, so a crash between the
worker state-store write and queue insertion cannot strand the application row.
Workers remain idempotent by their durable aggregate id.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID
from uuid import uuid4

from pydantic import BaseModel
from skrift.hooks import APP_SHUTDOWN
from skrift.hooks import APP_STARTUP
from skrift.hooks import action
from skrift.workers import registry as worker_registry
from skrift.workers import submit as worker_submit
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.models import AccountDeletionRequest
from smarter_dev.web.models import ResourceAgentRun
from smarter_dev.web.models import WebChatSubagent
from smarter_dev.web.models import WebChatTurn
from smarter_dev.web.models import WorkDispatch

logger = logging.getLogger(__name__)
_dispatcher_task: asyncio.Task | None = None


class _ChatTurnSubmission(BaseModel):
    turn_id: str


class _ChatSubagentSubmission(BaseModel):
    subagent_id: str


class _ChatAccountDeletionSubmission(BaseModel):
    request_id: str


class _ResourcesSubmission(BaseModel):
    run_id: str | None = None
    conversation_id: str | None = None
    owner_user_id: str | None = None
    question: str | None = None


async def _submission_only_handler(_payload: BaseModel) -> None:
    raise RuntimeError("submission-only handler cannot execute in the web process")


_SUBMISSION_DESCRIPTORS = {
    "chat.turn.run": (_ChatTurnSubmission, "agents", 1800.0),
    "chat.subagent.run": (_ChatSubagentSubmission, "chat-subagents", 1200.0),
    "chat.account.delete": (_ChatAccountDeletionSubmission, "agents", 600.0),
    "resources.agent.run": (_ResourcesSubmission, "agents", 600.0),
}


def ensure_submission_handler(job_type: str) -> None:
    """Register the lightweight descriptor Skrift requires before enqueueing.

    Worker processes already hold the real handlers. The web process needs only
    matching payload/queue metadata; importing full agent jobs here would waste
    scarce web-pod memory.
    """
    try:
        worker_registry.get(job_type)
        return
    except KeyError:
        pass
    config = _SUBMISSION_DESCRIPTORS.get(job_type)
    if config is None:
        raise KeyError(f"No worker submission descriptor for {job_type!r}")
    payload_model, queue, visibility_timeout = config
    worker_registry.register(
        job_type,
        _submission_only_handler,
        payload_model=payload_model,
        queue=queue,
        max_attempts=5,
        visibility_timeout=visibility_timeout,
    )


async def create_dispatch(
    session: AsyncSession,
    *,
    job_type: str,
    aggregate_id: UUID,
    payload: dict,
    queue: str = "agents",
) -> WorkDispatch:
    existing = await session.scalar(
        select(WorkDispatch).where(
            WorkDispatch.job_type == job_type,
            WorkDispatch.aggregate_id == aggregate_id,
        )
    )
    if existing is not None:
        if existing.status == "cancelled":
            existing.status = "pending"
            existing.next_attempt_at = datetime.now(UTC)
        return existing
    row = WorkDispatch(
        job_type=job_type,
        aggregate_id=aggregate_id,
        payload=payload,
        queue=queue,
        status="pending",
    )
    session.add(row)
    await session.flush()
    return row


async def dispatch_one(dispatch_id: UUID) -> bool:
    """Attempt one outbox handoff.  Failure remains durable and retryable."""
    async with get_db_session_context() as session:
        row = await session.scalar(
            select(WorkDispatch).where(WorkDispatch.id == dispatch_id).with_for_update()
        )
        if row is None or row.status in {"dispatched", "cancelled", "complete"}:
            return row is not None and row.status in {"dispatched", "complete"}
        now = datetime.now(UTC)
        if row.next_attempt_at > now:
            return False
        row.attempt_count += 1
        # A fresh queue id repairs Skrift's own state-write/queue-insert failure
        # window.  The aggregate id in payload remains the idempotency key.
        worker_job_id = f"outbox:{row.id}:{row.attempt_count}:{uuid4().hex[:8]}"
        job_type, payload, queue = row.job_type, dict(row.payload), row.queue
        try:
            # Skrift validates the handler descriptor in the submitting process
            # before writing to the durable queue. Worker imports alone do not
            # populate the web ASGI process's registry.
            ensure_submission_handler(job_type)
            await worker_submit(
                job_type,
                payload,
                queue=queue,
                job_id=worker_job_id,
            )
        except Exception as exc:  # queue/state backend failures are retried
            row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            delay = min(2 ** min(row.attempt_count, 8), 300)
            row.next_attempt_at = now + timedelta(seconds=delay)
            await session.commit()
            logger.warning(
                "worker outbox dispatch failed for %s", row.id, exc_info=True
            )
            return False
        row.status = "dispatched"
        row.worker_job_id = worker_job_id
        row.dispatched_at = now
        row.last_error = None
        if job_type == "chat.subagent.run":
            child = await session.get(WebChatSubagent, row.aggregate_id)
            if child is not None:
                child.job_id = worker_job_id
        await session.commit()
        return True


async def requeue_stale_dispatches() -> int:
    """Repair active aggregates whose dispatched worker lease disappeared."""
    now = datetime.now(UTC)
    unclaimed_before = now - timedelta(minutes=2)
    legacy_resource_before = now - timedelta(minutes=12)
    repaired = 0
    async with get_db_session_context() as session:
        rows = list(
            (
                await session.execute(
                    select(WorkDispatch)
                    .where(WorkDispatch.status == "dispatched")
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for row in rows:
            stale = False
            if row.job_type == "chat.turn.run":
                aggregate = await session.get(WebChatTurn, row.aggregate_id)
                if aggregate is None or aggregate.status in {
                    "complete",
                    "stopped",
                    "error",
                    "usage_limited",
                    "selection_required",
                }:
                    row.status = "complete"
                    continue
                stale = bool(
                    aggregate
                    and aggregate.status
                    in {"submitted", "queued", "running", "stopping"}
                    and (
                        (
                            aggregate.worker_lease_expires_at is None
                            and row.dispatched_at is not None
                            and row.dispatched_at <= unclaimed_before
                        )
                        or (
                            aggregate.worker_lease_expires_at is not None
                            and aggregate.worker_lease_expires_at <= now
                        )
                    )
                )
            elif row.job_type == "chat.subagent.run":
                aggregate = await session.get(WebChatSubagent, row.aggregate_id)
                if aggregate is None or aggregate.status in {
                    "complete",
                    "error",
                    "cancelled",
                    "usage_limited",
                    "lease_lost",
                }:
                    row.status = "complete"
                    continue
                stale = bool(
                    aggregate
                    and aggregate.status in {"queued", "running"}
                    and (
                        (
                            aggregate.lease_expires_at is None
                            and row.dispatched_at is not None
                            and row.dispatched_at <= unclaimed_before
                        )
                        or (
                            aggregate.lease_expires_at is not None
                            and aggregate.lease_expires_at <= now
                        )
                    )
                )
            elif row.job_type == "resources.agent.run":
                aggregate = await session.get(ResourceAgentRun, row.aggregate_id)
                if aggregate is None or aggregate.status in {
                    "complete",
                    "error",
                    "cancelled",
                }:
                    row.status = "complete"
                    continue
                stale = bool(
                    aggregate
                    and aggregate.status in {"submitted", "running"}
                    and (
                        (
                            aggregate.worker_lease_expires_at is None
                            and row.dispatched_at is not None
                            and row.dispatched_at <= legacy_resource_before
                        )
                        or (
                            aggregate.worker_lease_expires_at is not None
                            and aggregate.worker_lease_expires_at <= now
                        )
                    )
                )
            elif row.job_type == "chat.account.delete":
                aggregate = await session.get(AccountDeletionRequest, row.aggregate_id)
                if aggregate is None or aggregate.status == "complete":
                    row.status = "complete"
                    continue
                stale = bool(
                    aggregate
                    and aggregate.status in {"pending", "error"}
                    and row.dispatched_at is not None
                    and row.dispatched_at <= unclaimed_before
                )
            if stale:
                row.status = "pending"
                row.next_attempt_at = now
                row.worker_job_id = None
                repaired += 1
        await session.commit()
    return repaired


async def dispatch_pending(*, limit: int = 50) -> int:
    now = datetime.now(UTC)
    async with get_db_session_context() as session:
        ids = list(
            (
                await session.execute(
                    select(WorkDispatch.id)
                    .where(
                        WorkDispatch.status == "pending",
                        WorkDispatch.next_attempt_at <= now,
                    )
                    .order_by(WorkDispatch.created_at)
                    .limit(limit)
                )
            ).scalars()
        )
    dispatched = 0
    for dispatch_id in ids:
        dispatched += int(await dispatch_one(dispatch_id))
    return dispatched


async def cancel_dispatch(
    session: AsyncSession, *, job_type: str, aggregate_id: UUID
) -> tuple[str | None, bool]:
    row = await session.scalar(
        select(WorkDispatch)
        .where(
            WorkDispatch.job_type == job_type,
            WorkDispatch.aggregate_id == aggregate_id,
        )
        .with_for_update()
    )
    if row is None:
        return None, False
    pending_cancelled = row.status == "pending"
    if pending_cancelled:
        row.status = "cancelled"
    return row.worker_job_id, pending_cancelled


async def _dispatcher_loop() -> None:
    next_attachment_cleanup = 0.0
    while True:
        try:
            await requeue_stale_dispatches()
            await dispatch_pending()
            now = asyncio.get_running_loop().time()
            if now >= next_attachment_cleanup:
                from smarter_dev.web.chat.attachments import cleanup_orphan_attachments
                from smarter_dev.web.chat.usage import reconcile_discord_usage

                next_attachment_cleanup = now + 3600
                await cleanup_orphan_attachments()
                async with get_db_session_context() as session:
                    await reconcile_discord_usage(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker outbox or attachment reconciliation failed")
        await asyncio.sleep(1.0)


@action(APP_STARTUP)
async def start_dispatcher(_app) -> None:
    global _dispatcher_task
    if _dispatcher_task is None or _dispatcher_task.done():
        _dispatcher_task = asyncio.create_task(
            _dispatcher_loop(), name="smarter-dev-worker-outbox"
        )


@action(APP_SHUTDOWN)
async def stop_dispatcher(_app) -> None:
    global _dispatcher_task
    if _dispatcher_task is not None:
        _dispatcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await _dispatcher_task
        _dispatcher_task = None
