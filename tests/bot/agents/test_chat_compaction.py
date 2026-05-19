"""Tests for ``chat_compaction.compact_history``.

The summariser agent is patched out — these tests verify the *plumbing*:
which parts get replaced, which pass through, and that compactions are
self-stabilising across repeated processor runs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from smarter_dev.bot.agents.chat_compaction import (
    COMPACT_THRESHOLD_CHARS,
    compact_history,
)


def _long_text(n: int = COMPACT_THRESHOLD_CHARS + 500) -> str:
    return "x" * n


def _short_text() -> str:
    return "short"


def _request(*parts) -> ModelRequest:
    return ModelRequest(parts=list(parts))


def _response(*parts) -> ModelResponse:
    return ModelResponse(parts=list(parts))


def _stub_summary(label: str, text: str):
    from smarter_dev.bot.agents.chat_compaction import _SummariseResult

    return _SummariseResult(
        text=f"[compacted] summary-of-{label}",
        tokens_input=10,
        tokens_output=5,
        model_name="stub-model",
    )


@pytest.fixture
def patched_summarise():
    """Replace the model-driven summariser with a deterministic stub."""
    with patch(
        "smarter_dev.bot.agents.chat_compaction._summarise",
        side_effect=lambda label, text: _stub_summary(label, text),
        new_callable=AsyncMock,
    ) as m:
        yield m


@pytest.mark.asyncio
async def test_short_parts_pass_through_unchanged(patched_summarise):
    messages = [
        _request(UserPromptPart(content=_short_text())),
        _response(TextPart(content=_short_text())),
        # current turn
        _request(UserPromptPart(content="new turn input")),
    ]
    out = await compact_history(messages)
    # Same identity (or value equality) — and summariser never called.
    assert patched_summarise.await_count == 0
    assert len(out) == len(messages)
    assert out[0].parts[0].content == _short_text()
    assert out[1].parts[0].content == _short_text()


@pytest.mark.asyncio
async def test_long_user_prompt_summarised(patched_summarise):
    long = _long_text()
    messages = [
        _request(UserPromptPart(content=long)),
        _response(TextPart(content="ok")),
        _request(UserPromptPart(content="new turn input")),
    ]
    out = await compact_history(messages)
    assert patched_summarise.await_count == 1
    assert out[0].parts[0].content.startswith("[compacted]")
    assert "USER_PROMPT" in out[0].parts[0].content


@pytest.mark.asyncio
async def test_long_assistant_text_summarised(patched_summarise):
    messages = [
        _request(UserPromptPart(content="q")),
        _response(TextPart(content=_long_text())),
        _request(UserPromptPart(content="follow-up")),
    ]
    out = await compact_history(messages)
    assert patched_summarise.await_count == 1
    assert out[1].parts[0].content.startswith("[compacted]")
    assert "ASSISTANT_TEXT" in out[1].parts[0].content


@pytest.mark.asyncio
async def test_long_tool_return_summarised(patched_summarise):
    messages = [
        _request(UserPromptPart(content="q")),
        _response(ToolCallPart(tool_name="web_search", args={"q": "x"}, tool_call_id="t1")),
        _request(ToolReturnPart(tool_name="web_search", content=_long_text(), tool_call_id="t1")),
        _response(TextPart(content="short reply")),
        _request(UserPromptPart(content="follow-up")),
    ]
    out = await compact_history(messages)
    assert patched_summarise.await_count == 1
    assert out[2].parts[0].content.startswith("[compacted]")
    assert "TOOL_RETURN" in out[2].parts[0].content


@pytest.mark.asyncio
async def test_current_turn_never_compacted(patched_summarise):
    """Even if the just-added current turn has long content, it passes through."""
    messages = [
        _request(UserPromptPart(content="q")),
        _response(TextPart(content="ok")),
        _request(UserPromptPart(content=_long_text())),  # current turn — keep!
    ]
    out = await compact_history(messages)
    assert patched_summarise.await_count == 0
    # The current turn (last ModelRequest) is intact, including its long content.
    assert out[-1].parts[0].content == _long_text()


@pytest.mark.asyncio
async def test_already_compacted_parts_passed_through(patched_summarise):
    """Running the processor a second time after a compaction shouldn't
    re-summarise — the summary stub is short so the threshold check skips it."""
    messages = [
        _request(UserPromptPart(content=_long_text())),
        _response(TextPart(content="ok")),
        _request(UserPromptPart(content="new turn input")),
    ]
    first = await compact_history(messages)
    assert patched_summarise.await_count == 1

    # Simulate Pydantic AI persisting + reloading + appending a new turn.
    second_round = list(first) + [
        _response(TextPart(content="reply")),
        _request(UserPromptPart(content="another new turn")),
    ]
    out = await compact_history(second_round)
    # Still only the one original call — second pass found nothing long.
    assert patched_summarise.await_count == 1
    # Old summary is preserved verbatim.
    assert out[0].parts[0].content == first[0].parts[0].content


@pytest.mark.asyncio
async def test_typical_initial_activation_input_passes_through(patched_summarise):
    """Regression for the production bug: a typical 10-message AgentInput
    JSON (~3-5k chars) must NOT be compacted at the 10k threshold."""
    # Realistic-ish 4500 char blob to mimic a real first-turn AgentInput.
    realistic_initial = "y" * 4500
    messages = [
        _request(UserPromptPart(content=realistic_initial)),
        _response(TextPart(content="ok")),
        _request(UserPromptPart(content="follow-up 1")),
        _response(TextPart(content="ack")),
        _request(UserPromptPart(content="current turn")),
    ]
    out = await compact_history(messages)
    assert patched_summarise.await_count == 0
    assert out[0].parts[0].content == realistic_initial


@pytest.mark.asyncio
async def test_compaction_events_recorded_when_collector_active(patched_summarise):
    """The processor should append a CompactionEvent for every part it
    summarises into the per-run ContextVar collector. The engine drains this
    after agent.run() to persist alongside the turn."""
    from smarter_dev.bot.agents.chat_compaction import (
        drain_collection,
        start_collection,
    )

    long = _long_text()
    messages = [
        _request(UserPromptPart(content=long)),
        _response(TextPart(content=_long_text())),
        _request(UserPromptPart(content="current turn")),
    ]

    start_collection()
    await compact_history(messages)
    events = drain_collection()

    assert len(events) == 2
    kinds = sorted(e.event_kind for e in events)
    assert kinds == ["assistant_text", "user_prompt"]
    for e in events:
        assert e.original_chars > 0
        assert e.summary_chars > 0
        assert e.summarizer_tokens_input == 10
        assert e.summarizer_tokens_output == 5
        assert e.summarizer_model_name == "stub-model"

    # Drained — collector is reset.
    assert drain_collection() == []


@pytest.mark.asyncio
async def test_compaction_events_silently_dropped_when_no_collector(patched_summarise):
    """If no collector is active (e.g. tests that don't call start_collection),
    the processor still works — it just doesn't record events."""
    long = _long_text()
    messages = [
        _request(UserPromptPart(content=long)),
        _response(TextPart(content="ok")),
        _request(UserPromptPart(content="current turn")),
    ]
    # No start_collection() call — collector is None.
    await compact_history(messages)
    # No exception, summariser still called.
    assert patched_summarise.await_count == 1


@pytest.mark.asyncio
async def test_empty_history_passes_through(patched_summarise):
    messages = []
    out = await compact_history(messages)
    assert out == []
    assert patched_summarise.await_count == 0


@pytest.mark.asyncio
async def test_single_message_passes_through(patched_summarise):
    """Only the current turn exists — there's nothing to compact."""
    messages = [_request(UserPromptPart(content=_long_text()))]
    out = await compact_history(messages)
    assert out == messages
    assert patched_summarise.await_count == 0
