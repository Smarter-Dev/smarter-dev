"""The streak celebration prompt quotes the member's message, so a failure
is logged as its type, never its text."""

from __future__ import annotations

import logging

import pytest

from smarter_dev.bot.agents import streak_agent


async def test_a_provider_error_echoing_the_message_logs_only_its_type(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def echoing_provider(**kwargs):
        raise ValueError(f"bad request: {kwargs['user_message']}")

    monkeypatch.setattr(streak_agent.dspy, "asyncify", lambda _agent: echoing_provider)

    with caplog.at_level(logging.DEBUG, logger=streak_agent.logger.name):
        result = await streak_agent.StreakCelebrationAgent().generate_celebration_message(
            bytes_earned=10,
            streak_multiplier=2,
            streak_days=3,
            user_id=42,
            user_message="my private words",
        )

    assert result == ("", 0)
    assert "ValueError" in caplog.text
    assert "my private words" not in caplog.text


def _stub_prediction(monkeypatch: pytest.MonkeyPatch, response: str) -> None:
    class Prediction:
        def __init__(self) -> None:
            self.response = response

        def get_lm_usage(self) -> dict:
            return {"total_tokens": 7}

    async def provider(**kwargs):
        return Prediction()

    monkeypatch.setattr(streak_agent.dspy, "asyncify", lambda _agent: provider)


async def _celebrate() -> tuple[str, int]:
    return await streak_agent.StreakCelebrationAgent().generate_celebration_message(
        bytes_earned=32, streak_multiplier=4, streak_days=48, user_id=42, user_message="hi"
    )


async def test_a_loose_completed_marker_is_not_posted(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_prediction(monkeypatch, "Your 48-day run earned 32 BYTES!\n[[ ## completed ##]]")

    assert await _celebrate() == ("-# <@42> Your 48-day run earned 32 BYTES!", 7)


async def test_a_response_that_is_only_a_marker_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_prediction(monkeypatch, "[[ ## completed ##]]")

    assert await _celebrate() == ("", 0)


def test_the_signature_tells_the_model_not_to_greet_by_time_of_day() -> None:
    assert "greet by time of day" in streak_agent.StreakCelebrationSignature.__doc__
