"""Tests for ``chat_compaction.compact_history`` (cost-model driven).

The summariser agent is patched out — these tests verify the *plumbing*
(where the cut lands, tool-pair integrity, system-prompt survival,
self-stabilising merges) and the *economics* (fold on cold cache, hold on
warm, thresholds per model price).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import SystemPromptPart
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart

from smarter_dev.bot.agents.chat_compaction import CACHE_TTL_SECONDS
from smarter_dev.bot.agents.chat_compaction import COMPACTED_PREFIX
from smarter_dev.bot.agents.chat_compaction import DEFAULT_CHAT_MODEL
from smarter_dev.bot.agents.chat_compaction import DEFAULT_COMPACT_MODEL
from smarter_dev.bot.agents.chat_compaction import HARD_FOLD_TOKENS
from smarter_dev.bot.agents.chat_compaction import KEEP_RECENT_CHARS
from smarter_dev.bot.agents.chat_compaction import CompactionEvent
from smarter_dev.bot.agents.chat_compaction import _should_fold
from smarter_dev.bot.agents.chat_compaction import _summarise_conversation
from smarter_dev.bot.agents.chat_compaction import compact_history
from smarter_dev.bot.agents.chat_compaction import drain_collection
from smarter_dev.bot.agents.chat_compaction import set_last_model_call
from smarter_dev.bot.agents.chat_compaction import start_collection

STUB_SUMMARY_TEXT = f"{COMPACTED_PREFIX} alice (id 1) asked about webhooks."


def _stub_result():
    from smarter_dev.bot.agents.chat_compaction import _SummariseResult

    return _SummariseResult(
        text=STUB_SUMMARY_TEXT,
        tokens_input=10,
        tokens_output=5,
        model_name="stub-model",
        cache_read_tokens=4,
        cache_write_tokens=0,
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
    set_last_model_call(datetime.now(UTC) - timedelta(seconds=CACHE_TTL_SECONDS + 60))


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
# The cost model itself (token-level unit tests; default Luna prices unless
# CHAT_AGENT_MODEL says otherwise)
# ---------------------------------------------------------------------------


def test_default_chat_and_compaction_models_are_luna():
    assert DEFAULT_CHAT_MODEL == "gpt-5.6-luna"
    assert DEFAULT_COMPACT_MODEL == "gpt-5.6-luna"


def test_should_fold_luna_default_cold_threshold(monkeypatch):
    monkeypatch.delenv("CHAT_AGENT_MODEL", raising=False)
    # (p + n*c)(F - S) >= Sigma crosses at F ~= 9375 tokens.
    assert not _should_fold(9_000, 3_000, cache_warm=False)
    assert _should_fold(10_000, 3_000, cache_warm=False)


def test_should_fold_luna_default_never_warm_below_cap(monkeypatch):
    monkeypatch.delenv("CHAT_AGENT_MODEL", raising=False)
    # Cached savings (0.02*n) accrue slower than the summariser's own input
    # rate (0.20) — below the latency cap, no F makes a warm fold profitable.
    assert not _should_fold(HARD_FOLD_TOKENS - 1, 0, cache_warm=True)


def test_hard_cap_folds_regardless_of_economics(monkeypatch):
    monkeypatch.delenv("CHAT_AGENT_MODEL", raising=False)
    # At the cap, latency protection overrides the dollar math even on the
    # cheapest model with a warm cache.
    assert _should_fold(HARD_FOLD_TOKENS, 0, cache_warm=True)


def test_explicit_luna_uses_reduced_rate_threshold(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_MODEL", "gpt-5.6-luna")
    assert not _should_fold(9_000, 3_000, cache_warm=False)
    assert _should_fold(10_000, 3_000, cache_warm=False)


def test_should_fold_luna_warm_threshold(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_MODEL", "gpt-5.6-luna")
    # Luna-on-Luna compaction is not economical while warm below the hard cap.
    assert not _should_fold(15_000, 3_000, cache_warm=True)
    assert _should_fold(17_000, 3_000, cache_warm=True)


def test_should_fold_nothing_to_save():
    assert not _should_fold(100, 3_000, cache_warm=False)


# ---------------------------------------------------------------------------
# compact_history plumbing (Luna prices, cold cache unless stated)
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
    call is seconds after the first, so Luna must not fold there."""
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
        if isinstance(p, UserPromptPart | TextPart)
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
    messages = (
        _user_turn(BIG, system=True)
        + _user_turn(BIG)
        + [ModelRequest(parts=[UserPromptPart(content=BIG * 3)])]
    )
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
    assert ev.summarizer_cache_read_tokens == 4
    assert ev.summarizer_cache_write_tokens == 0


