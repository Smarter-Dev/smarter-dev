"""Transactional outbox behaviour: hand-off, backoff, and stale repair.

SQLite drops ``tzinfo`` on round-trip while production Postgres returns aware
timestamps, so ``dispatch``'s own ``datetime`` is swapped for a naive-UTC clock
here.  Comparison semantics under test are unchanged; only the tz-awareness of
both sides is made consistent for the sqlite-backed fixtures.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from skrift.db.models.user import User
from sqlalchemy import select

from smarter_dev.web.chat import dispatch as chat_dispatch
from smarter_dev.web.chat.dispatch import create_dispatch
from smarter_dev.web.chat.dispatch import dispatch_one
from smarter_dev.web.chat.dispatch import dispatch_pending
from smarter_dev.web.chat.dispatch import requeue_stale_dispatches
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatSubagent
from smarter_dev.web.models import WebChatTurn
from smarter_dev.web.models import WorkDispatch


class _NaiveUTC(datetime):
    """``datetime`` whose ``now()`` matches sqlite's naive storage."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz or UTC).replace(tzinfo=None)


def _now():
    return _NaiveUTC.now()


@pytest.fixture
def outbox(db_session, monkeypatch):
    """Route the outbox at the test session and capture queue submissions."""
    submissions = []

    async def fake_submit(job_type, payload, **kwargs):
        submissions.append({"job_type": job_type, "payload": payload, **kwargs})

    @asynccontextmanager
    async def fake_session_context():
        yield db_session

    monkeypatch.setattr(chat_dispatch, "get_db_session_context", fake_session_context)
    monkeypatch.setattr(chat_dispatch, "worker_submit", fake_submit)
    monkeypatch.setattr(chat_dispatch, "datetime", _NaiveUTC)
    return submissions


async def _seed_user(db_session):
    connection = await db_session.connection()
    await connection.run_sync(
        lambda sync_connection: User.metadata.create_all(sync_connection)
    )
    user = User(email=f"{uuid4().hex}@example.test", name="Chat User", is_active=True)
    db_session.add(user)
    await db_session.commit()
    return user


async def _seed_turn(db_session, user, **kwargs):
    """One conversation per turn: only one turn per conversation may be active."""
    conversation = WebChatConversation(
        owner_user_id=user.id,
        intelligence_mode="efficient",
        selected_model_key="gpt-5-6-luna",
        reasoning_level="medium",
        title="New Chat",
        status="running",
    )
    db_session.add(conversation)
    await db_session.flush()
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=1,
        submission_key=f"submission-{uuid4().hex[:8]}",
        response_version_group=uuid4(),
        response_sequence=2,
        model_key=conversation.selected_model_key,
        reasoning_level=conversation.reasoning_level,
        **kwargs,
    )
    db_session.add(turn)
    await db_session.commit()
    return turn


