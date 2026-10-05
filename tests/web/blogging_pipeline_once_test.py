"""A blogging run publishes at most one post, however often its job runs (#27)."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from smarter_dev.web.blogging_agent import pipeline
from smarter_dev.web.models import AuthoringPipelineRun


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(AuthoringPipelineRun.__table__.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def pg_sessions():
    """Row locks need a real database; SQLite ignores FOR UPDATE."""
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set TEST_POSTGRES_URL to run the row-lock test")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(AuthoringPipelineRun.__table__.drop, checkfirst=True)
        await conn.run_sync(AuthoringPipelineRun.__table__.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(AuthoringPipelineRun.__table__.drop)
    await engine.dispose()


@pytest.fixture
def inserted(monkeypatch) -> list[UUID]:
    """Stands in for the pages insert, which needs skrift's tables."""
    pages: list[UUID] = []

    async def insert(db, out):
        pages.append(page := uuid4())
        return page

    monkeypatch.setattr(pipeline, "_insert_blog_post", insert)
    return pages


async def a_run(sessions, status: str = "running") -> UUID:
    run_id = uuid4()
    async with sessions() as db:
        db.add(AuthoringPipelineRun(id=run_id, status=status, stage_session_ids={}))
        await db.commit()
    return run_id


async def get_run(sessions, run_id) -> AuthoringPipelineRun:
    async with sessions() as db:
        return await db.get(AuthoringPipelineRun, run_id)


async def test_a_second_attempt_does_not_publish_again(sessions, inserted):
    run_id = await a_run(sessions)

    first = await pipeline._publish_once(sessions, run_id, out=None)
    second = await pipeline._publish_once(sessions, run_id, out=None)

    assert inserted == [first] and second == first
    run = await get_run(sessions, run_id)
    assert run.status == "completed" and run.result_page_id == first


async def test_overlapping_attempts_publish_once(pg_sessions, monkeypatch):
    pages: list[UUID] = []

    async def slow_insert(db, out):
        await asyncio.sleep(0.2)  # both attempts are inside _publish_once at once
        pages.append(page := uuid4())
        return page

    monkeypatch.setattr(pipeline, "_insert_blog_post", slow_insert)
    run_id = await a_run(pg_sessions)

    results = await asyncio.gather(
        *(pipeline._publish_once(pg_sessions, run_id, out=None) for _ in range(2))
    )

    assert len(pages) == 1 and set(results) == set(pages)


async def test_a_failing_overlap_does_not_undo_a_completed_run(sessions, inserted):
    run_id = await a_run(sessions)
    page = await pipeline._publish_once(sessions, run_id, out=None)

    await pipeline._finalise_failed(sessions, run_id, "RuntimeError: late duplicate")

    run = await get_run(sessions, run_id)
    assert run.status == "completed" and run.result_page_id == page and run.error is None


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_a_finished_run_is_not_run_again(sessions, monkeypatch, status):
    run_id = await a_run(sessions, status=status)
    engine = create_async_engine("sqlite+aiosqlite://")
    monkeypatch.setattr(pipeline, "_build_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "async_sessionmaker", lambda *a, **k: sessions)

    async def no_stage(*args, **kwargs):
        raise AssertionError("a finished run must not start a stage")

    monkeypatch.setattr(pipeline, "_run_stage", no_stage)

    result = await pipeline.run_authoring_pipeline(pipeline.PipelineRunPayload(run_id=run_id))

    assert result == {"status": status, "reason": "already finished"}
    assert (await get_run(sessions, run_id)).status == status


async def test_an_ended_attempt_deletes_only_its_own_stage_sessions(sessions, monkeypatch):
    """The run state of this attempt's stages goes; the timeline's streams stay."""
    run_id = await a_run(sessions)
    engine = create_async_engine("sqlite+aiosqlite://")
    monkeypatch.setattr(pipeline, "_build_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "async_sessionmaker", lambda *a, **k: sessions)

    async def scout_then_crash(agent, prompt, *, started, stage_name, **kwargs):
        started.append(f"{stage_name}-session")
        if stage_name == "brainstorm":
            raise RuntimeError("provider unavailable")
        return started[-1], None

    forgotten = []

    async def forget(session_ids, **options):
        forgotten.append((list(session_ids), options))

    monkeypatch.setattr(pipeline, "_run_stage", scout_then_crash)
    monkeypatch.setattr(pipeline, "_typed", lambda raw, model: model.model_construct())
    monkeypatch.setattr(pipeline, "forget_agent_sessions", forget)

    result = await pipeline.run_authoring_pipeline(pipeline.PipelineRunPayload(run_id=run_id))

    assert result == {"status": "failed", "reason": "exception"}
    assert forgotten == [
        (
            ["scout-session", "brainstorm-session"],
            {"with_sub_agents": True, "keep_events": True},
        )
    ]
