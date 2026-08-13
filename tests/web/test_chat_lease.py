"""Worker lease protocol: heartbeat extension and takeover fencing.

A turn is executed by exactly one worker at a time, identified by
``WebChatTurn.worker_lease_token``.  The owning worker refreshes
``worker_lease_expires_at``; once a replacement claims the turn every guarded
write from the superseded worker must fail with ``RunSuperseded`` instead of
corrupting the new run's durable state.

SQLite drops ``tzinfo`` on round-trip while production Postgres returns aware
timestamps, so the modules under test get a naive-UTC clock here.  The
comparisons under test are unchanged; only both sides' tz-awareness is aligned.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from skrift.db.models.user import User

from smarter_dev.shared.model_catalog import get_model
from smarter_dev.web.chat import jobs as chat_jobs
from smarter_dev.web.chat import runtime as chat_runtime
from smarter_dev.web.chat.runtime import HardSpendCutoff
from smarter_dev.web.chat.runtime import MeteringContext
from smarter_dev.web.chat.runtime import RunSuperseded
from smarter_dev.web.chat.runtime import SpendMeteredModel
from smarter_dev.web.chat.settings import ensure_settings
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatTurn


class _NaiveUTC(datetime):
    """``datetime`` whose ``now()`` matches sqlite's naive storage."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz or UTC).replace(tzinfo=None)


def _now():
    return _NaiveUTC.now()


@pytest.fixture
def chat_db(db_session, monkeypatch):
    """Point the worker and runtime seams at the test session."""

    @asynccontextmanager
    async def fake_session_context():
        yield db_session

    monkeypatch.setattr(chat_jobs, "get_db_session_context", fake_session_context)
    monkeypatch.setattr(chat_runtime, "get_db_session_context", fake_session_context)
    monkeypatch.setattr(chat_jobs, "datetime", _NaiveUTC)
    return db_session


async def _seed_turn(db_session, *, status="running", **kwargs):
    connection = await db_session.connection()
    await connection.run_sync(
        lambda sync_connection: User.metadata.create_all(sync_connection)
    )
    user = User(email=f"{uuid4().hex}@example.test", name="Chat User", is_active=True)
    db_session.add(user)
    await db_session.flush()
    await ensure_settings(db_session)
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
        status=status,
        **kwargs,
    )
    db_session.add(turn)
    await db_session.commit()
    return user, conversation, turn


def _stub_heartbeat_interval(monkeypatch):
    """Run heartbeat iterations without waiting on the ten second interval."""
    real_sleep = asyncio.sleep
    ticks = []

    async def fake_sleep(delay, *args, **kwargs):
        if delay != 10:
            return await real_sleep(delay, *args, **kwargs)
        ticks.append(delay)
        if len(ticks) > 1:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return ticks