def _row(
    *,
    job_type="chat.turn.run",
    aggregate_id=None,
    status="pending",
    queue="agents",
    payload=None,
    **kwargs,
):
    aggregate_id = aggregate_id or uuid4()
    return WorkDispatch(
        job_type=job_type,
        aggregate_id=aggregate_id,
        payload=payload or {"turn_id": str(aggregate_id)},
        queue=queue,
        status=status,
        next_attempt_at=kwargs.pop("next_attempt_at", _now()),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_dispatcher_hands_only_due_pending_rows_to_the_queue(db_session, outbox):
    due = _row()
    scheduled = _row(next_attempt_at=_now() + timedelta(minutes=5))
    cancelled = _row(status="cancelled")
    already = _row(
        status="dispatched", worker_job_id="outbox:old", dispatched_at=_now()
    )
    db_session.add_all([due, scheduled, cancelled, already])
    await db_session.commit()

    assert await dispatch_pending() == 1

    assert [item["payload"] for item in outbox] == [due.payload]
    assert due.status == "dispatched"
    assert due.attempt_count == 1
    assert due.worker_job_id.startswith(f"outbox:{due.id}:1:")
    assert due.dispatched_at is not None
    assert outbox[0]["job_id"] == due.worker_job_id
    assert outbox[0]["queue"] == "agents"
    assert scheduled.status == "pending"
    assert cancelled.status == "cancelled"
    assert already.worker_job_id == "outbox:old"


@pytest.mark.asyncio
async def test_cancelled_dispatch_is_never_handed_to_the_queue(db_session, outbox):
    cancelled = _row(status="cancelled")
    db_session.add(cancelled)
    await db_session.commit()

    assert await dispatch_one(cancelled.id) is False

    assert outbox == []
    assert cancelled.status == "cancelled"
    assert cancelled.attempt_count == 0
    assert cancelled.worker_job_id is None


@pytest.mark.asyncio
async def test_already_dispatched_row_is_reported_done_without_resubmitting(
    db_session, outbox
):
    row = _row(status="dispatched", worker_job_id="outbox:old", dispatched_at=_now())
    missing = uuid4()
    db_session.add(row)
    await db_session.commit()

    assert await dispatch_one(row.id) is True
    assert await dispatch_one(missing) is False
    assert outbox == []


@pytest.mark.asyncio
async def test_queue_failure_keeps_the_row_pending_with_backoff(
    db_session, outbox, monkeypatch
):
    async def explode(*_args, **_kwargs):
        raise RuntimeError("queue backend unavailable")

    monkeypatch.setattr(chat_dispatch, "worker_submit", explode)
    row = _row()
    db_session.add(row)
    await db_session.commit()

    assert await dispatch_one(row.id) is False

    assert row.status == "pending"
    assert row.attempt_count == 1
    assert row.worker_job_id is None
    assert "queue backend unavailable" in row.last_error
    assert row.next_attempt_at > _now()
    # The freshly scheduled retry is not yet due, so the loop leaves it alone.
    assert await dispatch_pending() == 0
    assert outbox == []


@pytest.mark.asyncio
async def test_subagent_dispatch_records_the_worker_job_id_on_the_child(
    db_session, outbox
):
    user = await _seed_user(db_session)
    turn = await _seed_turn(db_session, user, status="running")
    child = WebChatSubagent(
        root_turn_id=turn.id,
        name="researcher",
        task="find things",
        spawn_ordinal=1,
        status="queued",
    )
    db_session.add(child)
    await db_session.commit()
    row = _row(
        job_type="chat.subagent.run",
        aggregate_id=child.id,
        queue="chat-subagents",
        payload={"subagent_id": str(child.id)},
    )
    db_session.add(row)
    await db_session.commit()

    assert await dispatch_one(row.id) is True

    await db_session.refresh(child)
    assert child.job_id == row.worker_job_id
    assert outbox[0]["queue"] == "chat-subagents"


@pytest.mark.asyncio
async def test_expired_turn_lease_is_redispatched(db_session, outbox):
    user = await _seed_user(db_session)
    stale_turn = await _seed_turn(
        db_session,
        user,
        status="running",
        worker_lease_token="dead-worker",
        worker_lease_expires_at=_now() - timedelta(seconds=30),
    )
    live_turn = await _seed_turn(
        db_session,
        user,
        status="running",
        worker_lease_token="live-worker",
        worker_lease_expires_at=_now() + timedelta(seconds=600),
    )
    stale = _row(
        aggregate_id=stale_turn.id,
        status="dispatched",
        worker_job_id="outbox:stale",
        dispatched_at=_now() - timedelta(minutes=30),
    )
    live = _row(
        aggregate_id=live_turn.id,
        status="dispatched",
        worker_job_id="outbox:live",
        dispatched_at=_now() - timedelta(minutes=30),
    )
    db_session.add_all([stale, live])
    await db_session.commit()

    assert await requeue_stale_dispatches() == 1

    assert stale.status == "pending"
    assert stale.worker_job_id is None
    assert live.status == "dispatched" and live.worker_job_id == "outbox:live"

    assert await dispatch_pending() == 1
    assert [item["payload"] for item in outbox] == [stale.payload]
    assert stale.status == "dispatched"


@pytest.mark.asyncio
async def test_unclaimed_dispatch_is_repaired_only_after_the_grace_period(
    db_session, outbox
):
    user = await _seed_user(db_session)
    recent_turn = await _seed_turn(db_session, user, status="submitted")
    old_turn = await _seed_turn(db_session, user, status="submitted")
    recent = _row(
        aggregate_id=recent_turn.id,
        status="dispatched",
        dispatched_at=_now() - timedelta(seconds=30),
    )
    old = _row(
        aggregate_id=old_turn.id,
        status="dispatched",
        dispatched_at=_now() - timedelta(minutes=5),
    )
    db_session.add_all([recent, old])
    await db_session.commit()

    assert await requeue_stale_dispatches() == 1

    assert recent.status == "dispatched"
    assert old.status == "pending" and old.next_attempt_at <= _now()


@pytest.mark.asyncio
async def test_finished_or_missing_aggregates_close_their_dispatch(db_session, outbox):
    user = await _seed_user(db_session)
    finished_turn = await _seed_turn(
        db_session,
        user,
        status="complete",
        worker_lease_expires_at=_now() - timedelta(minutes=1),
    )
    finished = _row(
        aggregate_id=finished_turn.id,
        status="dispatched",
        dispatched_at=_now() - timedelta(minutes=30),
    )
    orphan = _row(status="dispatched", dispatched_at=_now() - timedelta(minutes=30))
    db_session.add_all([finished, orphan])
    await db_session.commit()

    assert await requeue_stale_dispatches() == 0

    assert finished.status == "complete"
    assert orphan.status == "complete"
    assert await dispatch_pending() == 0
    assert outbox == []


@pytest.mark.asyncio
async def test_cancelled_dispatch_is_not_repaired_even_with_an_expired_lease(
    db_session, outbox
):
    user = await _seed_user(db_session)
    turn = await _seed_turn(
        db_session,
        user,
        status="running",
        worker_lease_token="dead-worker",
        worker_lease_expires_at=_now() - timedelta(minutes=1),
    )
    cancelled = _row(
        aggregate_id=turn.id,
        status="cancelled",
        worker_job_id="outbox:cancelled",
        dispatched_at=_now() - timedelta(minutes=30),
    )
    db_session.add(cancelled)
    await db_session.commit()

    assert await requeue_stale_dispatches() == 0
    assert await dispatch_pending() == 0

    assert cancelled.status == "cancelled"
    assert cancelled.worker_job_id == "outbox:cancelled"
    assert outbox == []


@pytest.mark.asyncio
async def test_expired_subagent_lease_is_redispatched_but_terminal_children_are_not(
    db_session, outbox
):
    user = await _seed_user(db_session)
    turn = await _seed_turn(db_session, user, status="running")
    running = WebChatSubagent(
        root_turn_id=turn.id,
        name="stale-child",
        task="find things",
        spawn_ordinal=1,
        status="running",
        lease_expires_at=_now() - timedelta(seconds=5),
    )
    done = WebChatSubagent(
        root_turn_id=turn.id,
        name="finished-child",
        task="already done",
        spawn_ordinal=2,
        status="complete",
        lease_expires_at=_now() - timedelta(seconds=5),
    )
    db_session.add_all([running, done])
    await db_session.commit()
    stale = _row(
        job_type="chat.subagent.run",
        aggregate_id=running.id,
        status="dispatched",
        dispatched_at=_now() - timedelta(minutes=30),
    )
    terminal = _row(
        job_type="chat.subagent.run",
        aggregate_id=done.id,
        status="dispatched",
        dispatched_at=_now() - timedelta(minutes=30),
    )
    db_session.add_all([stale, terminal])
    await db_session.commit()

    assert await requeue_stale_dispatches() == 1

    assert stale.status == "pending"
    assert terminal.status == "complete"


@pytest.mark.asyncio
async def test_create_dispatch_is_idempotent_and_revives_a_cancelled_row(
    db_session, outbox
):
    aggregate_id = uuid4()
    first = await create_dispatch(
        db_session,
        job_type="chat.turn.run",
        aggregate_id=aggregate_id,
        payload={"turn_id": str(aggregate_id)},
    )
    await db_session.commit()
    second = await create_dispatch(
        db_session,
        job_type="chat.turn.run",
        aggregate_id=aggregate_id,
        payload={"turn_id": str(aggregate_id)},
    )
    await db_session.commit()
    assert first.id == second.id
    assert len(list((await db_session.execute(select(WorkDispatch))).scalars())) == 1

    first.status = "cancelled"
    await db_session.commit()
    revived = await create_dispatch(
        db_session,
        job_type="chat.turn.run",
        aggregate_id=aggregate_id,
        payload={"turn_id": str(aggregate_id)},
    )
    await db_session.commit()

    assert revived.id == first.id
    assert revived.status == "pending"
    assert await dispatch_pending() == 1
