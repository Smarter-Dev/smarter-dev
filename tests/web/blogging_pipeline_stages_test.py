"""The blogging pipeline runs on Scout's news alone: no blog ideas, no Review."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from smarter_dev.web.blogging_agent import pipeline
from smarter_dev.web.blogging_agent.brainstorm_agent import BrainstormInput
from smarter_dev.web.blogging_agent.brainstorm_agent import BrainstormOutput
from smarter_dev.web.blogging_agent.research_agent import ResearchOutput
from smarter_dev.web.blogging_agent.scout_agent import ScoutOutput
from smarter_dev.web.blogging_agent.scout_agent import ScoutTopic
from smarter_dev.web.blogging_agent.synthesis_agent import SynthesisOutput
from smarter_dev.web.models import AuthoringPipelineRun


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(AuthoringPipelineRun.__table__.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_the_stages_are_scout_brainstorm_research_synthesis(sessions, monkeypatch):
    run_id = uuid4()
    async with sessions() as db:
        db.add(AuthoringPipelineRun(id=run_id, status="queued", stage_session_ids={}))
        await db.commit()
    monkeypatch.setattr(pipeline, "_build_engine", lambda: create_async_engine("sqlite+aiosqlite://"))
    monkeypatch.setattr(pipeline, "async_sessionmaker", lambda *a, **k: sessions)

    async def publish(Session, run_id, out):
        return uuid4()

    monkeypatch.setattr(pipeline, "_publish_once", publish)

    news = ScoutTopic(headline="A release", observation="It shipped", scope="tooling")
    outputs = {
        "scout": ScoutOutput(topics=[news]),
        "brainstorm": BrainstormOutput(
            hypothesis="h", counter_hypothesis="c", open_questions=["q"]
        ),
        "research": ResearchOutput(
            hypothesis_status="supported", revised_hypothesis="h", surprises=[], limits=[]
        ),
        "synthesis": SynthesisOutput(
            title="t", slug="t", content="c", limits_paragraph="l"
        ),
    }
    stages: list[str] = []
    prompts: dict[str, str] = {}

    async def run_stage(agent, user_prompt, *, stage_name, **kwargs):
        stages.append(stage_name)
        prompts[stage_name] = user_prompt
        return f"sid-{stage_name}", outputs[stage_name]

    monkeypatch.setattr(pipeline, "_run_stage", run_stage)

    result = await pipeline.run_authoring_pipeline(pipeline.PipelineRunPayload(run_id=run_id))

    assert result["status"] == "completed"
    assert stages == ["scout", "brainstorm", "research", "synthesis"]
    assert "A release" in prompts["brainstorm"]


def test_brainstorm_candidates_come_only_from_scout():
    assert BrainstormInput(candidates=[]).candidates == []
    assert not hasattr(pipeline, "CandidateBlogTopic")
    assert not hasattr(pipeline, "review_agent")
