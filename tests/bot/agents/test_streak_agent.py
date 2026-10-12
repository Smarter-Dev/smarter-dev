"""The streak celebration on pydantic-ai.

The prompt quotes the member's message, so a failure is logged as its type,
never its text.
"""

from __future__ import annotations

import logging

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.agents import streak_agent


@pytest.fixture(autouse=True)
def _openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real model is built before the test swaps in its FunctionModel.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def _prompt(messages: list[ModelMessage]) -> str:
    return "\n".join(
        part.content
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )


def _answering(response: str, seen: list[str] | None = None) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(_prompt(messages))
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"response": response})]
        )

    return FunctionModel(respond)


async def _celebrate(model: FunctionModel, **overrides) -> tuple[str, int]:
    kwargs = {
        "bytes_earned": 32,
        "streak_multiplier": 4,
        "streak_days": 48,
        "user_id": 42,
        "user_message": "hi",
        **overrides,
    }
    with streak_agent.get_streak_celebration_agent().override(model=model):
        return await streak_agent.StreakCelebrationAgent().generate_celebration_message(
            **kwargs
        )


async def test_a_provider_error_echoing_the_message_logs_only_its_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def echoing_provider(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ValueError(f"bad request: {_prompt(messages)}")

    with caplog.at_level(logging.DEBUG, logger=streak_agent.logger.name):
        result = await _celebrate(
            FunctionModel(echoing_provider), user_message="my private words"
        )

    assert result == ("", 0)
    assert "ValueError" in caplog.text
    assert "my private words" not in caplog.text


async def test_the_celebration_mentions_the_member_and_reports_real_tokens() -> None:
    seen: list[str] = []

    message, tokens = await _celebrate(
        _answering("  Your 48-day run earned 32 BYTES!  ", seen)
    )

    assert message == "-# <@42> Your 48-day run earned 32 BYTES!"
    assert tokens > 0
    for label, value in (
        ("bytes_earned", "32"),
        ("streak_multiplier", "4"),
        ("streak_days", "48"),
        ("user_message", "hi"),
    ):
        assert f"## {label}\n{value}" in seen[0]


async def test_an_empty_response_posts_nothing() -> None:
    assert await _celebrate(_answering("   ")) == ("", 0)


async def test_no_bonus_means_no_model_call() -> None:
    def never(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("the model must not be called without a bonus")

    assert await _celebrate(FunctionModel(never), streak_multiplier=1) == ("", 0)


def test_the_instructions_tell_the_model_not_to_greet_by_time_of_day() -> None:
    assert "greet by time of day" in streak_agent.STREAK_CELEBRATION_INSTRUCTIONS
