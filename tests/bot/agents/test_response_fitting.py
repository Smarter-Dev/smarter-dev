"""Tests for fitting overlong chat replies into Discord's message cap."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

import smarter_dev.bot.agents.response_fitting as response_fitting
from smarter_dev.bot.agents.response_fitting import DISCORD_MESSAGE_LIMIT
from smarter_dev.bot.agents.response_fitting import LENGTH_SUMMARIZER_MODEL_KEY
from smarter_dev.bot.agents.response_fitting import SPLIT_TARGET
from smarter_dev.bot.agents.response_fitting import SUMMARIZE_THRESHOLD
from smarter_dev.bot.agents.response_fitting import _shorten_with_agent
from smarter_dev.bot.agents.response_fitting import fit_overlong_response
from smarter_dev.bot.agents.response_fitting import split_for_discord

# --------------------------------------------------------------------------- #
# split_for_discord
# --------------------------------------------------------------------------- #


def test_length_summarizer_uses_luna():
    assert LENGTH_SUMMARIZER_MODEL_KEY == "gpt-5-6-luna"


def test_split_short_text_passes_through():
    assert split_for_discord("hello") == ["hello"]


def test_split_exactly_at_limit_passes_through():
    text = "a" * DISCORD_MESSAGE_LIMIT
    assert split_for_discord(text) == [text]


def test_split_empty_text_returns_no_parts():
    assert split_for_discord("   ") == []


def test_split_breaks_on_last_newline_before_target():
    lines = ["x" * 99] * 25  # 100-char lines -> newlines at 99, 199, ... 2499
    text = "\n".join(lines)  # 2499 chars
    parts = split_for_discord(text)
    assert len(parts) == 2
    # Last newline at or before index 1500 is after the 15th line.
    assert parts[0] == "\n".join(lines[:15])
    assert parts[1] == "\n".join(lines[15:])
    assert all(len(part) <= DISCORD_MESSAGE_LIMIT for part in parts)


def test_split_without_newline_breaks_on_space():
    words = ("word " * 500).strip()  # 2499 chars, spaces only
    parts = split_for_discord(words)
    assert len(parts) == 2
    assert all(len(part) <= DISCORD_MESSAGE_LIMIT for part in parts)
    assert " ".join(parts) == words


def test_split_without_any_break_point_cuts_hard():
    text = "a" * 2500
    parts = split_for_discord(text)
    assert parts == ["a" * SPLIT_TARGET, "a" * 1000]


def test_split_ignores_newlines_that_would_overflow_the_tail():
    # Only newline is at index 100; splitting there would leave a 2399-char
    # tail. The split must move past it so both parts fit the cap.
    text = "a" * 100 + "\n" + "b" * 2399
    parts = split_for_discord(text)
    assert len(parts) == 2
    assert all(len(part) <= DISCORD_MESSAGE_LIMIT for part in parts)


def test_split_defensive_overlong_tail_is_truncated():
    # >3000 input shouldn't reach here, but must never produce an unsendable part.
    text = "a" * 5000
    parts = split_for_discord(text)
    assert all(len(part) <= DISCORD_MESSAGE_LIMIT for part in parts)
    assert parts[1].endswith("…")


# --------------------------------------------------------------------------- #
# _shorten_with_agent
# --------------------------------------------------------------------------- #


def _agent_returning(message, *, input_tokens=10, output_tokens=5):
    agent = MagicMock()
    response = (
        SimpleNamespace(message=message) if message is not None else None
    )
    agent.run = AsyncMock(
        return_value=SimpleNamespace(
            output=SimpleNamespace(response=response),
            usage=lambda: SimpleNamespace(
                input_tokens=input_tokens, output_tokens=output_tokens
            ),
        )
    )
    return agent


@pytest.mark.asyncio
async def test_shorten_returns_rewrite_and_usage():
    agent = _agent_returning("short version")
    text, input_tokens, output_tokens = await _shorten_with_agent(
        "x" * 4000, agent, deps=None, message_history=[]
    )
    assert text == "short version"
    assert (input_tokens, output_tokens) == (10, 5)
    prompt = agent.run.await_args.kwargs["user_prompt"]
    assert "4000" in prompt


@pytest.mark.asyncio
async def test_shorten_no_response_returns_none():
    agent = _agent_returning(None)
    text, _, _ = await _shorten_with_agent("x" * 4000, agent, None, [])
    assert text is None


@pytest.mark.asyncio
async def test_shorten_run_failure_degrades_to_none():
    agent = MagicMock()
    agent.run = AsyncMock(side_effect=RuntimeError("model down"))
    text, input_tokens, output_tokens = await _shorten_with_agent(
        "x" * 4000, agent, None, []
    )
    assert (text, input_tokens, output_tokens) == (None, 0, 0)


# --------------------------------------------------------------------------- #
# fit_overlong_response tiers
# --------------------------------------------------------------------------- #

LONG = "z" * 4000


@pytest.mark.asyncio
async def test_fit_uses_agent_rewrite_when_it_fits(monkeypatch):
    monkeypatch.setattr(
        response_fitting,
        "_shorten_with_agent",
        AsyncMock(return_value=("rewritten", 11, 7)),
    )
    summarize = AsyncMock()
    monkeypatch.setattr(response_fitting, "_summarize_with_luna", summarize)

    fit = await fit_overlong_response(LONG, agent=None, deps=None, message_history=[])

    assert fit.text == "rewritten"
    assert fit.method == "shortened"
    assert (fit.extra_input_tokens, fit.extra_output_tokens) == (11, 7)
    summarize.assert_not_called()


@pytest.mark.asyncio
async def test_fit_falls_back_to_summarizer_when_rewrite_still_long(monkeypatch):
    monkeypatch.setattr(
        response_fitting,
        "_shorten_with_agent",
        AsyncMock(return_value=("y" * (SUMMARIZE_THRESHOLD + 1), 11, 7)),
    )
    summarize = AsyncMock(return_value="a tidy summary")
    monkeypatch.setattr(response_fitting, "_summarize_with_luna", summarize)

    fit = await fit_overlong_response(LONG, agent=None, deps=None, message_history=[])

    assert fit.text == "a tidy summary"
    assert fit.method == "summarized"
    # The failed rewrite still spent chat-model tokens — they must be metered.
    assert (fit.extra_input_tokens, fit.extra_output_tokens) == (11, 7)
    summarize.assert_awaited_once_with(LONG)


@pytest.mark.asyncio
async def test_fit_truncates_when_everything_fails(monkeypatch):
    monkeypatch.setattr(
        response_fitting, "_shorten_with_agent", AsyncMock(return_value=(None, 0, 0))
    )
    monkeypatch.setattr(
        response_fitting, "_summarize_with_luna", AsyncMock(return_value=None)
    )

    fit = await fit_overlong_response(LONG, agent=None, deps=None, message_history=[])

    assert fit.method == "truncated"
    assert fit.text.endswith("…")
    assert len(fit.text) <= DISCORD_MESSAGE_LIMIT
