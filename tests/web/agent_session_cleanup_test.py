"""An agent run's copy in Skrift's worker tables is deleted once its caller has the result.

Sessions are written by Skrift's own session-state code (run state, snapshots,
the event stream) through its SQLAlchemy backends over the test database, with
the state store ``app.yaml`` names. Only the model loop is skipped: each stage
records its prompt and an output of its type and completes, as Skrift's runner
does. Skrift 0.2.1a1's runner cannot run against the locked pydantic-ai 2.x
(it needs <2.0), so the loop itself is not exercised here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
import skrift.workers.runtime as worker_runtime
from skrift.agents.models import AgentUsageRecord
from skrift.agents.models import RunState
from skrift.agents.session import Session
from skrift.agents.state import append_event
from skrift.agents.state import create_or_update_runstate
from skrift.agents.state import drain_outbox
from skrift.agents.state import update_runstate
from skrift.config import AgentsConfig
from skrift.config import WorkersConfig
from skrift.db.models.worker import WorkerArchiveEventRecord
from skrift.db.models.worker import WorkerArchiveSnapshotRecord
from skrift.db.models.worker import WorkerDeadLetterRecord
from skrift.db.models.worker import WorkerEventRecord
from skrift.db.models.worker import WorkerQueueRecord
from skrift.db.models.worker import WorkerStateRecord
from skrift.workers import configure_workers
from skrift.workers.models import utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smarter_dev.web import agent_session_cleanup
from smarter_dev.web import resources_agent
from smarter_dev.web import title_agent
from smarter_dev.web.agent_session_cleanup import forget_agent_sessions
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.worker_state_store import FinishedJobStateStore

_QUESTION = "how do I size a connection pool for a burst of webhooks"

_WORKER_TABLES = (
    (WorkerStateRecord, WorkerStateRecord.value),
    (WorkerArchiveSnapshotRecord, WorkerArchiveSnapshotRecord.value),
    (WorkerEventRecord, WorkerEventRecord.event),
    (WorkerArchiveEventRecord, WorkerArchiveEventRecord.event),
    (WorkerQueueRecord, WorkerQueueRecord.job),
    (WorkerDeadLetterRecord, WorkerDeadLetterRecord.entry),
)


@pytest.fixture
async def skrift_runtime(test_engine, db_session, monkeypatch):
    """Skrift's runtime over the test database, as app.yaml configures it."""
    session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(worker_runtime, "_runtime", None)
    runtime = configure_workers(
        mode="inline",
        queues=("agents", "agents-priority"),
        backend_imports={
            "state_store": "smarter_dev.web.worker_state_store:FinishedJobStateStore",
            "event_log": "skrift.workers.sqlalchemy:SQLAlchemyEventLog",
            "queue": "skrift.workers.sqlalchemy:SQLAlchemyQueue",
            "dead_letter_store": "skrift.workers.sqlalchemy:SQLAlchemyDeadLetterStore",
            "archive": "skrift.workers.sqlalchemy:SQLAlchemyArchive",
        },
        session_maker=session_maker,
    )
    assert isinstance(runtime.state_store, FinishedJobStateStore)
    # The agent settings app.yaml gives, without reading app.yaml.
    settings = SimpleNamespace(
        agents=AgentsConfig(default_subagent_dispatch="inline"), workers=WorkersConfig()
    )
    monkeypatch.setattr("skrift.config.get_settings", lambda: settings)

    @asynccontextmanager
    async def session_context():
        async with session_maker() as session:
            yield session

    monkeypatch.setattr(
        agent_session_cleanup, "get_db_session_context", session_context
    )
    return runtime


