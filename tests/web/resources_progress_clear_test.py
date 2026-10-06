"""A finished Resources run deletes its stored progress notifications (task #71).

The job runs as the worker runs it, against the test database, with the agent
stubbed (``RESOURCE_AGENT_STUB=1``) so nothing reaches a provider. Skrift's
notification service stores into ``stored_notifications`` through the
``RedisBackend`` ``app.yaml`` names, unstarted, so nothing is published.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
import skrift.notifications as skrift_notifications
from skrift.db.models.notification import StoredNotification
from skrift.db.models.user import User
from skrift.lib.notification_backends import RedisBackend
from skrift.notifications import NotificationService
from skrift.notifications import notify_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smarter_dev.web import resources_jobs
from smarter_dev.web.models import AgentConversation
from smarter_dev.web.models import AgentMessage
from smarter_dev.web.models import ResourceAgentRun
from smarter_dev.web.resources_jobs import ResourcesRunPayload
from smarter_dev.web.resources_jobs import run_resources_job

_WORDS = "how do I size a connection pool for a burst of webhooks"


@pytest.fixture
async def service(test_engine, monkeypatch):
    session_maker = async_sessionmaker(test_engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_context():
        async with session_maker() as session:
            yield session

    settings = SimpleNamespace(
        redis=SimpleNamespace(url="", make_key=lambda *parts: ":".join(parts)),
        notifications=SimpleNamespace(
            queued_ttl_seconds=18000, timeseries_ttl_seconds=604800
        ),
    )
    service = NotificationService()
    service.set_backend(RedisBackend(settings=settings, session_maker=session_maker))
    monkeypatch.setattr(skrift_notifications, "notifications", service)
    monkeypatch.setattr(resources_jobs, "get_db_session_context", session_context)
    monkeypatch.setenv("RESOURCE_AGENT_STUB", "1")
    return service


async def _run(db_session, *, attempt_count: int = 0, owner=None):
    """A submitted run, and the progress it sent; returns the owner and run ids."""
    connection = await db_session.connection()
    await connection.run_sync(lambda sync: User.metadata.create_all(sync))
    if owner is None:
        user = User(email=f"{uuid4().hex}@example.test", name="Member", is_active=True)
        db_session.add(user)
        await db_session.flush()
        owner = user.id
    conversation = AgentConversation(
        owner_user_id=owner, agent_type="resources", title="Pool sizing"
    )
    db_session.add(conversation)
    await db_session.flush()
    run = ResourceAgentRun(
        conversation_id=conversation.id,
        owner_user_id=owner,
        user_sequence=1,
        submission_key=uuid4().hex[:16],
        question=_WORDS,
        status="submitted",
        attempt_count=attempt_count,
    )
    db_session.add_all(
        [
            AgentMessage(
                conversation_id=conversation.id, sequence=1, role="user", content=_WORDS
            ),
            run,
        ]
    )
    await db_session.flush()
    user_id, conversation_id, run_id = owner, conversation.id, run.id
    await db_session.commit()
    # The progress the pipeline sent while it worked.
    await notify_user(
        str(user_id),
        "agent_reframe_ready",
        conversation_id=str(conversation_id),
        message=_WORDS,
    )
    await notify_user(
        str(user_id),
        "agent_tool_event",
        conversation_id=str(conversation_id),
        tool="search",
        label="Searching",
        summary=_WORDS,
    )
    return user_id, run_id


async def _stored(db_session, source_key: str) -> list[str]:
    db_session.expire_all()
    rows = await db_session.scalars(
        select(StoredNotification.payload_json).where(
            StoredNotification.source_key == source_key
        )
    )
    return list(rows)


@pytest.mark.asyncio
async def test_a_finished_run_deletes_its_stored_progress(service, db_session):
    user_id, run_id = await _run(db_session)
    other_user = f"user:{uuid4()}"
    await service.send(
        other_user,
        skrift_notifications.Notification(type="agent_tool_event", payload={"x": 1}),
    )
    assert len(await _stored(db_session, f"user:{user_id}")) == 2

    result = await run_resources_job(ResourcesRunPayload(run_id=str(run_id)))

    assert result["status"] == "ok"
    assert await _stored(db_session, f"user:{user_id}") == []
    # Another member's queue is not touched.
    assert len(await _stored(db_session, other_user)) == 1


@pytest.mark.asyncio
async def test_another_run_of_the_same_owner_keeps_its_progress(service, db_session):
    user_id, first_run = await _run(db_session)
    _, second_run = await _run(db_session, owner=user_id)
    second = await db_session.get(ResourceAgentRun, second_run)
    second_conversation = str(second.conversation_id)
    assert len(await _stored(db_session, f"user:{user_id}")) == 4

    result = await run_resources_job(ResourcesRunPayload(run_id=str(first_run)))

    assert result["status"] == "ok"
    # A reconnect while the second run works still replays its steps.
    replayed = await service.get_queued(uuid4().hex, str(user_id))
    assert [n.type for n in replayed] == ["agent_reframe_ready", "agent_tool_event"]
    assert {n.payload["conversation_id"] for n in replayed} == {second_conversation}


@pytest.mark.asyncio
async def test_a_failed_run_deletes_its_stored_progress(
    service, db_session, monkeypatch
):
    # The fifth attempt: the run fails for good instead of being retried.
    user_id, run_id = await _run(db_session, attempt_count=4)

    async def broken(*args, **kwargs):
        raise RuntimeError("the model failed")

    monkeypatch.delenv("RESOURCE_AGENT_STUB")
    monkeypatch.setattr(resources_jobs, "run_resources_pipeline", broken)

    result = await run_resources_job(ResourcesRunPayload(run_id=str(run_id)))

    assert result["status"] == "error"
    assert await _stored(db_session, f"user:{user_id}") == []


@pytest.mark.asyncio
async def test_a_run_that_will_be_retried_keeps_its_progress(
    service, db_session, monkeypatch
):
    user_id, run_id = await _run(db_session)

    async def broken(*args, **kwargs):
        raise RuntimeError("the model failed")

    monkeypatch.delenv("RESOURCE_AGENT_STUB")
    monkeypatch.setattr(resources_jobs, "run_resources_pipeline", broken)

    with pytest.raises(RuntimeError, match="transient"):
        await run_resources_job(ResourcesRunPayload(run_id=str(run_id)))

    assert len(await _stored(db_session, f"user:{user_id}")) == 2


@pytest.mark.asyncio
async def test_a_worker_that_lost_its_lease_leaves_the_progress_alone(
    service, db_session, monkeypatch
):
    user_id, run_id = await _run(db_session, attempt_count=4)

    async def taken_over(*args, **kwargs):
        # Another worker claimed the run while this one was working.
        run = await db_session.get(ResourceAgentRun, run_id)
        run.worker_lease_token = "another-worker"
        await db_session.commit()
        raise RuntimeError("the model failed")

    monkeypatch.delenv("RESOURCE_AGENT_STUB")
    monkeypatch.setattr(resources_jobs, "run_resources_pipeline", taken_over)

    result = await run_resources_job(ResourcesRunPayload(run_id=str(run_id)))

    assert result["status"] == "error"
    assert len(await _stored(db_session, f"user:{user_id}")) == 2


@pytest.mark.asyncio
async def test_a_clear_failure_is_logged_and_the_run_still_completes(
    service, db_session, monkeypatch, caplog
):
    user_id, run_id = await _run(db_session)

    async def broken(*args, **kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(resources_jobs, "forget_queued_progress", broken)

    with caplog.at_level(logging.ERROR, logger=resources_jobs.logger.name):
        result = await run_resources_job(ResourcesRunPayload(run_id=str(run_id)))

    assert result["status"] == "ok"
    assert "Clearing Resources progress notifications failed" in caplog.text
    db_session.expire_all()
    assert (await db_session.get(ResourceAgentRun, run_id)).status == "complete"
    # Left for Skrift's lifetime sweep.
    assert len(await _stored(db_session, f"user:{user_id}")) == 2
