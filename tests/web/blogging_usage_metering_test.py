"""A blogging run's model usage reaches the ledger, once, as the site's own.

The real pipeline handler runs the real Scout stage on Skrift (on a
FunctionModel that calls the real ``read_news`` tool, whose news summariser
runs on a second FunctionModel) and then fails at Brainstorm. Its ``finally``
writes the Scout session's turn and the summariser's run as ``resources`` rows
charged to the admin who started it, with no conversation: no Chat bill and
no Resources question counts them. The Skrift turn holds the stage's own
requests only, so the summariser's tokens are not counted twice.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smarter_dev.shared import database
from smarter_dev.shared import model_router
from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.shared.usage_meter import UsageMeter
from smarter_dev.web.agent_api import _count_questions_last_week
from smarter_dev.web.blogging_agent import pipeline
from smarter_dev.web.blogging_agent import scout_agent as scout
from smarter_dev.web.blogging_agent import summariser
from smarter_dev.web.chat.usage import spend_between
from smarter_dev.web.llm_pricing import calc_cost_by_request
from smarter_dev.web.models import AuthoringPipelineRun
from smarter_dev.web.models import UsageCostRow
from tests.web.agent_session_cleanup_test import skrift_runtime  # noqa: F401 — fixture

HAIKU = "claude-haiku-5-5"
NEWS_URL = "https://news.example/release"


def _usage(input_tokens: int, output_tokens: int) -> RequestUsage:
    return RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens)


class _Summariser:
    """The summariser's model: text for a summary, a tool call for excerpts."""

    def __init__(self, *usages: RequestUsage):
        self.usages = list(usages)
        self.billed: list[RequestTokens] = []
        self.model = FunctionModel(self._respond, model_name=HAIKU)

    def _respond(self, messages, info: AgentInfo) -> ModelResponse:
        usage = self.usages.pop(0)
        self.billed.append(RequestTokens(usage.input_tokens, usage.output_tokens))
        if info.output_tools:
            part = ToolCallPart(
                info.output_tools[0].name, {"excerpts": ["It shipped."]}
            )
        else:
            part = TextPart("A release shipped.")
        return ModelResponse(parts=[part], usage=usage)


@pytest.fixture
def summariser_model(monkeypatch):
    def use(model: _Summariser) -> None:
        monkeypatch.setattr(summariser, "_news_summariser_agent", None)
        monkeypatch.setattr(summariser, "_excerpt_extractor_agent", None)
        monkeypatch.setattr(model_router, "build_model_for", lambda _model: model.model)

    return use


async def test_summariser_runs_are_metered_failed_ones_too(summariser_model):
    model = _Summariser(_usage(150_000, 40), _usage(900, 30))
    summariser_model(model)
    meter = UsageMeter()

    summary = await summariser.summarise_news_page(
        text="body", url=NEWS_URL, title="Release", meter=meter
    )
    excerpts = await summariser.extract_excerpts(
        text="body",
        url=NEWS_URL,
        title="Release",
        questions=["what shipped?"],
        meter=meter,
    )

    assert summary == "A release shipped." and excerpts == ["It shipped."]
    [(name, total, requests)] = meter.stages()
    assert name == HAIKU and requests == model.billed

    # A run that raises after the answer (its output refused) is still kept;
    # the summariser degrades as before.
    failing = UsageMeter()
    summariser_model(_Summariser(*[_usage(10, 1)] * 5))

    async def refuse(output):
        raise ValueError("refused")

    summariser._news_summariser().output_validator(refuse)
    assert (
        await summariser.summarise_news_page(
            text="b", url=NEWS_URL, title="T", meter=failing
        )
    ).startswith("[summary unavailable]")
    assert failing.stages()[0][1].input_tokens >= 10