async def _record_session(
    agent,
    prompt,
    output,
    *,
    session_id=None,
    parent_session_id=None,
    root_session_id=None,
) -> Session:
    """Store a finished session the way Skrift's runner leaves one."""
    sid = session_id or uuid4().hex
    state = RunState(
        session_id=sid,
        agent_name=agent.skrift_name,
        messages=[{"role": "user", "content": prompt}],
        parent_session_id=parent_session_id,
        root_session_id=root_session_id or parent_session_id or sid,
    )
    append_event(state, "UserMessageReceived", {"message": prompt})
    await create_or_update_runstate(state)
    await drain_outbox(sid)
    stored = output.model_dump(mode="json") if hasattr(output, "model_dump") else output

    def complete(runstate: RunState) -> RunState:
        # The model turn's usage, as Skrift's runner records it.
        runstate.turn_usage["turn-1"] = AgentUsageRecord(
            session_id=sid,
            turn_id="turn-1",
            agent_name=agent.skrift_name,
            configured_model="openai-responses:gpt-6-luna",
            input_tokens=120,
            output_tokens=8,
        )
        runstate.status = "completed"
        runstate.terminal_at = utcnow()
        runstate.output = stored
        runstate.messages.append({"role": "model", "content": {"text": str(stored)}})
        append_event(runstate, "AgentCompleted", {"output": stored})
        return runstate

    await update_runstate(sid, complete)
    await drain_outbox(sid)
    return Session(sid)


def _recording(monkeypatch, agent, output) -> None:
    """``agent.run`` stores a finished session holding the prompt and ``output``."""

    async def run(prompt, *, session_id=None, **kwargs):
        return await _record_session(agent, prompt, output, session_id=session_id)

    monkeypatch.setattr(agent, "run", run)


def _stages_answer(monkeypatch) -> None:
    """Each Resources stage answers with an output of its own type."""
    ra = resources_agent
    _recording(
        monkeypatch,
        ra.reframer_agent,
        ra.ReframerOutput(
            restated_question="You want to size a pool for bursts.",
            reframing_instructions="Lead with queueing.",
            corpus_topics=["connection pools"],
            web_search_topics=["webhook bursts"],
        ),
    )
    _recording(monkeypatch, ra.researcher_agent, ra.ResearchOutput())
    _recording(monkeypatch, ra.gap_filler_agent, ra.GapFillerOutput())
    _recording(
        monkeypatch, ra.author_agent, "Size the pool for the queue, not the burst."
    )


async def _rows_holding(db_session, text: str) -> dict[str, int]:
    """How many rows of each worker table still contain ``text``."""
    db_session.expire_all()
    found = {}
    for model, column in _WORKER_TABLES:
        values = (await db_session.scalars(select(column))).all()
        found[model.__tablename__] = sum(text in str(value) for value in values)
    return found


async def _rows(db_session, model) -> int:
    db_session.expire_all()
    return len((await db_session.scalars(select(model.id))).all())


@pytest.mark.asyncio
async def test_a_resources_answer_leaves_nothing_of_the_question_in_the_worker_tables(
    skrift_runtime, db_session, monkeypatch
):
    _stages_answer(monkeypatch)

    async def no_notification(*args, **kwargs):
        return None

    monkeypatch.setattr(resources_agent, "notify_user", no_notification)
    usage: list[dict] = []
    seen_while_running: list[dict[str, int]] = []

    async def record_usage(record: dict, index: int) -> None:
        seen_while_running.append(await _rows_holding(db_session, _QUESTION))

    answer = await resources_agent.run_resources_pipeline(
        _QUESTION,
        message_history=None,
        actor="user-1",
        conversation_id="conversation-1",
        owner_user_id="user-1",
        usage_records=usage,
        usage_callback=record_usage,
    )

    assert answer
    assert [record["stage"] for record in usage][:2] == ["reframer", "researcher"]
    assert usage[-1]["stage"] == "author"
    # While the stages ran, Skrift held the question in its session state.
    assert seen_while_running[0]["worker_state"] > 0
    assert await _rows_holding(db_session, _QUESTION) == {
        model.__tablename__: 0 for model, _ in _WORKER_TABLES
    }
    assert not any(
        stream.startswith("agents:run:") for stream in await _streams(db_session)
    )


