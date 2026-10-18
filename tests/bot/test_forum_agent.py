"""Tests for ForumMonitorAgent on pydantic-ai.

Each test swaps a FunctionModel in for the routed model, so these check what the
agent sends, what it returns to forum_agent_service and the tokens it reports.
"""

from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.agents import forum_agent
from smarter_dev.bot.agents.forum_agent import ForumMonitorAgent


@pytest.fixture(autouse=True)
def _model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real model is built before the test swaps in its FunctionModel.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _prompt(messages: list[ModelMessage]) -> str:
    return "\n".join(
        part.content
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )


def _answering(payload: dict, seen: list[str] | None = None) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(_prompt(messages))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    return FunctionModel(respond)


def _failing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise RuntimeError("AI service unavailable")


_EVALUATION = {
    "decision": "Should respond to this question",
    "confidence": 0.85,
    "response": " Here's a helpful response to your question. ",
}


async def _evaluate(model: FunctionModel, *args) -> tuple[str, float, str, int]:
    args = args or ("Help with React", "Title", "Content", "Author", [], [])
    with forum_agent.get_forum_monitor_agent().override(model=model):
        return await ForumMonitorAgent().evaluate_post(*args)


# -- evaluate_post -----------------------------------------------------------------


async def test_evaluate_post_returns_the_stored_fields_and_real_tokens() -> None:
    decision, confidence, response, tokens = await _evaluate(_answering(_EVALUATION))

    assert decision == "Should respond to this question"
    assert confidence == 0.85
    assert response == "Here's a helpful response to your question."
    assert isinstance(tokens, int)
    assert tokens > 0


async def test_evaluate_post_sends_the_prompt_and_formatted_post() -> None:
    seen: list[str] = []

    await _evaluate(
        _answering(_EVALUATION, seen),
        "Help with React questions",
        "React useState hook question",
        "How do I update state <properly>?",
        "ReactLearner",
        ["react", "hooks"],
        ["component.jsx"],
    )

    prompt = seen[0]
    assert "## system_prompt\nHelp with React questions" in prompt
    assert "## post_context\n<post>" in prompt
    assert "<title>React useState hook question</title>" in prompt
    assert "<author>ReactLearner</author>" in prompt
    assert "<content>How do I update state &lt;properly&gt;?</content>" in prompt
    assert "<tags>react, hooks</tags>" in prompt
    assert "<attachments>component.jsx</attachments>" in prompt


async def test_evaluate_post_with_no_response() -> None:
    _, confidence, response, _ = await _evaluate(
        _answering({"decision": "Off topic", "confidence": 0.1, "response": ""})
    )

    assert (confidence, response) == (0.1, "")


@pytest.mark.parametrize(
    ("given", "stored"), [(1.5, 1.0), (-0.2, 0.0), (0.5, 0.5), (0.0, 0.0), (1.0, 1.0)]
)
async def test_confidence_is_clamped_to_the_unit_range(given: float, stored: float) -> None:
    _, confidence, _, _ = await _evaluate(
        _answering({"decision": "d", "confidence": given, "response": "r"})
    )

    assert confidence == stored


async def test_evaluate_post_errors_propagate_to_the_service() -> None:
    with pytest.raises(RuntimeError, match="AI service unavailable"):
        await _evaluate(FunctionModel(_failing))


# -- classify_topics_only ----------------------------------------------------------


async def test_classification_keeps_only_the_forums_topics() -> None:
    seen: list[str] = []
    model = _answering({"matching_topics": ["react", "made-up", "python"]}, seen)

    with forum_agent.get_forum_topic_classifier_agent().override(model=model):
        topics, tokens = await ForumMonitorAgent().classify_topics_only(
            ["python", "react", "rust"], "Title", "Content", "Author"
        )

    assert topics == ["react", "python"]
    assert tokens > 0
    assert f"## available_topics\n{json.dumps(['python', 'react', 'rust'])}" in seen[0]
    assert "## system_prompt" not in seen[0]


# -- evaluate_post_combined --------------------------------------------------------


async def test_combined_returns_all_five_fields() -> None:
    seen: list[str] = []
    model = _answering(
        {**_EVALUATION, "confidence": 3, "matching_topics": ["rust", "go"]}, seen
    )

    with forum_agent.get_forum_combined_agent().override(model=model):
        result = await ForumMonitorAgent().evaluate_post_combined(
            "Help with Rust", ["rust", "python"], "Title", "Content", "Author"
        )

    decision, confidence, response, topics, tokens = result
    assert decision == "Should respond to this question"
    assert confidence == 1.0
    assert response == "Here's a helpful response to your question."
    assert topics == ["rust"]
    assert tokens > 0
    assert "## system_prompt\nHelp with Rust" in seen[0]
    assert f"## available_topics\n{json.dumps(['rust', 'python'])}" in seen[0]


# -- prompts and model -------------------------------------------------------------


def test_each_agent_runs_on_claude_haiku_through_the_router() -> None:
    for agent in (
        forum_agent.get_forum_monitor_agent(),
        forum_agent.get_forum_topic_classifier_agent(),
        forum_agent.get_forum_combined_agent(),
    ):
        assert isinstance(agent.model, AnthropicModel)
        assert agent.model.model_name == "claude-haiku-5-5"


def test_the_instructions_keep_the_confidence_definition() -> None:
    for instructions in (
        forum_agent.FORUM_MONITOR_INSTRUCTIONS,
        forum_agent.FORUM_COMBINED_INSTRUCTIONS,
    ):
        assert "CONFIDENCE SCORE - CRITICAL UNDERSTANDING" in instructions
    assert "If no topics match, return an empty list" in (
        forum_agent.FORUM_TOPIC_CLASSIFICATION_INSTRUCTIONS
    )
