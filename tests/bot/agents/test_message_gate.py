"""Tests for the pre-turn message gate (GPT-5.4 Nano relevance filter).

No live API calls: the mapping/order/hallucination cases drive the real agent
through pydantic_ai's ``TestModel``/``FunctionModel`` via ``agent.override``,
and the short-circuit cases patch the agent getter to prove the model is never
built or called.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from smarter_dev.bot.agents import message_gate
from smarter_dev.bot.agents.message_gate import GateMessage, filter_messages


@pytest.fixture(autouse=True)
def _offline_gate_agent(monkeypatch):
    # The OpenAI SDK refuses to construct without a key; supply a dummy one so
    # the agent builds. Every test that reaches the model swaps it out via
    # ``agent.override``, so the key is never used for a real request. Reset the
    # module-level cache so each test builds a fresh, un-overridden agent.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    message_gate._gate_agent = None
    yield
    message_gate._gate_agent = None


def _candidates() -> list[GateMessage]:
    return [
        GateMessage(message_id="1", author_display="alice", content="how do decorators work?"),
        GateMessage(message_id="2", author_display="bob", content="what's for lunch?"),
        GateMessage(message_id="3", author_display="cara", content="explain async/await"),
    ]


@pytest.mark.asyncio
async def test_returns_only_allowed_candidate_ids():
    agent = message_gate.get_message_gate_agent()
    with agent.override(model=TestModel(custom_output_args={"allowed_message_ids": ["1", "3"]})):
        result = await filter_messages("only python questions", _candidates(), [])
    assert result == ["1", "3"]


@pytest.mark.asyncio
async def test_preserves_candidate_order_regardless_of_model_order():
    agent = message_gate.get_message_gate_agent()
    # Model answers out of candidate order; result must follow candidate order.
    with agent.override(model=TestModel(custom_output_args={"allowed_message_ids": ["3", "1"]})):
        result = await filter_messages("only python questions", _candidates(), [])
    assert result == ["1", "3"]


@pytest.mark.asyncio
async def test_intersects_hallucinated_ids_out():
    agent = message_gate.get_message_gate_agent()
    with agent.override(
        model=TestModel(custom_output_args={"allowed_message_ids": ["1", "9999", "2"]})
    ):
        result = await filter_messages("anything goes", _candidates(), [])
    # "9999" is not a real candidate id and is dropped.
    assert result == ["1", "2"]


@pytest.mark.asyncio
async def test_fail_open_returns_all_ids_on_model_error():
    def _boom(messages, info: AgentInfo):
        raise RuntimeError("nano outage")

    agent = message_gate.get_message_gate_agent()
    with agent.override(model=FunctionModel(_boom)):
        result = await filter_messages("only python questions", _candidates(), [])
    # A model failure must never silence the bot: every candidate is allowed.
    assert result == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_empty_candidates_short_circuits_without_model():
    with patch.object(message_gate, "get_message_gate_agent") as getter:
        result = await filter_messages("only python questions", [], _candidates())
    assert result == []
    getter.assert_not_called()


@pytest.mark.asyncio
async def test_blank_filter_allows_all_without_model():
    with patch.object(message_gate, "get_message_gate_agent") as getter:
        result = await filter_messages("   ", _candidates(), [])
    assert result == ["1", "2", "3"]
    getter.assert_not_called()


@pytest.mark.asyncio
async def test_grounding_is_rendered_but_never_returned():
    grounding = [GateMessage(message_id="g1", author_display="dan", content="earlier chatter")]
    captured: dict[str, object] = {}

    def _echo(messages, info: AgentInfo):
        captured["prompt"] = messages[-1].parts[-1].content
        return ModelResponse(
            parts=[ToolCallPart(tool_name="final_result", args={"allowed_message_ids": ["1", "g1"]})]
        )

    agent = message_gate.get_message_gate_agent()
    with agent.override(model=FunctionModel(_echo)):
        result = await filter_messages("only python questions", _candidates(), grounding)

    # The grounding id the model tried to return is intersected out.
    assert result == ["1"]
    assert "earlier chatter" in captured["prompt"]