@pytest.mark.asyncio
async def test_a_failed_resources_stage_still_leaves_nothing_behind(
    skrift_runtime, db_session, monkeypatch
):
    _stages_answer(monkeypatch)

    async def no_notification(*args, **kwargs):
        return None

    async def stop_after_research(record: dict, index: int) -> None:
        if record["stage"] == "researcher":
            raise RuntimeError("the run lost its lease")

    monkeypatch.setattr(resources_agent, "notify_user", no_notification)

    with pytest.raises(RuntimeError, match="lost its lease"):
        await resources_agent.run_resources_pipeline(
            _QUESTION,
            message_history=None,
            actor="user-1",
            conversation_id="conversation-1",
            owner_user_id="user-1",
            usage_records=[],
            usage_callback=stop_after_research,
        )

    assert sum((await _rows_holding(db_session, _QUESTION)).values()) == 0


@pytest.mark.asyncio
async def test_a_title_leaves_nothing_of_the_question_in_the_worker_tables(
    skrift_runtime, db_session, monkeypatch
):
    _recording(monkeypatch, title_agent.title_agent, "Webhook Burst Pool Sizing")

    asker, conversation = uuid4(), uuid4()

    title = await title_agent.generate_title(
        _QUESTION, actor=str(asker), conversation_id=conversation
    )

    assert title
    assert sum((await _rows_holding(db_session, _QUESTION)).values()) == 0
    assert await _rows(db_session, WorkerArchiveSnapshotRecord) == 0
    # The session was the only record of what the title cost; it is kept.
    db_session.expire_all()
    cost = await db_session.scalar(select(UsageCostRow))
    assert (cost.product_mode, cost.operation_type) == ("resources", "resource_title")
    assert (cost.user_id, cost.conversation_id) == (asker, conversation)
    assert (cost.model_id, cost.input_tokens, cost.output_tokens) == (
        "gpt-6-luna",
        120,
        8,
    )
    assert _QUESTION not in str(cost.details)


async def _streams(db_session) -> set[str]:
    db_session.expire_all()
    return set((await db_session.scalars(select(WorkerEventRecord.stream))).all())


async def _state_keys(db_session, model) -> set[str]:
    db_session.expire_all()
    return set((await db_session.scalars(select(model.key))).all())


@pytest.mark.asyncio
async def test_sub_agents_go_with_their_root(skrift_runtime, db_session):
    agent = resources_agent.author_agent
    parent = await _record_session(agent, _QUESTION, "an answer")
    await _record_session(
        agent, f"look into {_QUESTION}", "notes", parent_session_id=parent.id
    )
    unrelated = await _record_session(agent, "another member's question", "theirs")

    await forget_agent_sessions([parent.id], with_sub_agents=True)

    assert await _state_keys(db_session, WorkerStateRecord) == {
        f"runstate:{unrelated.id}"
    }
    assert await _state_keys(db_session, WorkerArchiveSnapshotRecord) == {
        f"runstate:{unrelated.id}"
    }
    assert await _streams(db_session) == {f"agents:run:{unrelated.id}"}


@pytest.mark.asyncio
async def test_kept_event_streams_stay_for_the_page_that_reads_them(
    skrift_runtime, db_session
):
    agent = resources_agent.author_agent
    parent = await _record_session(agent, _QUESTION, "an answer")
    child = await _record_session(
        agent, f"look into {_QUESTION}", "notes", parent_session_id=parent.id
    )
    streams = await _streams(db_session)

    await forget_agent_sessions([parent.id], with_sub_agents=True, keep_events=True)

    assert await _state_keys(db_session, WorkerStateRecord) == set()
    assert await _state_keys(db_session, WorkerArchiveSnapshotRecord) == set()
    assert (
        await _streams(db_session)
        == streams
        == {
            f"agents:run:{parent.id}",
            f"agents:run:{child.id}",
        }
    )


@pytest.mark.asyncio
async def test_a_failed_delete_is_left_to_the_hourly_job(monkeypatch, caplog):
    @asynccontextmanager
    async def broken():
        raise ConnectionError("database restarting")
        yield

    monkeypatch.setattr(agent_session_cleanup, "get_db_session_context", broken)

    await forget_agent_sessions(["abc"])

    assert "the hourly retention job will" in caplog.text