@pytest.mark.asyncio
async def test_events_dropped_without_collector(patched_summarise):
    drain_collection()  # ensure no active bucket
    await compact_history(_long_history(5) + _current_turn())
    assert drain_collection() == []


@pytest.mark.asyncio
async def test_summarise_reads_cache_tokens_from_usage():
    """The summariser folds the usage object's cache split onto the result."""
    from types import SimpleNamespace

    usage = SimpleNamespace(
        input_tokens=200,
        output_tokens=20,
        cache_read_tokens=150,
        cache_write_tokens=5,
    )
    run_result = SimpleNamespace(output="a concise summary", usage=lambda: usage)
    stub_agent = SimpleNamespace(run=AsyncMock(return_value=run_result))
    with patch(
        "smarter_dev.bot.agents.chat_compaction.get_summarizer_agent",
        return_value=stub_agent,
    ):
        result = await _summarise_conversation("some transcript")
    assert result is not None
    assert result.tokens_input == 200
    assert result.cache_read_tokens == 150
    assert result.cache_write_tokens == 5


@pytest.mark.asyncio
async def test_summarise_defaults_cache_tokens_when_usage_lacks_them():
    """A usage object without cache attrs yields a zero cache split."""
    from types import SimpleNamespace

    usage = SimpleNamespace(input_tokens=200, output_tokens=20)
    run_result = SimpleNamespace(output="summary", usage=lambda: usage)
    stub_agent = SimpleNamespace(run=AsyncMock(return_value=run_result))
    with patch(
        "smarter_dev.bot.agents.chat_compaction.get_summarizer_agent",
        return_value=stub_agent,
    ):
        result = await _summarise_conversation("some transcript")
    assert result is not None
    assert result.cache_read_tokens == 0
    assert result.cache_write_tokens == 0


@pytest.mark.asyncio
async def test_empty_and_single_message_pass_through(patched_summarise):
    assert await compact_history([]) == []
    single = [ModelRequest(parts=[UserPromptPart(content=BIG * 5)])]
    assert await compact_history(list(single)) == single
    assert patched_summarise.await_count == 0


# ---------------------------------------------------------------------------
# The current turn is never folded, including mid tool-loop
#
# ``_index_of_last_request`` anchors the boundary on the last ModelRequest,
# and Pydantic AI appends a tool-return-only ModelRequest after every round of
# tool calls — so that anchor walks forward while a turn is still running,
# which reads like it should let a long tool loop fold away its own in-flight
# work. It doesn't: ``_pick_cut_index`` only ever cuts at a user-turn start,
# and the running turn's own prompt is one, so everything from it onward stays
# verbatim regardless of where the anchor landed.
#
# These pin that invariant down, because the reasoning above is subtle enough
# to be worth a regression test rather than a re-derivation.
# ---------------------------------------------------------------------------

CURRENT_PROMPT = "count the characters in my draft"


def _in_flight_tool_turn() -> list:
    """The current turn, mid tool-loop — no final response yet."""
    return [
        ModelRequest(parts=[UserPromptPart(content=CURRENT_PROMPT)]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="run_code",
                    args='{"reason":"Quick char count"}',
                    tool_call_id="tc9",
                )
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="run_code", content="4211", tool_call_id="tc9")
            ]
        ),
    ]


