"""An agent run's copy in Skrift's worker tables is deleted once its caller has the result.

Every agent here really runs: Skrift's runner executes it inline through its
SQLAlchemy backends over the test database, with the state store ``app.yaml``
names, and pydantic-ai drives the model loop. Only the model is swapped, for
pydantic-ai's ``TestModel`` answering with an output of the stage's type and
calling no tools, so nothing reaches a provider. The agent keeps its
configured model id, which is what its cost row is priced from.
"""

from __future__ import annotations

from contextlib import ExitStack
from contextlib import asynccontextmanager
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
import skrift.workers.runtime as worker_runtime
from pydantic_ai.models.test import TestModel
from skrift.agents.session import Session
from skrift.config import AgentsConfig
from skrift.config import WorkersConfig
from skrift.db.models.worker import WorkerArchiveEventRecord
from skrift.db.models.worker import WorkerArchiveSnapshotRecord
from skrift.db.models.worker import WorkerDeadLetterRecord
from skrift.db.models.worker import WorkerEventRecord
from skrift.db.models.worker import WorkerQueueRecord
from skrift.db.models.worker import WorkerStateRecord
from skrift.workers import configure_workers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smarter_dev.web import agent_session_cleanup
from smarter_dev.web import resources_agent
from smarter_dev.web import title_agent
from smarter_dev.web.agent_session_cleanup import forget_agent_sessions
from smarter_dev.web.blogging_agent import pipeline as blogging
from smarter_dev.web.blogging_agent.scout_agent import scout_agent
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
    # Building an agent's configured OpenAI model needs a key; TestModel
    # replaces that model for every run, so the key is never used.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-never-sent")

    @asynccontextmanager
    async def session_context():
        async with session_maker() as session:
            yield session

    monkeypatch.setattr(
        agent_session_cleanup, "get_db_session_context", session_context
    )
    return runtime


@contextmanager
def _answering(*answers):
    """Each ``(agent, output)`` runs on ``TestModel`` and answers with ``output``."""
    with ExitStack() as stack:
        for agent, output in answers:
            model = (
                TestModel(call_tools=[], custom_output_args=output.model_dump(mode="json"))
                if hasattr(output, "model_dump")
                else TestModel(call_tools=[], custom_output_text=output)
            )
            stack.enter_context(agent.materialized.override(model=model))
        yield


async def _run(agent, prompt, output, *, parent_session_id=None) -> Session:
    """A finished session of ``agent``, run for real on ``prompt``."""
    with _answering((agent, output)):
        session = await agent.run(prompt, parent_session_id=parent_session_id)
        await session.result()
    return session


def _stages_answer():
    """Each Resources stage answers with an output of its own type."""
    ra = resources_agent
    return _answering(
        (
            ra.reframer_agent,
            ra.ReframerOutput(
                restated_question="You want to size a pool for bursts.",
                reframing_instructions="Lead with queueing.",
                corpus_topics=["connection pools"],
                web_search_topics=["webhook bursts"],
            ),
        ),
        (ra.researcher_agent, ra.ResearchOutput()),
        (ra.gap_filler_agent, ra.GapFillerOutput()),
        (ra.author_agent, "Size the pool for the queue, not the burst."),
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
    async def no_notification(*args, **kwargs):
        return None

    monkeypatch.setattr(resources_agent, "notify_user", no_notification)
    usage: list[dict] = []
    seen_while_running: list[dict[str, int]] = []

    async def record_usage(record: dict, index: int) -> None:
        seen_while_running.append(await _rows_holding(db_session, _QUESTION))

    with _stages_answer():
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
    async def no_notification(*args, **kwargs):
        return None

    async def stop_after_research(record: dict, index: int) -> None:
        if record["stage"] == "researcher":
            raise RuntimeError("the run lost its lease")

    monkeypatch.setattr(resources_agent, "notify_user", no_notification)

    with _stages_answer(), pytest.raises(RuntimeError, match="lost its lease"):
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
    asker, conversation = uuid4(), uuid4()

    with _answering((title_agent.title_agent, "Webhook Burst Pool Sizing")):
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
    assert cost.model_id == "gpt-6-luna"
    assert cost.input_tokens > 0 and cost.output_tokens > 0
    assert _QUESTION not in str(cost.details)


async def _streams(db_session) -> set[str]:
    """Session event streams; ``workers:lifecycle`` is an operational record."""
    db_session.expire_all()
    streams = set((await db_session.scalars(select(WorkerEventRecord.stream))).all())
    return {stream for stream in streams if stream.startswith("agents:run:")}


async def _state_keys(db_session, model) -> set[str]:
    """Session state keys. A run's job state stays, emptied, for the hourly job."""
    db_session.expire_all()
    keys = set((await db_session.scalars(select(model.key))).all())
    return {key for key in keys if not key.startswith("workers:jobs:")}


@pytest.mark.asyncio
async def test_sub_agents_go_with_their_root(skrift_runtime, db_session):
    agent = resources_agent.author_agent
    parent = await _run(agent, _QUESTION, "an answer")
    await _run(agent, f"look into {_QUESTION}", "notes", parent_session_id=parent.id)
    unrelated = await _run(agent, "another member's question", "theirs")

    await forget_agent_sessions([parent.id], with_sub_agents=True)

    assert (await _rows_holding(db_session, _QUESTION))["worker_state"] == 0

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
    parent = await _run(agent, _QUESTION, "an answer")
    child = await _run(
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
async def test_a_blogging_stage_leaves_its_timeline_and_nothing_else(
    skrift_runtime, test_engine, db_session
):
    """One real blogging stage, run as the pipeline runs it, then forgotten as
    the pipeline forgets an attempt's stages."""
    started: list[str] = []
    found = scout_agent._init_kwargs["output_type"](
        topics=[
            {
                "headline": "Webhook bursts",
                "observation": _QUESTION,
                "scope": "pool sizing",
            }
        ]
    )

    with _answering((scout_agent, found)):
        session_id, result = await blogging._run_stage(
            scout_agent,
            f"Scout this week: {_QUESTION}",
            run_id=uuid4(),
            root_session_id=None,
            session_maker=async_sessionmaker(test_engine, expire_on_commit=False),
            stage_name="scout",
            started=started,
        )

    assert started == [session_id]
    assert blogging._typed(result, type(found)).topics[0].observation == _QUESTION
    assert (await _rows_holding(db_session, _QUESTION))["worker_state"] > 0

    await forget_agent_sessions(started, with_sub_agents=True, keep_events=True)

    assert await _state_keys(db_session, WorkerStateRecord) == set()
    assert await _state_keys(db_session, WorkerArchiveSnapshotRecord) == set()
    # The run's admin timeline reads its event stream after the run ends.
    assert await _streams(db_session) == {f"agents:run:{session_id}"}

@pytest.mark.asyncio
async def test_a_failed_delete_is_left_to_the_hourly_job(monkeypatch, caplog):
    @asynccontextmanager
    async def broken():
        raise ConnectionError("database restarting")
        yield

    monkeypatch.setattr(agent_session_cleanup, "get_db_session_context", broken)

    await forget_agent_sessions(["abc"])

    assert "the hourly retention job will" in caplog.text
