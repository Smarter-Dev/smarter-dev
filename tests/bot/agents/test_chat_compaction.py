"""Tests for ``chat_compaction.compact_history`` (conversation-level).

The summariser agent is patched out — these tests verify the *plumbing*:
when the fold triggers, where the cut lands, that tool call/return pairs
never split, that the system prompt survives, and that repeated runs are
self-stabilising.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from smarter_dev.bot.agents.chat_compaction import (
    COMPACT_TRIGGER_CHARS,
    COMPACTED_PREFIX,
    KEEP_RECENT_CHARS,
    CompactionEvent,
    compact_history,
    drain_collection,
    start_collection,
)

STUB_SUMMARY_TEXT = f"{COMPACTED_PREFIX} alice (id 1) asked about webhooks."


def _stub_result():
    from smarter_dev.bot.agents.chat_compaction import _SummariseResult

    return _SummariseResult(
        text=STUB_SUMMARY_TEXT,
        tokens_input=10,
        tokens_output=5,
        model_name="stub-model",
    )


@pytest.fixture
def patched_summarise():
    """Replace the model-driven summariser with a deterministic stub."""
    with patch(
        "smarter_dev.bot.agents.chat_compaction._summarise_conversation",
        new_callable=AsyncMock,
        return_value=_stub_result(),
    ) as m:
        yield m


def _user_turn(text: str, reply: str = "ok", *, system: bool = False) -> list:
    """One full prior turn: user request + assistant response."""
    req_parts = ([SystemPromptPart(content="sys")] if system else []) + [
        UserPromptPart(content=text)
    ]
    return [
        ModelRequest(parts=req_parts),
        ModelResponse(parts=[TextPart(content=reply)]),
    ]


def _tool_turn(text: str, tool_return: str) -> list:
    """A turn where the agent called a tool before answering."""
    return [
        ModelRequest(parts=[UserPromptPart(content=text)]),
        ModelResponse(parts=[ToolCallPart(tool_name="web_search", args='{"q":"x"}')]),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="web_search", content=tool_return, tool_call_id="tc1"
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content="answered")]),
    ]


def _current_turn() -> list:
    return [ModelRequest(parts=[UserPromptPart(content="newest message")])]


BIG = "x" * 12_000  # one of these per turn; a few cross the trigger
SMALL = "hello"


@pytest.mark.asyncio
async def test_short_history_untouched(patched_summarise):
    messages = _user_turn(SMALL, system=True) + _user_turn(SMALL) + _current_turn()
    out = await compact_history(list(messages))
    assert out == messages
    assert patched_summarise.await_count == 0


@pytest.mark.asyncio
async def test_long_history_folds_old_turns(patched_summarise):
    turns = _user_turn(BIG, system=True)
    for _ in range(4):
        turns += _user_turn(BIG)
    messages = turns + _current_turn()

    out = await compact_history(list(messages))

    assert patched_summarise.await_count == 1
    # First message is the summary request, carrying the system prompt.
    first = out[0]
    assert isinstance(first, ModelRequest)
    assert isinstance(first.parts[0], SystemPromptPart)
    assert isinstance(first.parts[-1], UserPromptPart)
    assert first.parts[-1].content == STUB_SUMMARY_TEXT
    # Result is smaller and still ends with the untouched current turn.
    assert len(out) < len(messages)
    assert out[-1] is messages[-1]


@pytest.mark.asyncio
async def test_kept_window_respects_budget(patched_summarise):
    turns = _user_turn(BIG, system=True)
    for _ in range(5):
        turns += _user_turn(BIG)
    messages = turns + _current_turn()

    out = await compact_history(list(messages))

    kept = out[1:-1]  # between summary request and current turn
    kept_chars = sum(
        len(p.content)
        for m in kept
        for p in m.parts
        if isinstance(p, (UserPromptPart, TextPart))
    )
    assert kept_chars <= KEEP_RECENT_CHARS
    # But at least one prior turn stayed verbatim.
    assert any(
        isinstance(p, UserPromptPart) and p.content == BIG
        for m in kept
        for p in m.parts
    )


@pytest.mark.asyncio
async def test_tool_pairs_never_split(patched_summarise):
    """A cut point never lands between a tool call and its return."""
    turns = _user_turn(BIG, system=True) + _user_turn(BIG)
    turns += _tool_turn(BIG, tool_return=BIG)
    turns += _user_turn(SMALL)
    messages = turns + _current_turn()

    out = await compact_history(list(messages))

    def _part_kinds(msgs):
        return [type(p).__name__ for m in msgs for p in m.parts]

    kinds = _part_kinds(out)
    # Every kept ToolReturnPart must be preceded (somewhere after the
    # summary) by its ToolCallPart — i.e. a return never appears without
    # its call in the same retained window.
    if "ToolReturnPart" in kinds:
        assert kinds.index("ToolCallPart") < kinds.index("ToolReturnPart")


@pytest.mark.asyncio
async def test_current_turn_never_compacted(patched_summarise):
    messages = _user_turn(BIG, system=True) + _user_turn(BIG) + [
        ModelRequest(parts=[UserPromptPart(content=BIG * 3)])
    ]
    out = await compact_history(list(messages))
    # However huge, the current request is byte-identical.
    assert out[-1] is messages[-1]


@pytest.mark.asyncio
async def test_summary_merges_on_next_fold(patched_summarise):
    """A prior summary sits at the head of 'old' on the next fold."""
    turns = _user_turn(BIG, system=True)
    for _ in range(4):
        turns += _user_turn(BIG)
    out1 = await compact_history(turns + _current_turn())

    # Grow the conversation again past the trigger.
    grown = out1[:-1]
    for _ in range(4):
        grown += _user_turn(BIG)
    out2 = await compact_history(grown + _current_turn())

    assert patched_summarise.await_count == 2
    # Second fold's transcript includes the first summary text.
    transcript = patched_summarise.await_args_list[1].args[0]
    assert STUB_SUMMARY_TEXT in transcript


@pytest.mark.asyncio
async def test_summariser_failure_leaves_history_untouched(patched_summarise):
    patched_summarise.return_value = None
    turns = _user_turn(BIG, system=True)
    for _ in range(4):
        turns += _user_turn(BIG)
    messages = turns + _current_turn()

    out = await compact_history(list(messages))
    assert out == messages


@pytest.mark.asyncio
async def test_events_recorded_when_collector_active(patched_summarise):
    turns = _user_turn(BIG, system=True)
    for _ in range(4):
        turns += _user_turn(BIG)
    messages = turns + _current_turn()

    start_collection()
    await compact_history(list(messages))
    events = drain_collection()

    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, CompactionEvent)
    assert ev.event_kind == "conversation"
    assert ev.summary == STUB_SUMMARY_TEXT
    assert ev.original_chars > 0
    assert ev.summarizer_model_name == "stub-model"


@pytest.mark.asyncio
async def test_events_dropped_without_collector(patched_summarise):
    drain_collection()  # ensure no active bucket
    turns = _user_turn(BIG, system=True)
    for _ in range(4):
        turns += _user_turn(BIG)
    await compact_history(turns + _current_turn())
    assert drain_collection() == []


@pytest.mark.asyncio
async def test_empty_and_single_message_pass_through(patched_summarise):
    assert await compact_history([]) == []
    single = [ModelRequest(parts=[UserPromptPart(content=BIG * 5)])]
    assert await compact_history(list(single)) == single
    assert patched_summarise.await_count == 0


@pytest.mark.asyncio
async def test_trigger_boundary_exact(patched_summarise):
    """At exactly the trigger, no fold; one char past, fold."""
    half = COMPACT_TRIGGER_CHARS // 2
    messages = (
        _user_turn("x" * half, reply="", system=True)
        + _user_turn("x" * half, reply="")
        + _current_turn()
    )
    out = await compact_history(list(messages))
    assert out == messages

    messages_over = (
        _user_turn("x" * half, reply="", system=True)
        + _user_turn("x" * (half + 1), reply="")
        + _current_turn()
    )
    out2 = await compact_history(list(messages_over))
    assert out2 != messages_over