async def test_a_blogging_run_records_its_stages_and_summaries_once(
    skrift_runtime,  # noqa: F811 — fixture
    test_engine,
    db_session,
    monkeypatch,
    summariser_model,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    @asynccontextmanager
    async def main_db():
        async with maker() as session:
            yield session

    # record_meter_now writes through the main database's sessions.
    monkeypatch.setattr(database, "get_db_session_context", main_db)

    async def keep_engine():
        return None

    # The run's own engine is only handed to the session maker and disposed.
    monkeypatch.setattr(
        pipeline, "_build_engine", lambda: SimpleNamespace(dispose=keep_engine)
    )
    monkeypatch.setattr(pipeline, "async_sessionmaker", lambda *a, **k: maker)

    admin = uuid4()
    run_id = uuid4()
    async with maker() as db:
        db.add(
            AuthoringPipelineRun(
                id=run_id,
                status="queued",
                stage_session_ids={},
                kicked_off_by_user_id=admin,
            )
        )
        await db.commit()

    async def page(client, url):
        return {"content": "# Release\nIt shipped.", "title": "Release"}

    monkeypatch.setattr(scout, "jina_read", page)
    # The summary's request is past Haiku's 100k long-context line on its own.
    summaries = _Summariser(_usage(150_000, 40))
    summariser_model(summaries)

    scout_billed: list[RequestTokens] = []

    def scout_model(messages, info: AgentInfo) -> ModelResponse:
        usage = _usage(5_000, 70) if not scout_billed else _usage(6_000, 90)
        scout_billed.append(RequestTokens(usage.input_tokens, usage.output_tokens))
        if len(scout_billed) == 1:
            part = ToolCallPart("read_news", {"url": NEWS_URL})
        else:
            part = ToolCallPart(info.output_tools[0].name, {"topics": []})
        return ModelResponse(parts=[part], usage=usage, model_name=HAIKU)

    real_stage = pipeline._run_stage

    async def stages(agent, user_prompt, **kwargs):
        if kwargs["stage_name"] != "scout":
            raise RuntimeError("provider unavailable")
        with scout.scout_agent.materialized.override(model=FunctionModel(scout_model)):
            return await real_stage(agent, user_prompt, **kwargs)

    monkeypatch.setattr(pipeline, "_run_stage", stages)

    result = await pipeline.run_authoring_pipeline(
        pipeline.PipelineRunPayload(run_id=run_id)
    )

    assert result == {"status": "failed", "reason": "exception"}
    db_session.expire_all()
    rows = {
        row.operation_type: row
        for row in (await db_session.scalars(select(UsageCostRow))).all()
    }
    assert set(rows) == {"blog_pipeline", "blog_summariser"}
    for row in rows.values():
        assert row.product_mode == "resources"
        assert row.user_id == admin and row.conversation_id is None
        assert row.discord_user_id is None
        assert (row.provider_key, row.model_id) == ("anthropic", HAIKU)

    # The Scout turn holds Scout's two requests and nothing of the summary.
    stage = rows["blog_pipeline"]
    assert len(scout_billed) == 2
    assert (stage.input_tokens, stage.output_tokens) == (11_000, 160)
    # The summary: its own row, priced on its one long request.
    summary = rows["blog_summariser"]
    assert summary.operation_key.startswith(f"blog_summariser:{run_id}:")
    assert (summary.input_tokens, summary.output_tokens) == (150_000, 40)
    assert summary.cost_usd == calc_cost_by_request(
        HAIKU, RequestTokens(150_000, 40), summaries.billed
    )
    assert summary.cost_usd > calc_cost_by_request(HAIKU, RequestTokens(150_000, 40))
    assert summary.details["run_id"] == str(run_id)

    # Nobody's Chat bill, and not a Resources question.
    now = datetime.now(UTC)
    assert (
        await spend_between(
            db_session, admin, now - timedelta(days=1), now + timedelta(days=1)
        )
        == 0
    )
    assert await _count_questions_last_week(db_session, admin, "resources") == 0
