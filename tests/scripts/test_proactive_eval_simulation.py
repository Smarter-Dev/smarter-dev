"""Tests for the periodic-activation simulator (stage 3). No network anywhere."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from scripts.proactive_eval import adapters, simulation  # noqa: E402


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 20, hour, minute, second, tzinfo=UTC)


def _message(
    message_id: str,
    timestamp: datetime,
    *,
    author_id: str = "1",
    display: str = "alice",
    bot: bool = False,
    content: str = "hello",
    reply_to_id: str | None = None,
) -> simulation.FixtureMessage:
    return simulation.FixtureMessage(
        id=message_id,
        timestamp=timestamp,
        author_id=author_id,
        author_name=display,
        author_display=display,
        is_bot=bot,
        content=content,
        reply_to_id=reply_to_id,
        mention_user_ids=(),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=0,
    )


class ScriptedAdapter:
    """Returns a planned ActivationResult per call and records every context."""

    def __init__(self, planned: dict[int, simulation.ActivationResult]):
        self.planned = planned
        self.contexts: list[simulation.ActivationContext] = []

    async def activate(
        self, context: simulation.ActivationContext
    ) -> simulation.ActivationResult:
        call_index = len(self.contexts)
        self.contexts.append(context)
        return self.planned.get(
            call_index,
            simulation.ActivationResult(
                responses=[],
                input_tokens=100,
                output_tokens=10,
                cache_read_tokens=0,
                model_id="stub-model",
            ),
        )


def _respond(content: str, reply_to_id: str | None = None) -> simulation.ActivationResult:
    return simulation.ActivationResult(
        responses=[simulation.ProposedResponse(reply_to_id=reply_to_id, content=content)],
        input_tokens=200,
        output_tokens=20,
        cache_read_tokens=5,
        model_id="stub-model",
    )


async def _run(messages, adapter, **overrides):
    settings = {
        "channel_name": "💬general",
        "guild_name": "Smarter Dev",
        "bot_user_id": "B1",
        "adapter_name": "scripted",
        "model_id": "stub-model",
        "cadence_seconds": 300,
        "history_size": 60,
        "activation_cost": lambda result: 0.0,
    }
    settings.update(overrides)
    return await simulation.run_simulation(
        messages=messages, adapter=adapter, **settings
    )


# --- windowing ---------------------------------------------------------------


def test_floor_to_cadence_hits_the_boundary():
    assert simulation.floor_to_cadence(_ts(0, 7, 9), 300) == _ts(0, 5, 0)
    assert simulation.floor_to_cadence(_ts(0, 5, 0), 300) == _ts(0, 5, 0)


def test_activation_windows_step_by_cadence_and_cover_the_span():
    windows = simulation.activation_windows(_ts(0, 7, 9), _ts(0, 21, 0), 300)
    assert windows[0] == (_ts(0, 5), _ts(0, 10))
    assert windows[-1] == (_ts(0, 20), _ts(0, 25))
    assert len(windows) == 4
    for start, end in windows:
        assert start.timestamp() % 300 == 0
        assert end - start == timedelta(seconds=300)


async def test_messages_land_in_the_right_window_and_empty_windows_skip():
    messages = [
        _message("1", _ts(0, 6)),
        _message("2", _ts(0, 8)),
        _message("3", _ts(0, 21)),  # two empty windows in between
    ]
    adapter = ScriptedAdapter({})
    record = await _run(messages, adapter)

    activations = record["activations"]
    assert [a["skipped"] for a in activations] == [False, True, True, False]
    assert [a["new_message_count"] for a in activations] == [2, 0, 0, 1]
    assert activations[0]["window_start"] == _ts(0, 5).isoformat()
    assert activations[0]["window_end"] == _ts(0, 10).isoformat()
    # Adapter only ran for the two non-empty windows.
    assert len(adapter.contexts) == 2
    assert [m.id for m in adapter.contexts[0].new_messages] == ["1", "2"]
    assert [m.id for m in adapter.contexts[1].new_messages] == ["3"]


# --- history interleaving and sequential dependency --------------------------


async def test_injected_responses_appear_in_later_histories_in_order():
    messages = [
        _message("1", _ts(0, 6)),
        _message("2", _ts(0, 12)),
        _message("3", _ts(0, 17)),
    ]
    adapter = ScriptedAdapter({0: _respond("I can help", reply_to_id="1")})
    record = await _run(messages, adapter)

    second_history = adapter.contexts[1].history
    assert [m.id for m in second_history[:1]] == ["1"]
    injected = second_history[1]
    assert injected.injected_bot_response is True
    assert injected.is_bot is True
    assert injected.author_id == "B1"
    assert injected.content == "I can help"
    assert injected.reply_to_id == "1"
    # Stamped at its activation time = end of window 0.
    assert injected.timestamp == _ts(0, 10)
    # Still present one window later, interleaved in timestamp order.
    third_history = [m.id for m in adapter.contexts[2].history]
    assert third_history == ["1", injected.id, "2"]

    assert record["totals"]["responses"] == 1
    assert record["totals"]["activations_with_responses"] == 1


async def test_history_is_capped_at_history_size():
    messages = [_message(str(n), _ts(0, 6, n)) for n in range(30)] + [
        _message("late", _ts(0, 12))
    ]
    adapter = ScriptedAdapter({})
    await _run(messages, adapter, history_size=10)
    assert len(adapter.contexts[1].history) == 10
    assert [m.id for m in adapter.contexts[1].history][-1] == "29"


# --- cost math and totals ----------------------------------------------------


async def test_cost_fn_is_applied_per_activation_and_totals_aggregate():
    messages = [_message("1", _ts(0, 6)), _message("2", _ts(0, 12))]
    adapter = ScriptedAdapter({0: _respond("hi")})
    record = await _run(
        messages,
        adapter,
        activation_cost=lambda result: result.output_tokens * 0.001,
    )
    non_empty = [a for a in record["activations"] if not a["skipped"]]
    assert non_empty[0]["cost_usd"] == pytest.approx(0.02)  # 20 output tokens
    assert non_empty[1]["cost_usd"] == pytest.approx(0.01)  # 10 output tokens
    totals = record["totals"]
    assert totals["input_tokens"] == 300
    assert totals["output_tokens"] == 30
    assert totals["cache_read_tokens"] == 5
    assert totals["cost_usd"] == pytest.approx(0.03)
    assert totals["activations"] == 2
    assert totals["activations_with_messages"] == 2


def test_real_price_math_for_flash_lite_list_price():
    # Gemini 3.5 Flash Lite list price: $0.30/M in, $2.50/M out.
    import eval_prices

    eval_prices.install()
    from scripts.proactive_eval import simulate

    cost = simulate.model_cost_calculator("gemini-3.5-flash-lite")(
        simulation.ActivationResult(
            responses=[],
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=0,
            model_id="gemini-3.5-flash-lite",
        )
    )
    assert Decimal(str(cost)) == Decimal("2.8")


def test_zero_token_result_costs_nothing_without_price_lookup():
    from scripts.proactive_eval import simulate

    cost = simulate.model_cost_calculator("no-such-model")(
        simulation.ActivationResult(
            responses=[], input_tokens=0, output_tokens=0,
            cache_read_tokens=0, model_id="none",
        )
    )
    assert cost == 0.0


# --- run record shape --------------------------------------------------------


async def test_run_record_round_trips_and_matches_schema():
    messages = [_message("1", _ts(0, 6))]
    adapter = ScriptedAdapter({0: _respond("hi", reply_to_id="1")})
    record = await _run(messages, adapter)

    round_tripped = json.loads(json.dumps(record))
    assert round_tripped == record
    assert set(record) == {
        "fixture", "adapter", "model_id", "cadence_seconds", "history_size",
        "started_at", "finished_at", "activations", "totals", "cost_summary",
    }
    active = record["activations"][0]
    assert set(active) == {
        "index", "window_start", "window_end", "skipped", "new_message_count",
        "history_count", "responses", "input_tokens", "output_tokens",
        "cache_read_tokens", "cost_usd",
    }
    assert active["responses"] == [{"reply_to_id": "1", "content": "hi"}]
    assert set(record["totals"]) == {
        "activations", "activations_with_messages", "activations_with_responses",
        "responses", "input_tokens", "output_tokens", "cache_read_tokens",
        "cost_usd",
    }


# --- cost summary ------------------------------------------------------------


async def test_cost_summary_stats_and_cadence_sensitivity():
    messages = [
        _message("1", _ts(0, 6)),
        _message("2", _ts(0, 8)),
        _message("3", _ts(0, 12)),
    ]
    adapter = ScriptedAdapter({})
    record = await _run(messages, adapter)
    summary = record["cost_summary"]

    stats = summary["non_empty_activation_input_tokens"]
    assert stats["mean"] == 100 and stats["median"] == 100 and stats["p95"] == 100
    assert summary["projected_cost_30_days_usd"] == pytest.approx(
        record["totals"]["cost_usd"] * 30
    )
    cadences = {row["cadence_seconds"]: row for row in summary["cadence_sensitivity"]}
    assert set(cadences) == {120, 300, 900}
    # At 300s the three messages occupy two windows; at 900s one; at 120s three.
    assert cadences[300]["windows_with_messages"] == 2
    assert cadences[900]["windows_with_messages"] == 1
    assert cadences[120]["windows_with_messages"] == 3
    assert "measured only at" in summary["note"]


# --- SilentAdapter end-to-end ------------------------------------------------


async def test_silent_adapter_costs_nothing_and_counts_activations():
    messages = [_message("1", _ts(0, 6)), _message("2", _ts(0, 21))]
    record = await _run(
        messages,
        adapters.SilentAdapter(),
        adapter_name="silent",
        model_id="none",
    )
    totals = record["totals"]
    assert totals["cost_usd"] == 0.0
    assert totals["input_tokens"] == 0
    assert totals["responses"] == 0
    assert totals["activations"] == 4
    assert totals["activations_with_messages"] == 2
    assert totals["activations_with_responses"] == 0


# --- baseline adapter prompt rendering ---------------------------------------


def _context(history, new_messages) -> simulation.ActivationContext:
    return simulation.ActivationContext(
        channel_name="💬general",
        guild_name="Smarter Dev",
        bot_user_id="B1",
        activated_at=_ts(0, 10),
        history=history,
        new_messages=new_messages,
    )


def test_baseline_prompt_renders_history_and_new_blocks():
    history = [_message("1", _ts(0, 1), content="earlier chatter")]
    new = [
        _message("2", _ts(0, 6), content="does anyone know uv?"),
        _message(
            "3", _ts(0, 7), bot=True, display="smarter-bot",
            author_id="B1", content="I do",
        ),
    ]
    prompt = adapters.render_activation_prompt(_context(history, new))

    history_block, new_block = prompt.split("NEW MESSAGES")
    assert "HISTORY" in history_block
    assert "[id=1]" in history_block and "earlier chatter" in history_block
    assert "[id=2]" in new_block and "does anyone know uv?" in new_block
    assert "[BOT]" in new_block  # bot line marked


def test_baseline_prompt_handles_empty_history():
    prompt = adapters.render_activation_prompt(
        _context([], [_message("2", _ts(0, 6))])
    )
    assert "(none)" in prompt


def test_baseline_system_prompt_states_the_rules():
    system_prompt = adapters.build_baseline_system_prompt(
        bot_display_name="smarter-bot",
        guild_name="Smarter Dev",
        channel_name="💬general",
        cadence_seconds=300,
    )
    assert "smarter-bot" in system_prompt
    assert "💬general" in system_prompt
    assert "5 minutes" in system_prompt
    assert "never" in system_prompt.lower()  # never insert into exchanges
    assert "2" in system_prompt  # at most 2 responses per wake


def test_baseline_output_clamps_to_two_responses():
    output = adapters.BaselineOutput(
        responses=[
            adapters.BaselineResponse(reply_to_id=None, content=f"r{n}")
            for n in range(5)
        ]
    )
    proposed = adapters.proposed_responses(output)
    assert len(proposed) == 2
    assert all(isinstance(p, simulation.ProposedResponse) for p in proposed)


# --- fixture message conversion ----------------------------------------------


def test_fixture_message_round_trips_record_dict():
    record = {
        "id": "42",
        "timestamp": "2026-07-20T15:10:20.361000+00:00",
        "author_id": "5",
        "author_name": "bob",
        "author_display": "bobby",
        "is_bot": False,
        "content": "hi",
        "reply_to_id": None,
        "mention_user_ids": ["9"],
        "mention_everyone": False,
        "attachment_count": 1,
        "sticker_count": 0,
        "reaction_counts": {"👍": 2},
        "message_type": 0,
    }
    message = simulation.FixtureMessage.from_record(record)
    assert message.timestamp == datetime(2026, 7, 20, 15, 10, 20, 361000, tzinfo=UTC)
    assert message.mention_user_ids == ("9",)
    rendered = message.to_record()
    assert rendered["id"] == "42"
    assert rendered["author_display"] == "bobby"
    assert rendered["is_bot"] is False
    assert rendered["reply_to_id"] is None
