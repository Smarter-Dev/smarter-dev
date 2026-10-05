"""A finished job's state is written without its payload, result or paused state.

Jobs run through Skrift's own runtime and SQLAlchemy backends over the test
database, with only the state store swapped for the one ``app.yaml`` names, so
the rows checked are the rows production writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel
from skrift.db.models.worker import WorkerDeadLetterRecord
from skrift.db.models.worker import WorkerQueueRecord
from skrift.db.models.worker import WorkerStateRecord
from skrift.workers.models import JobStatus
from skrift.workers.models import RetryPolicy
from skrift.workers.registry import HandlerRegistry
from skrift.workers.runtime import WorkerConfig
from skrift.workers.runtime import WorkerRuntime
from skrift.workers.sqlalchemy import SQLAlchemyArchive
from skrift.workers.sqlalchemy import SQLAlchemyDeadLetterStore
from skrift.workers.sqlalchemy import SQLAlchemyEventLog
from skrift.workers.sqlalchemy import SQLAlchemyQueue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smarter_dev.web.worker_state_store import FinishedJobStateStore

_WORDS = "what a member typed"
_REPO = Path(__file__).resolve().parents[2]


class _Ask(BaseModel):
    question: str


def _runtime(test_engine, registry: HandlerRegistry) -> WorkerRuntime:
    session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    return WorkerRuntime(
        # As on the agent-worker: submit() queues, a claim runs the job.
        config=WorkerConfig(mode="out_of_process", queues=("agents",)),
        state_store=FinishedJobStateStore(session_maker=session_maker),
        event_log=SQLAlchemyEventLog(session_maker=session_maker),
        queue=SQLAlchemyQueue(session_maker=session_maker),
        dead_letter_store=SQLAlchemyDeadLetterStore(session_maker=session_maker),
        archive=SQLAlchemyArchive(session_maker=session_maker),
        handler_registry=registry,
    )


async def _stored_job_state(db_session, job_id: str) -> dict:
    db_session.expire_all()
    return await db_session.scalar(
        select(WorkerStateRecord.value).where(
            WorkerStateRecord.key == f"workers:jobs:{job_id}"
        )
    )


def _job(stored: dict) -> dict:
    return stored["value"]


@pytest.mark.asyncio
async def test_a_completed_job_keeps_neither_its_payload_nor_its_result(
    test_engine, db_session
):
    registry = HandlerRegistry()

    async def answer(payload: _Ask) -> dict:
        return {"status": "ok", "reply": f"an answer to {payload.question}"}

    registry.register("test.answer", answer, payload_model=_Ask, queue="agents")
    runtime = _runtime(test_engine, registry)

    handle = await runtime.submit("test.answer", {"question": _WORDS}, queue="agents")
    stored = await _stored_job_state(db_session, handle.id)
    assert _job(stored)["job"]["payload"] == {"question": _WORDS}

    claimed = await runtime.queue.claim(["agents"], visibility_timeout=30)
    await runtime.execute_claim(claimed)

    stored = await _stored_job_state(db_session, handle.id)
    assert _job(stored)["status"] == JobStatus.COMPLETED
    assert _job(stored)["job"]["payload"] == {}
    assert _job(stored)["result"] is None
    assert _job(stored)["job"]["type"] == "test.answer"
    assert _WORDS not in str(stored)
    assert await db_session.scalar(select(WorkerQueueRecord.id)) is None


@pytest.mark.asyncio
async def test_a_job_waiting_for_a_retry_keeps_its_payload_until_it_is_dead_lettered(
    test_engine, db_session
):
    registry = HandlerRegistry()

    async def fail(payload: _Ask) -> None:
        raise RuntimeError("provider unavailable")

    registry.register(
        "test.fail",
        fail,
        payload_model=_Ask,
        queue="agents",
        retry_policy=RetryPolicy(max_attempts=2),
    )
    runtime = _runtime(test_engine, registry)
    handle = await runtime.submit("test.fail", {"question": _WORDS}, queue="agents")

    await runtime.execute_claim(
        await runtime.queue.claim(["agents"], visibility_timeout=30)
    )
    stored = await _stored_job_state(db_session, handle.id)
    assert _job(stored)["status"] == JobStatus.SUBMITTED
    assert _job(stored)["job"]["payload"] == {"question": _WORDS}

    await runtime.execute_claim(
        await runtime.queue.claim(["agents"], visibility_timeout=30)
    )
    stored = await _stored_job_state(db_session, handle.id)
    assert _job(stored)["status"] == JobStatus.DEAD_LETTERED
    assert _job(stored)["job"]["payload"] == {}
    assert _WORDS not in str(stored)
    assert _job(stored)["last_error"] == "RuntimeError: provider unavailable"

    # The dead letter is what an operator replays from, so it keeps the payload
    # until the hourly retention job deletes it, and a replay still carries it.
    entry = (await runtime.inspect_dlq())[0]
    assert entry.job.payload == {"question": _WORDS}
    replay = await runtime.retry_dlq_entry(entry.id)
    replayed = await _stored_job_state(db_session, replay.id)
    assert _job(replayed)["job"]["payload"] == {"question": _WORDS}
    assert await db_session.scalar(select(WorkerDeadLetterRecord.id)) is not None


@pytest.mark.asyncio
async def test_a_cancelled_job_keeps_no_payload(test_engine, db_session):
    registry = HandlerRegistry()

    async def never(payload: _Ask) -> None:
        raise AssertionError("a cancelled job does not run")

    registry.register("test.later", never, payload_model=_Ask, queue="agents")
    runtime = _runtime(test_engine, registry)
    handle = await runtime.submit("test.later", {"question": _WORDS}, queue="agents")

    assert await handle.cancel()

    stored = await _stored_job_state(db_session, handle.id)
    assert _job(stored)["status"] == JobStatus.CANCELLED
    assert _job(stored)["job"]["payload"] == {}
    assert _WORDS not in str(stored)


@pytest.mark.asyncio
async def test_other_state_is_written_as_given(test_engine, db_session):
    session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    store = FinishedJobStateStore(session_maker=session_maker)
    await store.set("runstate:abc", {"status": "completed", "output": _WORDS})
    await store.update("workers:queue_wait_history", lambda _: [{"words": _WORDS}])

    assert await store.get("runstate:abc") == {"status": "completed", "output": _WORDS}
    assert await store.get("workers:queue_wait_history") == [{"words": _WORDS}]


@pytest.mark.parametrize("config", ["app.yaml", "app.development.yaml"])
def test_the_app_writes_job_state_through_this_store(config):
    workers = yaml.safe_load((_REPO / config).read_text())["workers"]
    assert workers["backends"]["state_store"] == (
        "smarter_dev.web.worker_state_store:FinishedJobStateStore"
    )
