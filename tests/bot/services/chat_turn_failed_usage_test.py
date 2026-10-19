"""A Discord chat turn that raises still bills what the provider charged (#211).

The turn runs through the real ChannelEngine with a real pydantic-ai agent on a
FunctionModel: one billed response that calls a tool, then a provider error.
The engine posts its usual error and reports the billed request to the usage
ledger as ``chat_turn_failed``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from smarter_dev.bot.services import usage_reporter
from tests.bot.services.test_chat_engine import _build_engine
from tests.bot.services.test_chat_engine import _fake_trigger_message
from tests.bot.services.test_chat_engine import _patch_engine
from tests.bot.services.test_chat_engine import fake_bot  # noqa: F401
from tests.bot.services.test_chat_engine import fake_memory  # noqa: F401

BILLED = RequestUsage(
    input_tokens=120_000, output_tokens=25, cache_write_tokens=110_000
)


class _Client:
    def __init__(self):
        self.posts = []

    async def post(self, path, json_data=None, **_kwargs):
        self.posts.append((path, json_data))


@pytest.fixture
def ledger_client():
    client = _Client()
    usage_reporter.configure_usage_reporter(client)
    yield client
    usage_reporter.configure_usage_reporter(None)


@pytest.mark.asyncio
async def test_a_failed_turn_reports_its_billed_request(
    fake_bot,  # noqa: F811 - the fixture imported above
    fake_memory,  # noqa: F811
    ledger_client,
):
    def respond(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("no_such_tool", {})], usage=BILLED)
        raise ModelHTTPError(500, "claude-sonnet-5-5")

    agent = Agent(FunctionModel(respond, model_name="claude-sonnet-5-5"))
    patches = _patch_engine(agent_run=None, fake_memory=fake_memory)
    with (
        patch(
            "smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent
        ),
        patches[1],
        patches[2],
        patches[3],
    ):
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial(_fake_trigger_message(author_id=200))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if ledger_client.posts:
                break
        await engine.shutdown()
    await usage_reporter.drain_usage_reports()

    fake_bot.rest.create_message.assert_awaited()  # the usual error reply
    [(path, payload)] = ledger_client.posts
    assert path == "/bot-usage"
    assert payload["operation_type"] == "chat_turn_failed"
    assert payload["failed"] is True
    assert (payload["guild_id"], payload["channel_id"]) == ("99", "42")
    assert payload["discord_user_id"] == "200"
    [stage] = payload["stages"]
    assert stage["model_name"] == "claude-sonnet-5-5"
    assert stage["requests"] == [
        {
            "input_tokens": 120_000,
            "output_tokens": 25,
            "cache_read_tokens": 0,
            "cache_write_tokens": 110_000,
        }
    ]


SHORTEN_BILLED = RequestUsage(input_tokens=150_000, output_tokens=40)
MAIN = RequestUsage(input_tokens=20_000, output_tokens=1_500)


@pytest.mark.asyncio
async def test_a_failed_shorten_rerun_bills_its_request_with_the_turn(
    fake_bot,  # noqa: F811
    fake_memory,  # noqa: F811
    monkeypatch,
):
    # Astra's #211 reproduction: the reply is too long, the shorten re-run
    # gets one billed answer that needs a retry, and the retry's request
    # fails. The summarizer then fits the reply; the turn goes on.
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from pydantic_ai.messages import TextPart

    from smarter_dev.bot.agents import response_fitting
    from smarter_dev.bot.agents.chat_models import TurnDecision
    from smarter_dev.shared.request_usage import RequestTokens
    from smarter_dev.web.api_native.chat_conversations import _billed_chat_model
    from smarter_dev.web.api_native.schemas import ChatAgentTurnCreate
    from smarter_dev.web.llm_pricing import calc_cost_by_request
    from tests.bot.services.test_chat_engine import _send

    model_name = "claude-haiku-5-5"
    monkeypatch.setenv("CHAT_AGENT_MODEL", model_name)

    def respond(messages, info):
        shortening = "<length-notice>" in str(messages)
        if not shortening:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        _send("x" * 5000, target_message_id="101").model_dump(
                            mode="json"
                        ),
                    )
                ],
                usage=MAIN,
            )
        if "retry" not in str(messages[-1]).lower():
            # Text where the output tool is required: pydantic-ai asks again.
            return ModelResponse(parts=[TextPart("shorter?")], usage=SHORTEN_BILLED)
        raise ModelHTTPError(500, model_name)

    agent = Agent(
        FunctionModel(respond, model_name=model_name), output_type=TurnDecision
    )

    async def summarized(message, *, usage_fields=None):
        return "a short summary"

    monkeypatch.setattr(response_fitting, "_summarize_reply", summarized)
    captured: dict = {}
    persisted = asyncio.Event()

    async def fake_persist(**kwargs):
        captured.update(kwargs)
        persisted.set()

    patches = _patch_engine(agent_run=None, fake_memory=fake_memory)
    with (
        patch(
            "smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent
        ),
        patch("smarter_dev.bot.services.chat_engine.persist_turn", new=fake_persist),
        patches[1],
        patches[2],
        patches[3],
    ):
        engine, _ = await _build_engine(fake_bot)
        engine.engagement_id = "eng-1"
        engine.start()
        engine.trigger_initial(_fake_trigger_message(message_id=101))
        try:
            await asyncio.wait_for(persisted.wait(), 10)
        finally:
            await engine.shutdown()

    billed = [
        RequestTokens(MAIN.input_tokens, MAIN.output_tokens),
        RequestTokens(SHORTEN_BILLED.input_tokens, SHORTEN_BILLED.output_tokens),
    ]
    assert captured["chat_tokens_input"] == sum(r.input_tokens for r in billed)
    assert captured["chat_tokens_output"] == sum(r.output_tokens for r in billed)
    [stage] = captured["chat_stages"]
    assert sorted(stage["requests"], key=str) == sorted(
        (r.as_dict() for r in billed), key=str
    )
    payload = ChatAgentTurnCreate(
        engagement_id="00000000-0000-0000-0000-000000000001",
        request_id="r",
        turn_kind="initial",
        output_kind="send_response",
        triggering_messages=[],
        agent_output={},
        model_messages_delta=ModelMessagesTypeAdapter.dump_python(
            captured["new_model_messages"], mode="json"
        ),
        **{
            key: captured[key]
            for key in (
                "chat_model_name",
                "chat_tokens_input",
                "chat_tokens_output",
                "chat_cache_read_tokens",
                "chat_cache_write_tokens",
                "chat_stages",
            )
        },
    )
    # Each request at its own tier: the 150K re-run request at Haiku's 5x.
    total = RequestTokens(*(sum(column) for column in zip(*billed)))
    assert _billed_chat_model(payload).cost_usd == calc_cost_by_request(
        model_name, total, billed
    )