def _all_text(messages: list) -> str:
    chunks = []
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, str):
                chunks.append(content)
            args = getattr(part, "args", None)
            if isinstance(args, str):
                chunks.append(args)
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_in_flight_turn_prompt_survives_a_fold(patched_summarise):
    """The running turn's own user prompt must stay verbatim."""
    _cold()
    messages = _long_history(5) + _in_flight_tool_turn()
    result = await compact_history(messages)
    assert patched_summarise.await_count == 1, "expected a fold to happen"
    assert CURRENT_PROMPT in _all_text(result)


@pytest.mark.asyncio
async def test_in_flight_tool_call_and_return_survive_a_fold(patched_summarise):
    """The tool work this turn already did is what the model answers from."""
    _cold()
    messages = _long_history(5) + _in_flight_tool_turn()
    result = await compact_history(messages)
    text = _all_text(result)
    assert "Quick char count" in text
    assert "4211" in text


@pytest.mark.asyncio
async def test_prior_turns_still_fold_around_an_in_flight_turn(patched_summarise):
    """The fix must not disable folding — old turns still collapse."""
    _cold()
    messages = _long_history(5) + _in_flight_tool_turn()
    result = await compact_history(messages)
    assert STUB_SUMMARY_TEXT in _all_text(result)
    assert len(result) < len(messages)


@pytest.mark.asyncio
async def test_deep_tool_loop_does_not_erode_its_own_turn(patched_summarise):
    """Sixteen rounds — the shape of the 2026-07-28 incident."""
    _cold()
    loop: list = [ModelRequest(parts=[UserPromptPart(content=CURRENT_PROMPT)])]
    for i in range(16):
        loop.append(
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="run_code",
                        args=f'{{"reason":"Char count attempt {i + 2}"}}',
                        tool_call_id=f"tc{i}",
                    )
                ]
            )
        )
        loop.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="run_code",
                        content=f"result {i}",
                        tool_call_id=f"tc{i}",
                    )
                ]
            )
        )
    result = await compact_history(_long_history(5) + loop)
    text = _all_text(result)
    assert CURRENT_PROMPT in text
    # Every round of the in-flight turn is still there.
    for i in range(16):
        assert f"Char count attempt {i + 2}" in text


# ---------------------------------------------------------------------------
# Merged turn boundaries (the 2026-07-28 Gemini 400)
#
# Pydantic AI merges consecutive ``ModelRequest``s before a run starts, so the
# ``ToolReturnPart`` closing one turn ("Final result processed.") ends up in
# the SAME request as the next turn's user prompt — and that merged request is
# what gets persisted, so it stays that shape forever. It still reads as a
# user-turn start, so the compactor is free to cut there, which folds the tool
# CALL into the summary while its RETURN survives in the kept window. Pydantic
# AI then merges the summary request with that boundary (tool returns sorted
# first) and the orphan lands at the very head of the history, where Gemini
# rejects it: "Please ensure that function response turn comes immediately
# after a function call turn."
# ---------------------------------------------------------------------------

BOUNDARY_PROMPT = "the message that opened the next turn"


def _closed_turn(text: str, *, call_id: str, system: bool = False) -> list:
    """A completed turn the way Pydantic AI persists a tool-output run."""
    req_parts = ([SystemPromptPart(content="sys")] if system else []) + [
        UserPromptPart(content=text)
    ]
    return [
        ModelRequest(parts=req_parts),
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="final_result", args="{}", tool_call_id=call_id)
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="final_result",
                    content="Final result processed.",
                    tool_call_id=call_id,
                )
            ]
        ),
    ]


