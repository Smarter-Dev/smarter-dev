"""A handler's gathering agent, and the media its web_read describes, are metered.

The real path: ``HandlerExecution._spawn_agent`` → the gated runner a fire job
builds → the real gathering agent and its real ``_web_read`` tool → the real
``media_read`` describe, each agent on a FunctionModel reporting known
per-request usage, into SQLite ``usage_cost_rows``. A cap raised in a tool
after the model answered still leaves the billed request on the ledger.
"""

from __future__ import annotations

import contextlib
import io

import pytest
from PIL import Image
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.web import handler_agent
from smarter_dev.web import media_read
from smarter_dev.web.handler_agent import GatherUsage
from smarter_dev.web.handler_agent import gathering_agent_for
from smarter_dev.web.handler_budget import CapExceeded
from smarter_dev.web.handler_budget import HandlerBudget
from smarter_dev.web.handler_runtime import HandlerExecution
from smarter_dev.web.llm_pricing import calc_cost_by_request
from smarter_dev.web.models import UsageCostRow

HAIKU = "claude-haiku-5-5"
NIA = "222222222222222222"
IMAGE_URL = "https://cdn.example/screenshot.png"


class _Model:
    """A FunctionModel answering each request with the next scripted response."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.billed: list[RequestTokens] = []
        self.model = FunctionModel(self._respond, model_name=HAIKU)

    def _respond(self, messages, info: AgentInfo) -> ModelResponse:
        part, usage = self.answers.pop(0)
        if part == "read":
            [tool] = [t for t in info.function_tools if t.name.endswith("web_read")]
            part = ToolCallPart(
                tool.name, {"url": IMAGE_URL, "instruction": "what text?"}
            )
        elif isinstance(part, str):
            part = TextPart(part)
        self.billed.append(RequestTokens(usage.input_tokens, usage.output_tokens))
        return ModelResponse(parts=[part], usage=usage)

    def total(self) -> RequestTokens:
        return RequestTokens(*(sum(column) for column in zip(*self.billed)))


def _usage(input_tokens: int, output_tokens: int) -> RequestUsage:
    return RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def session_factory(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def factory():
        async with maker() as session:
            yield session

    return factory


@pytest.fixture
def models(monkeypatch):
    """Put the real gathering agent and the real image reader on scripted models."""

    def use(gathering: _Model, media: _Model | None = None) -> None:
        monkeypatch.setattr(handler_agent, "_agents", {})
        monkeypatch.setattr(
            handler_agent, "build_model_for", lambda _model: gathering.model
        )

        def no_redis():
            raise RuntimeError("no redis in tests")

        monkeypatch.setattr(handler_agent, "get_redis_client", no_redis)
        if media is not None:
            reader = Agent(media.model, output_type=str)
            monkeypatch.setattr(media_read, "_get_media_agent", lambda: reader)

        async def fetch(url, **kwargs):
            return _png(), "image/png"

        monkeypatch.setattr(media_read, "_fetch_bytes", fetch)

    return use


class _Limiter:
    async def hit(self, key, limit, window_seconds=None):
        return True


def _execution(session_factory, budget: HandlerBudget) -> HandlerExecution:
    usage = GatherUsage(
        session_factory=session_factory,
        job_id="job-7",
        handler_id="handler-3",
        handler_kind="standard",
        guild_id="G1",
        channel_id="C1",
    )
    return HandlerExecution(
        channel_id="C1",
        guild_id="G1",
        budget=budget,
        emitter=None,
        limiter=_Limiter(),
        agent_runner=gathering_agent_for(
            {"author_id": NIA}, session_factory, usage=usage
        ),
        handler_id="handler-3",
    )


async def _rows(db_session) -> dict[str, UsageCostRow]:
    db_session.expire_all()
    rows = (await db_session.scalars(select(UsageCostRow))).all()
    return {row.operation_type: row for row in rows}


async def test_an_agent_call_and_its_media_read_land_as_two_rows(
    db_session, session_factory, models
):
    # The first request is past Haiku 5.5's 100k long-context line on its own.
    gathering = _Model(
        ("read", _usage(150_000, 20)), ("It says hello.", _usage(2_000, 50))
    )
    media = _Model(("A screenshot saying hello.", _usage(1_200, 80)))
    models(gathering, media)

    out = await _execution(session_factory, HandlerBudget())._spawn_agent(
        f"what does {IMAGE_URL} say?", True
    )

    assert out == "It says hello."
    rows = await _rows(db_session)
    assert set(rows) == {"handler_agent", "handler_media_reader"}
    for operation_type, used in (
        ("handler_agent", gathering),
        ("handler_media_reader", media),
    ):
        row = rows[operation_type]
        assert row.product_mode == "discord"
        assert (row.provider_key, row.model_id) == ("anthropic", HAIKU)
        assert row.user_id is None and row.discord_user_id is None
        assert row.conversation_id is None
        assert row.operation_key == f"{operation_type}:job-7:1:{HAIKU}"
        assert row.details["job_id"] == "job-7"
        assert row.details["handler_id"] == "handler-3"
        assert (row.details["guild_id"], row.details["channel_id"]) == ("G1", "C1")
        assert row.details["agent_call"] == 1
        total = used.total()
        assert (row.input_tokens, row.output_tokens) == (
            total.input_tokens,
            total.output_tokens,
        )
        assert row.cost_usd == calc_cost_by_request(HAIKU, total, used.billed)
        assert row.details["requests"] == len(used.billed)
    # Priced per request, the 150k request bills at the long-context rate.
    agent_row = rows["handler_agent"]
    assert agent_row.cost_usd > calc_cost_by_request(HAIKU, gathering.total())


async def test_a_cap_raised_in_a_tool_still_records_the_billed_request(
    db_session, session_factory, models
):
    gathering = _Model(("read", _usage(3_000, 15)), ("never sent", _usage(1, 1)))
    models(gathering)

    with pytest.raises(CapExceeded):
        await _execution(session_factory, HandlerBudget(max_web_reads=0))._spawn_agent(
            f"read {IMAGE_URL}", True
        )

    rows = await _rows(db_session)
    assert set(rows) == {"handler_agent"}
    row = rows["handler_agent"]
    assert (row.input_tokens, row.output_tokens) == (3_000, 15)
    assert row.details["requests"] == 1
    assert row.cost_usd == calc_cost_by_request(
        HAIKU, RequestTokens(3_000, 15), gathering.billed
    )


async def test_a_fires_second_agent_call_is_its_own_row(
    db_session, session_factory, models
):
    gathering = _Model(("one", _usage(100, 5)), ("two", _usage(200, 6)))
    models(gathering)
    execution = _execution(session_factory, HandlerBudget())

    await execution._spawn_agent("first", False)
    await execution._spawn_agent("second", False)

    db_session.expire_all()
    keys = sorted((await db_session.scalars(select(UsageCostRow.operation_key))).all())
    assert keys == [f"handler_agent:job-7:1:{HAIKU}", f"handler_agent:job-7:2:{HAIKU}"]