@pytest.mark.asyncio
async def test_heartbeat_extends_the_worker_lease(chat_db, monkeypatch):
    _user, _conversation, turn = await _seed_turn(
        chat_db,
        status="running",
        worker_lease_token="worker-1",
        worker_lease_expires_at=_now(),
    )
    before = turn.worker_lease_expires_at
    ticks = _stub_heartbeat_interval(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await chat_jobs._heartbeat_turn(turn.id, "worker-1")

    await chat_db.refresh(turn)
    refreshed = turn
    assert len(ticks) == 2
    assert refreshed.worker_lease_expires_at > before
    assert (refreshed.worker_lease_expires_at - _now()).total_seconds() > 900


@pytest.mark.asyncio
async def test_heartbeat_stops_after_a_takeover(chat_db, monkeypatch):
    _user, _conversation, turn = await _seed_turn(
        chat_db,
        status="running",
        worker_lease_token="worker-2",
        worker_lease_expires_at=_now(),
    )
    before = turn.worker_lease_expires_at
    ticks = _stub_heartbeat_interval(monkeypatch)

    # The superseded worker still heartbeats with its own, now stale, token.
    assert await chat_jobs._heartbeat_turn(turn.id, "worker-1") is None

    await chat_db.refresh(turn)
    refreshed = turn
    assert ticks == [10]
    assert refreshed.worker_lease_expires_at == before
    assert refreshed.worker_lease_token == "worker-2"


@pytest.mark.asyncio
async def test_heartbeat_stops_once_the_turn_leaves_running(chat_db, monkeypatch):
    _user, _conversation, turn = await _seed_turn(
        chat_db,
        status="complete",
        worker_lease_token="worker-1",
        worker_lease_expires_at=_now(),
    )
    _stub_heartbeat_interval(monkeypatch)

    assert await chat_jobs._heartbeat_turn(turn.id, "worker-1") is None


@pytest.mark.asyncio
async def test_expired_lease_is_taken_over_and_the_old_worker_is_told(
    chat_db, monkeypatch
):
    _user, conversation, turn = await _seed_turn(
        chat_db,
        status="running",
        worker_lease_token="dead-worker",
        worker_lease_expires_at=_now() - chat_jobs.timedelta(seconds=1),
    )
    published = []

    async def fake_publish(turn_id, *, reason, worker_lease_token=None, **_kwargs):
        published.append((turn_id, reason, worker_lease_token))

    monkeypatch.setattr(chat_jobs, "publish_cancellation", fake_publish)

    claimed_turn, claimed_conversation, owner_id, token = await chat_jobs._claim_turn(
        turn.id
    )

    assert token not in {"terminal", "owned"}
    assert claimed_turn.id == turn.id
    assert claimed_conversation.id == conversation.id
    assert owner_id == conversation.owner_user_id
    await chat_db.refresh(turn)
    refreshed = turn
    assert refreshed.worker_lease_token == token != "dead-worker"
    assert refreshed.worker_lease_expires_at > _now()
    assert refreshed.attempt_count == 1
    assert published == [(turn.id, "superseded", "dead-worker")]


@pytest.mark.asyncio
async def test_live_lease_is_not_stolen_by_a_duplicate_delivery(chat_db):
    _user, _conversation, turn = await _seed_turn(
        chat_db,
        status="running",
        worker_lease_token="live-worker",
        worker_lease_expires_at=_now() + chat_jobs.timedelta(seconds=600),
    )

    _turn, _conversation_row, _owner_id, token = await chat_jobs._claim_turn(turn.id)

    assert token == "owned"
    await chat_db.refresh(turn)
    refreshed = turn
    assert refreshed.worker_lease_token == "live-worker"
    assert refreshed.attempt_count == 0


@pytest.mark.asyncio
async def test_superseded_worker_cannot_write_a_terminal_error(chat_db):
    _user, _conversation, turn = await _seed_turn(
        chat_db,
        status="running",
        worker_lease_token="new-worker",
        worker_lease_expires_at=_now() + chat_jobs.timedelta(seconds=600),
    )

    assert (
        await chat_jobs._terminal_error(
            turn.id, "stale worker gave up", expected_lease_token="old-worker"
        )
        is None
    )

    await chat_db.refresh(turn)
    refreshed = turn
    assert refreshed.status == "running"
    assert refreshed.finished_at is None
    assert refreshed.worker_lease_token == "new-worker"


@pytest.mark.asyncio
async def test_superseded_worker_cannot_consume_the_final_response(chat_db):
    _user, conversation, turn = await _seed_turn(
        chat_db,
        status="running",
        worker_lease_token="new-worker",
        worker_lease_expires_at=_now() + chat_jobs.timedelta(seconds=600),
    )
    stale = SimpleNamespace(id=turn.id, worker_lease_token="old-worker")

    with pytest.raises(RunSuperseded):
        await chat_jobs._final_response(
            model=get_model("gemini-3-5-flash-lite"),
            reasoning=None,
            prompt="anything",
            history=[],
            turn=stale,
            conversation=conversation,
            owner_id=conversation.owner_user_id,
            tier="r",
        )

    await chat_db.refresh(turn)
    refreshed = turn
    assert refreshed.final_response_used is False


@pytest.mark.asyncio
async def test_durable_cancellation_check_detects_a_takeover(chat_db):
    _user, _conversation, turn = await _seed_turn(
        chat_db, status="running", worker_lease_token="new-worker"
    )

    with pytest.raises(RunSuperseded):
        await chat_runtime._check_durable_cancellation_state(
            turn_id=turn.id,
            subagent_id=None,
            worker_lease_token="old-worker",
            allow_cutoff=False,
        )

    # The owning worker keeps running.
    assert (
        await chat_runtime._check_durable_cancellation_state(
            turn_id=turn.id,
            subagent_id=None,
            worker_lease_token="new-worker",
            allow_cutoff=False,
        )
        is None
    )


def _metered(turn, conversation, owner_id, *, worker_lease_token):
    metered = object.__new__(SpendMeteredModel)
    metered.wrapped = None  # count_tokens failure falls back to a size estimate
    metered.request_ordinal = 0
    metered.context = MeteringContext(
        model=get_model("gemini-3-5-flash-lite"),
        owner_id=owner_id,
        conversation_id=conversation.id,
        turn_id=turn.id,
        intelligence_mode="efficient",
        operation_type="primary",
        operation_prefix=f"chat:{turn.id}:primary",
        worker_lease_token=worker_lease_token,
    )
    return metered


@pytest.mark.asyncio
async def test_metered_provider_request_is_refused_after_a_takeover(chat_db):
    user, conversation, turn = await _seed_turn(
        chat_db, status="running", worker_lease_token="new-worker"
    )
    stale = _metered(turn, conversation, user.id, worker_lease_token="old-worker")

    with pytest.raises(RunSuperseded):
        await stale._reserve("chat:stale:request:1", [], None, None)

    # The current lease holder passes the fencing check and fails later, on
    # entitlement, proving the guard is lease-specific rather than blanket.
    current = _metered(turn, conversation, user.id, worker_lease_token="new-worker")
    with pytest.raises(HardSpendCutoff) as exc:
        await current._reserve("chat:current:request:1", [], None, None)
    assert "entitlement" in str(exc.value)


@pytest.mark.asyncio
async def test_metering_execution_key_is_scoped_to_the_lease(chat_db):
    _user, conversation, turn = await _seed_turn(
        chat_db, status="running", worker_lease_token="new-worker"
    )
    first = _metered(turn, conversation, uuid4(), worker_lease_token="worker-a")
    second = _metered(turn, conversation, uuid4(), worker_lease_token="worker-b")

    first_key, second_key = first._next_key(), second._next_key()
    assert first_key != second_key
    assert "worker-a" in first_key and "worker-b" in second_key
    assert first_key.endswith("request:1")
    assert first._next_key().endswith("request:2")
