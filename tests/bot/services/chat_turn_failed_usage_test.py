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