def _merged_boundary_history() -> list:
    """Prior turns, then a boundary request carrying a return AND a prompt.

    Sized so the cut lands *on* the boundary: the turn it opens is heavy
    enough to fill the keep-recent window on its own, so the previous turn
    start no longer fits and the boundary is the earliest one that does.
    """
    messages = _closed_turn(BIG, call_id="o0", system=True)
    for i in range(1, 6):
        messages += _closed_turn(BIG, call_id=f"o{i}")
    # What `_clean_message_history` leaves behind: the trailing tool return of
    # the last closed turn merged into the next turn's first user prompt.
    boundary = messages.pop()
    messages.append(
        ModelRequest(parts=[*boundary.parts, UserPromptPart(content=BOUNDARY_PROMPT)])
    )
    # The turn that boundary opened: a heavy research round, then its answer.
    messages += [
        ModelResponse(
            parts=[ToolCallPart(tool_name="web_search", args="{}", tool_call_id="s9")]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="web_search", content="x" * 16_000, tool_call_id="s9"
                )
            ]
        ),
        ModelResponse(
            parts=[ToolCallPart(tool_name="final_result", args="{}", tool_call_id="o9")]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="final_result",
                    content="Final result processed.",
                    tool_call_id="o9",
                )
            ]
        ),
    ]
    return messages + _current_turn()


def _first_return_precedes_no_call(messages: list) -> bool:
    """True when a tool return appears before any tool call — the 400 shape."""
    seen_call = False
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                seen_call = True
            elif isinstance(part, ToolReturnPart) and not seen_call:
                return True
    return False


@pytest.mark.asyncio
async def test_merged_boundary_fold_leaves_no_orphan_tool_return(patched_summarise):
    """Folding at a merged boundary must not strand its tool return."""
    _cold()
    result = await compact_history(_merged_boundary_history())
    assert patched_summarise.await_count == 1, "expected a fold to happen"
    assert not _first_return_precedes_no_call(result)


@pytest.mark.asyncio
async def test_merged_boundary_prompt_survives_the_fold(patched_summarise):
    """Splitting the boundary keeps the user's own message verbatim."""
    _cold()
    result = await compact_history(_merged_boundary_history())
    assert BOUNDARY_PROMPT in _all_text(result)


@pytest.mark.asyncio
async def test_merged_boundary_return_reaches_the_summariser(patched_summarise):
    """The folded-away return is summarised, not silently dropped."""
    _cold()
    await compact_history(_merged_boundary_history())
    transcript = patched_summarise.await_args.args[0]
    assert "Final result processed." in transcript


@pytest.mark.asyncio
async def test_gemini_never_sees_a_leading_function_response(patched_summarise):
    """The full wire path: Pydantic AI re-merges the compacted history.

    This is the actual regression — the orphan only reaches the head once
    ``_clean_message_history`` folds the summary request into the boundary and
    sorts tool returns to the front.
    """
    from pydantic_ai._agent_graph import _clean_message_history

    _cold()
    result = await compact_history(_merged_boundary_history())
    wire = _clean_message_history(result)
    head = wire[0]
    assert not any(isinstance(part, ToolReturnPart) for part in head.parts)
    assert not _first_return_precedes_no_call(wire)


@pytest.mark.asyncio
async def test_leading_orphan_return_is_stripped(patched_summarise):
    """Backstop: a history that arrives already orphaned is repaired."""
    messages = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="sys"),
                ToolReturnPart(
                    tool_name="final_result",
                    content="Final result processed.",
                    tool_call_id="stale",
                ),
                UserPromptPart(content="hello"),
            ]
        ),
        ModelResponse(parts=[TextPart(content="hi")]),
        ModelRequest(parts=[UserPromptPart(content="newest message")]),
    ]
    result = await compact_history(messages)
    assert not _first_return_precedes_no_call(result)
    # The rest of the head survives untouched.
    assert "hello" in _all_text(result)
    assert any(
        isinstance(part, SystemPromptPart)
        for message in result
        for part in message.parts
    )
