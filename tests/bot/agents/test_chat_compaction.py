"""Tests for ``chat_compaction.compact_history`` (cost-model driven).

The summariser agent is patched out — these tests verify the *plumbing*
(where the cut lands, tool-pair integrity, system-prompt survival,
self-stabilising merges) and the *economics* (fold on cold cache, hold on
warm, thresholds per model price).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    CACHE_TTL_SECONDS,
    COMPACTED_PREFIX,
    KEEP_RECENT_CHARS,
    CompactionEvent,
    _should_fold,
    compact_history,
    drain_collection,
    set_last_model_call,
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


@pytest.fixture(autouse=True)
def cold_cache():
    """Default every test to a cold cache; tests opt into warm."""
    set_last_model_call(None)
    yield
    set_last_model_call(None)


def _warm():
    set_last_model_call(datetime.now(UTC) - timedelta(seconds=60))


def _cold():
    set_last_model_call(
        datetime.now(UTC) - timedelta(seconds=CACHE_TTL_SECONDS + 60)
    )


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


def _long_history(n_turns: int = 5) -> list:
    turns = _user_turn(BIG, system=True)
    for _ in range(n_turns - 1):
        turns += _user_turn(BIG)
    return turns


BIG = "x" * 12_000  # per turn; a few of these make folding profitable
SMALL = "hello"


# ---------------------------------------------------------------------------
# The cost model itself (token-level unit tests; default Flash Lite prices
# unless CHAT_AGENT_MODEL says otherwise)
# ---------------------------------------------------------------------------


def test_should_fold_flash_lite_cold_threshold(monkeypatch):
    monkeypatch.delenv("CHAT_AGENT_MODEL", raising=False)
    # (p + n*c)(F - S) >= Sigma crosses at F ~= 9375 tokens
    assert not _should_fold(9_000, 3_000, cache_warm=False)
    assert _should_fold(10_000, 3_000, cache_warm=False)


def test_should_fold_flash_lite_never_warm(monkeypatch):
    monkeypatch.delenv("CHAT_AGENT_MODEL", raising=False)
    # Cached savings (0.025*n) accrue slower than the summariser's own
    # input rate (0.25) — no F makes a warm fold profitable.
    assert not _should_fold(1_000_000, 0, cache_warm=True)


def test_should_fold_luna_cold_threshold(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_MODEL", "gpt-5.6-luna")
    # Crosses at F ~= 1560 tokens
    assert not _should_fold(1_200, 3_000, cache_warm=False)
    assert _should_fold(2_000, 3_000, cache_warm=False)


def test_should_fold_luna_warm_threshold(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_MODEL", "gpt-5.6-luna")
    # With K=3000 kept tokens the warm break-even is F ~= 15.8k tokens
    assert not _should_fold(15_000, 3_000, cache_warm=True)
    assert _should_fold(17_000, 3_000, cache_warm=True)


def test_should_fold_nothing_to_save():
    assert not _should_fold(100, 3_000, cache_warm=False)


# ---------------------------------------------------------------------------
# compact_history plumbing (Flash Lite prices, cold cache unless stated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_history_untouched(patched_summarise):
    messages = _user_turn(SMALL, system=True) + _user_turn(SMALL) + _current_turn()
    out = await compact_history(list(messages))
    assert out == messages
    assert patched_summarise.await_count == 0


@pytest.mark.asyncio
async def test_long_history_folds_old_turns_when_cold(patched_summarise):
    messages = _long_history(5) + _current_turn()

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
async def test_warm_cache_blocks_fold_on_flash_lite(patched_summarise):
    messages = _long_history(5) + _current_turn()
    _warm()
    out = await compact_history(list(messages))
    assert out == messages
    assert patched_summarise.await_count == 0


@pytest.mark.asyncio
async def test_explicit_cold_gap_folds(patched_summarise):
    messages = _long_history(5) + _current_turn()
    _cold()
    out = await compact_history(list(messages))
    assert out != messages
    assert patched_summarise.await_count == 1


@pytest.mark.asyncio
async def test_second_call_same_run_reads_warm(patched_summarise):
    """The processor stamps its own invocations: a tool-loop's second model
    call is seconds after the first, so Flash Lite must not fold there."""
    messages = _long_history(5) + _current_turn()
    out1 = await compact_history(list(messages))
    assert patched_summarise.await_count == 1  # cold: folded

    grown = _long_history(5) + _current_turn()  # fresh long history again
    out2 = await compact_history(list(grown))
    assert out2 == grown  # immediately after: warm, no fold
    assert patched_summarise.await_count == 1
    assert out1 != messages


@pytest.mark.asyncio
async def test_luna_folds_warm_when_history_huge(patched_summarise, monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_MODEL", "gpt-5.6-luna")
    messages = _long_history(9) + _current_turn()  # ~24k foldable tokens
    _warm()
    out = await compact_history(list(messages))
    assert out != messages
    assert patched_summarise.await_count == 1


@pytest.mark.asyncio
async def test_kept_window_respects_budget(patched_summarise):
    messages = _long_history(6) + _current_turn()

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

    kinds = [type(p).__name__ for m in out for p in m.parts]
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
    out1 = await compact_history(_long_history(5) + _current_turn())

    # Grow the conversation again; next engagement turn arrives cold.
    grown = out1[:-1]
    for _ in range(5):
        grown += _user_turn(BIG)
    _cold()
    out2 = await compact_history(grown + _current_turn())

    assert patched_summarise.await_count == 2
    # Second fold's transcript includes the first summary text.
    transcript = patched_summarise.await_args_list[1].args[0]
    assert STUB_SUMMARY_TEXT in transcript
    assert out2[0].parts[-1].content == STUB_SUMMARY_TEXT


@pytest.mark.asyncio
async def test_summariser_failure_leaves_history_untouched(patched_summarise):
    patched_summarise.return_value = None
    messages = _long_history(5) + _current_turn()
    out = await compact_history(list(messages))
    assert out == messages


@pytest.mark.asyncio
async def test_events_recorded_when_collector_active(patched_summarise):
    messages = _long_history(5) + _current_turn()

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
    await compact_history(_long_history(5) + _current_turn())
    assert drain_collection() == []


@pytest.mark.asyncio
async def test_empty_and_single_message_pass_through(patched_summarise):
    assert await compact_history([]) == []
    single = [ModelRequest(parts=[UserPromptPart(content=BIG * 5)])]
    assert await compact_history(list(single)) == single
    assert patched_summarise.await_count == 0
