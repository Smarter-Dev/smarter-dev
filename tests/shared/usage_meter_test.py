"""A UsageMeter keeps every provider response's own counts, failed runs too."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai import ModelRetry
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.shared.usage_meter import UsageMeter


def _answering(*usages: RequestUsage, text: str = "ok", name: str = "claude-haiku-5-5"):
    calls = iter(usages)

    def respond(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)], usage=next(calls))

    return FunctionModel(respond, model_name=name)


async def test_each_request_of_a_run_is_kept_on_its_own():
    first = RequestUsage(
        input_tokens=150_000, output_tokens=10, cache_read_tokens=100_000
    )
    second = RequestUsage(input_tokens=40_000, output_tokens=20, cache_write_tokens=5)
    agent = Agent(_answering(first, second))
    attempts = 0

    @agent.output_validator
    def once_more(output: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelRetry("again")
        return output

    meter = UsageMeter()
    await agent.run("hi", model=meter.wrap(agent.model))

    assert meter.stages() == [
        (
            "claude-haiku-5-5",
            RequestTokens(190_000, 30, 100_000, 5),
            [RequestTokens(150_000, 10, 100_000, 0), RequestTokens(40_000, 20, 0, 5)],
        )
    ]


async def test_a_run_that_raises_keeps_what_the_provider_billed():
    agent = Agent(
        _answering(
            RequestUsage(input_tokens=7, output_tokens=3),
            RequestUsage(input_tokens=9, output_tokens=4),
        ),
        retries=1,
    )

    @agent.output_validator
    def never(output: str) -> str:
        raise ModelRetry("no")

    meter = UsageMeter()
    with pytest.raises(UnexpectedModelBehavior):
        await agent.run("hi", model=meter.wrap(agent.model))

    assert meter.stages()[0][2] == [RequestTokens(7, 3), RequestTokens(9, 4)]


async def test_a_fallbacks_requests_are_kept_at_its_own_model():
    def broken(messages, info):
        raise ModelHTTPError(500, "primary")

    fallback = FallbackModel(
        FunctionModel(broken, model_name="claude-haiku-5-5"),
        _answering(
            RequestUsage(input_tokens=11, output_tokens=2), name="gemini-3.8-flash"
        ),
    )
    meter = UsageMeter()
    await Agent(fallback).run("hi", model=meter.wrap(fallback))

    assert [stage[0] for stage in meter.stages()] == ["gemini-3.8-flash"]
    # The caller's model is left as it was.
    assert all(not hasattr(model, "meter") for model in fallback.models)


async def test_a_streamed_run_is_metered():
    def respond(messages, info):
        return ModelResponse(parts=[TextPart("ok")])

    async def stream(messages, info):
        yield "streamed"

    agent = Agent(
        FunctionModel(respond, stream_function=stream, model_name="claude-haiku-5-5")
    )
    meter = UsageMeter()
    async with agent.run_stream("hi", model=meter.wrap(agent.model)) as result:
        await result.get_output()

    assert meter.stages() and meter.stages()[0][1].input_tokens > 0


async def test_an_empty_meter_is_falsy_and_structured_output_counts():
    class Out(BaseModel):
        value: int

    assert not UsageMeter()
    meter = UsageMeter()
    from pydantic_ai.models.test import TestModel

    await Agent(TestModel(), output_type=Out).run("hi", model=meter.wrap(TestModel()))
    assert meter
    assert meter.stage_dicts()[0]["model_name"] == "test"
