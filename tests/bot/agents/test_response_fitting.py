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
from smarter_dev.bot.agents.response_fitting import _ShortenOutcome
from smarter_dev.bot.agents.response_fitting import _shorten_with_agent
from smarter_dev.bot.agents.response_fitting import fit_overlong_response
from smarter_dev.bot.agents.response_fitting import fit_writer_message
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


def _outcome(text, input_tokens, output_tokens, messages):
    return _ShortenOutcome(text, input_tokens, output_tokens, list(messages))


def _agent_returning(
    message, *, input_tokens=10, output_tokens=5, rerun_messages=None
):
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
            new_messages=lambda: list(rerun_messages or []),
        )
    )
    return agent


@pytest.mark.asyncio
async def test_shorten_returns_rewrite_and_usage():
    agent = _agent_returning("short version")
    outcome = await _shorten_with_agent(
        "x" * 4000, agent, deps=None, message_history=[]
    )
    assert outcome.text == "short version"
    assert (outcome.input_tokens, outcome.output_tokens) == (10, 5)
    prompt = agent.run.await_args.kwargs["user_prompt"]
    assert "4000" in prompt


@pytest.mark.asyncio
async def test_shorten_no_response_returns_none():
    agent = _agent_returning(None)
    outcome = await _shorten_with_agent("x" * 4000, agent, None, [])
    assert outcome.text is None


@pytest.mark.asyncio
async def test_shorten_run_failure_degrades_to_none():
    agent = MagicMock()
    agent.run = AsyncMock(side_effect=RuntimeError("model down"))
    outcome = await _shorten_with_agent("x" * 4000, agent, None, [])
    assert (outcome.text, outcome.input_tokens, outcome.output_tokens) == (
        None,
        0,
        0,
    )
    assert outcome.messages == []


# --------------------------------------------------------------------------- #
# fit_overlong_response tiers
# --------------------------------------------------------------------------- #

LONG = "z" * 4000


@pytest.mark.asyncio
async def test_fit_uses_agent_rewrite_when_it_fits(monkeypatch):
    monkeypatch.setattr(
        response_fitting,
        "_shorten_with_agent",
        AsyncMock(return_value=_outcome("rewritten", 11, 7, ["m1", "m2"])),
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
        AsyncMock(
            return_value=_outcome("y" * (SUMMARIZE_THRESHOLD + 1), 11, 7, ["m1"])
        ),
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
        response_fitting,
        "_shorten_with_agent",
        AsyncMock(return_value=_outcome(None, 0, 0, [])),
    )
    monkeypatch.setattr(
        response_fitting, "_summarize_with_luna", AsyncMock(return_value=None)
    )

    fit = await fit_overlong_response(LONG, agent=None, deps=None, message_history=[])

    assert fit.method == "truncated"
    assert fit.text.endswith("…")
    assert len(fit.text) <= DISCORD_MESSAGE_LIMIT


# --------------------------------------------------------------------------- #
# fit_writer_message (two-stage, summarizer-only)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fit_writer_message_uses_summarizer_when_it_fits(monkeypatch):
    summarize = AsyncMock(return_value="a tidy summary")
    monkeypatch.setattr(response_fitting, "_summarize_with_luna", summarize)

    fit = await fit_writer_message(LONG)

    assert fit.text == "a tidy summary"
    assert fit.method == "summarized"
    # Luna's spend is not metered against the channel budget.
    assert (fit.extra_input_tokens, fit.extra_output_tokens) == (0, 0)
    summarize.assert_awaited_once_with(LONG)


@pytest.mark.asyncio
async def test_fit_writer_message_truncates_when_summarizer_fails(monkeypatch):
    monkeypatch.setattr(
        response_fitting, "_summarize_with_luna", AsyncMock(return_value=None)
    )

    fit = await fit_writer_message(LONG)

    assert fit.method == "truncated"
    assert fit.text.endswith("…")
    assert len(fit.text) <= DISCORD_MESSAGE_LIMIT
    assert (fit.extra_input_tokens, fit.extra_output_tokens) == (0, 0)


@pytest.mark.asyncio
async def test_fit_writer_message_truncates_when_summary_still_too_long(monkeypatch):
    monkeypatch.setattr(
        response_fitting,
        "_summarize_with_luna",
        AsyncMock(return_value="q" * (SUMMARIZE_THRESHOLD + 1)),
    )

    fit = await fit_writer_message(LONG)

    assert fit.method == "truncated"
    assert len(fit.text) <= DISCORD_MESSAGE_LIMIT


# --------------------------------------------------------------------------- #
# The shorten re-run's messages must reach the persisted turn
#
# The 2026-07-28 incident happened inside this re-run — 16 run_code calls
# counting characters — and none of it was recoverable from the DB, because
# only the main run's new_messages() is persisted. Reconstructing it needed
# raw Discord history.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_shorten_returns_the_rerun_messages():
    agent = _agent_returning("short version", rerun_messages=["req", "resp"])
    outcome = await _shorten_with_agent(
        "x" * 4000, agent, deps=None, message_history=[]
    )
    assert outcome.messages == ["req", "resp"]


@pytest.mark.asyncio
async def test_shorten_messages_survive_into_the_fit_result(monkeypatch):
    monkeypatch.setattr(
        response_fitting,
        "_shorten_with_agent",
        AsyncMock(return_value=_outcome("rewritten", 11, 7, ["m1", "m2"])),
    )
    monkeypatch.setattr(response_fitting, "_summarize_with_luna", AsyncMock())

    fit = await fit_overlong_response(LONG, agent=None, deps=None, message_history=[])

    assert fit.extra_messages == ["m1", "m2"]


@pytest.mark.asyncio
async def test_rerun_messages_kept_even_when_the_rewrite_is_discarded(monkeypatch):
    """A rewrite that still overruns is thrown away — but it made the tool
    calls, so its messages are exactly what an investigation needs."""
    monkeypatch.setattr(
        response_fitting,
        "_shorten_with_agent",
        AsyncMock(
            return_value=_outcome("y" * (SUMMARIZE_THRESHOLD + 1), 11, 7, ["m1"])
        ),
    )
    monkeypatch.setattr(
        response_fitting, "_summarize_with_luna", AsyncMock(return_value="tidy")
    )

    fit = await fit_overlong_response(LONG, agent=None, deps=None, message_history=[])

    assert fit.method == "summarized"
    assert fit.extra_messages == ["m1"]


@pytest.mark.asyncio
async def test_failed_rerun_contributes_no_messages(monkeypatch):
    monkeypatch.setattr(
        response_fitting,
        "_shorten_with_agent",
        AsyncMock(return_value=_outcome(None, 0, 0, [])),
    )
    monkeypatch.setattr(
        response_fitting, "_summarize_with_luna", AsyncMock(return_value=None)
    )

    fit = await fit_overlong_response(LONG, agent=None, deps=None, message_history=[])

    assert fit.method == "truncated"
    assert fit.extra_messages == []


@pytest.mark.asyncio
async def test_writer_fit_has_no_rerun_messages():
    """Two-stage's writer is tool-less and never re-runs an agent."""
    fit = await fit_writer_message("z" * 5000)
    assert fit.extra_messages == []
