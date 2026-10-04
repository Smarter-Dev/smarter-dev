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
