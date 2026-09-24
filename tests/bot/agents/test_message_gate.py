"""Tests for the pre-turn message gate (Jev relevance filter).

No live API calls: the gate's Jev model is swapped for pydantic_ai's
``FunctionModel``, which answers the per-candidate bool questions as a final
output tool call, and the short-circuit cases patch the model getter to prove
it is never built or called.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.typesafe import TypeSafeModel

from smarter_dev.bot.agents import message_gate
from smarter_dev.bot.agents.message_gate import GateMessage
from smarter_dev.bot.agents.message_gate import filter_messages


@pytest.fixture(autouse=True)
def _reset_gate_model():
    message_gate._gate_model = None
    yield
    message_gate._gate_model = None


def _candidates() -> list[GateMessage]:
    return [
        GateMessage(message_id="1", author_display="alice", content="how do decorators work?"),
        GateMessage(message_id="2", author_display="bob", content="what's for lunch?"),
        GateMessage(message_id="3", author_display="cara", content="explain async/await"),
    ]


def _answering(verdicts: dict[str, bool], captured: dict | None = None) -> FunctionModel:
    """A stand-in Jev answering each ``candidate_<n>`` field with ``verdicts``."""

    def respond(messages, info: AgentInfo):
        if captured is not None:
            request = messages[-1]
            assert isinstance(request, ModelRequest)
            captured["instructions"] = request.instructions
            captured["material"] = request.parts[-1].content
            captured["schema"] = info.output_tools[0].parameters_json_schema
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=verdicts)]
        )

    return FunctionModel(respond)


def _use(model) -> None:
    message_gate._gate_model = model


@pytest.mark.asyncio
async def test_returns_only_allowed_candidate_ids_in_candidate_order():
    _use(_answering({"candidate_1": True, "candidate_2": False, "candidate_3": True}))
    result = await filter_messages("only python questions", _candidates(), [])
    assert result == ["1", "3"]


@pytest.mark.asyncio
async def test_asks_one_bool_question_per_candidate():
    captured: dict = {}
    _use(_answering({"candidate_1": True, "candidate_2": True, "candidate_3": True}, captured))
    await filter_messages("only python questions", _candidates(), [])

    properties = captured["schema"]["properties"]
    assert list(properties) == ["candidate_1", "candidate_2", "candidate_3"]
    assert {spec["type"] for spec in properties.values()} == {"boolean"}
    # Each question quotes its own candidate so Jev knows which message it judges.
    assert "[2]" in properties["candidate_2"]["description"]
    assert "what's for lunch?" in properties["candidate_2"]["description"]


@pytest.mark.asyncio
async def test_fail_open_returns_all_ids_on_model_error():
    def _boom(messages, info: AgentInfo):
        raise RuntimeError("jev outage")

    _use(FunctionModel(_boom))
    result = await filter_messages("only python questions", _candidates(), [])
    # A model failure must never silence the bot: every candidate is allowed.
    assert result == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_fail_open_when_the_model_cannot_be_built():
    with patch.object(
        message_gate, "get_gate_model", side_effect=RuntimeError("no TypeSafe key")
    ):
        result = await filter_messages("only python questions", _candidates(), [])
    assert result == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_fail_open_on_timeout(monkeypatch):
    async def _slow(messages, info: AgentInfo):
        await asyncio.sleep(1)
        raise AssertionError("the gate should have timed out first")

    monkeypatch.setattr(message_gate, "GATE_TIMEOUT_SECONDS", 0.01)
    _use(FunctionModel(_slow))
    result = await filter_messages("only python questions", _candidates(), [])
    assert result == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_empty_candidates_short_circuits_without_model():
    with patch.object(message_gate, "get_gate_model") as getter:
        result = await filter_messages("only python questions", [], _candidates())
    assert result == []
    getter.assert_not_called()


@pytest.mark.asyncio
async def test_blank_filter_allows_all_without_model():
    with patch.object(message_gate, "get_gate_model") as getter:
        result = await filter_messages("   ", _candidates(), [])
    assert result == ["1", "2", "3"]
    getter.assert_not_called()


@pytest.mark.asyncio
async def test_policy_covers_redirecting_candidates():
    """A candidate like "do you have an answer?" names no topic itself; the
    policy must direct Jev to take the topic from the channel and context."""
    captured: dict = {}
    _use(_answering({"candidate_1": True, "candidate_2": True, "candidate_3": True}, captured))
    await filter_messages("only coding questions", _candidates(), [])

    assert "do you have an answer?" in captured["instructions"]
    assert "CHANNEL name and the CONTEXT" in captured["instructions"]


@pytest.mark.asyncio
async def test_filter_and_channel_name_go_in_instructions_not_material():
    """In a forum post or thread the channel name is its title — often the
    clearest statement of the conversation's topic — so it reaches Jev beside
    the admin's filter, while only the messages are the judged text."""
    captured: dict = {}
    _use(_answering({"candidate_1": True, "candidate_2": True, "candidate_3": True}, captured))
    await filter_messages(
        "only coding questions",
        _candidates(),
        [],
        channel_name="How Does Logits Work?",
    )

    assert "How Does Logits Work?" in captured["instructions"]
    assert "only coding questions" in captured["instructions"]
    assert "only coding questions" not in captured["material"]
    assert "how do decorators work?" in captured["material"]


@pytest.mark.asyncio
async def test_missing_channel_name_renders_no_channel_section():
    captured: dict = {}
    _use(_answering({"candidate_1": True, "candidate_2": True, "candidate_3": True}, captured))
    await filter_messages("only coding questions", _candidates(), [])

    assert "CHANNEL (" not in captured["instructions"]


@pytest.mark.asyncio
async def test_grounding_is_rendered_but_never_judged():
    grounding = [GateMessage(message_id="g1", author_display="dan", content="earlier chatter")]
    captured: dict = {}
    _use(_answering({"candidate_1": True, "candidate_2": False, "candidate_3": False}, captured))
    result = await filter_messages("only python questions", _candidates(), grounding)

    assert result == ["1"]
    assert "earlier chatter" in captured["material"]
    # Only candidates get a question, so a context id can never come back.
    assert "earlier chatter" not in str(captured["schema"]["properties"])


def test_gate_model_is_jev(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    model = message_gate.get_gate_model()
    assert isinstance(model, TypeSafeModel)
    assert model.model_name == "jev-1.13.0"
    assert message_gate.get_gate_model() is model
