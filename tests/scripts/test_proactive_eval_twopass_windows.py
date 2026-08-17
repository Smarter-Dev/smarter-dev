"""Tests for two-pass burst windowing and the simulator extensions it needs."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))  # flat eval_prices import

from scripts.proactive_eval import simulation  # noqa: E402
from scripts.proactive_eval.twopass import windows  # noqa: E402

T = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)


def _s(seconds: int) -> datetime:
    return T + timedelta(seconds=seconds)


# --- burst windows -----------------------------------------------------------


def test_single_message_fires_after_quiet_period():
    assert windows.burst_windows([T]) == [(T, _s(15))]


def test_burst_extends_while_messages_keep_coming():
    spans = windows.burst_windows([T, _s(10), _s(20)])
    assert spans == [(T, _s(35))]


def test_burst_caps_at_max_wait_after_first_message():
    timestamps = [T, _s(10), _s(20), _s(30), _s(40), _s(50), _s(70)]
    spans = windows.burst_windows(timestamps)
    assert spans == [(T, _s(60)), (_s(70), _s(85))]


def test_quiet_gap_splits_bursts():
    assert windows.burst_windows([T, _s(100)]) == [
        (T, _s(15)),
        (_s(100), _s(115)),
    ]


def test_message_exactly_at_fire_time_starts_new_burst():
    assert windows.burst_windows([T, _s(15)]) == [
        (T, _s(15)),
        (_s(15), _s(30)),
    ]


def test_passive_ticks_fill_long_gaps_and_stay_empty():
    spans = windows.two_pass_windows([T, _s(2000)])
    assert spans == [
        (T, _s(15)),
        (_s(15), _s(915)),      # passive tick, guaranteed no messages
        (_s(915), _s(1815)),    # passive tick
        (_s(2000), _s(2015)),
    ]


# --- simulator accepts precomputed windows and records extensions ------------


def _message(message_id: str, timestamp: datetime) -> simulation.FixtureMessage:
    return simulation.FixtureMessage(
        id=message_id,
        timestamp=timestamp,
        author_id="1",
        author_name="alice",
        author_display="alice",
        is_bot=False,
        content="hello",
        reply_to_id=None,
        mention_user_ids=(),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=0,
    )


class RichResultAdapter:
    """Returns reactions, per-model usage and details on every activation."""

    async def activate(self, context):
        return simulation.ActivationResult(
            responses=[],
            input_tokens=30,
            output_tokens=3,
            cache_read_tokens=0,
            model_id="twopass",
            reactions=(simulation.ProposedReaction(message_id="1", emoji="👍"),),
            usage_by_model={
                "watcher-model": {"input_tokens": 20, "output_tokens": 2,
                                   "cache_read_tokens": 0},
                "agent-model": {"input_tokens": 10, "output_tokens": 1,
                                 "cache_read_tokens": 0},
            },
            details={"watcher": {"wake": True, "reason": "interesting"}},
        )


async def test_run_simulation_uses_precomputed_windows_and_records_extras():
    messages = [_message("1", T), _message("2", _s(10))]
    record = await simulation.run_simulation(
        messages=messages,
        channel_name="c",
        guild_name="g",
        bot_user_id="B1",
        adapter=RichResultAdapter(),
        adapter_name="twopass",
        model_id="twopass",
        cadence_seconds=0,
        history_size=60,
        activation_cost=lambda result: 0.0,
        windows=windows.two_pass_windows([m.timestamp for m in messages]),
    )
    assert len(record["activations"]) == 1
    activation = record["activations"][0]
    assert activation["window_end"] == _s(25).isoformat()
    assert activation["reactions"] == [{"message_id": "1", "emoji": "👍"}]
    assert activation["usage_by_model"]["watcher-model"]["input_tokens"] == 20
    assert activation["details"]["watcher"]["wake"] is True


async def test_plain_adapters_keep_the_stage3_record_schema():
    class PlainAdapter:
        async def activate(self, context):
            return simulation.ActivationResult(
                responses=[], input_tokens=1, output_tokens=1,
                cache_read_tokens=0, model_id="m",
            )

    record = await simulation.run_simulation(
        messages=[_message("1", T)],
        channel_name="c", guild_name="g", bot_user_id="B1",
        adapter=PlainAdapter(), adapter_name="plain", model_id="m",
        cadence_seconds=300, history_size=60,
        activation_cost=lambda result: 0.0,
    )
    activation = record["activations"][0]
    assert "reactions" not in activation
    assert "usage_by_model" not in activation
    assert "details" not in activation


# --- multi-model cost --------------------------------------------------------


def test_cost_calculator_prices_usage_by_model_entries():
    import eval_prices

    eval_prices.install()
    from scripts.proactive_eval import simulate

    result = simulation.ActivationResult(
        responses=[], input_tokens=2_000_000, output_tokens=2_000_000,
        cache_read_tokens=0, model_id="twopass",
        usage_by_model={
            "gemini-3.5-flash-lite": {"input_tokens": 1_000_000,
                                       "output_tokens": 1_000_000,
                                       "cache_read_tokens": 0},
            "idle-model": {"input_tokens": 0, "output_tokens": 0,
                            "cache_read_tokens": 0},
        },
    )
    cost = simulate.model_cost_calculator("twopass")(result)
    assert cost == pytest.approx(2.8)  # only the flash-lite entry is priced


def test_provider_fallback_treats_slug_ids_as_openrouter():
    from scripts.proactive_eval import simulate

    assert simulate._provider_id_for("moonshotai/kimi-k3") == "openrouter"


def test_twopass_models_are_priceable_at_list_price():
    import eval_prices

    eval_prices.install()
    from scripts.proactive_eval import simulate

    kimi = simulate._usage_cost("moonshotai/kimi-k3", 1_000_000, 1_000_000, 0)
    assert kimi == pytest.approx(18.0)  # $3 in + $15 out
    deepseek = simulate._usage_cost(
        "deepseek/deepseek-v4-flash", 1_000_000, 1_000_000, 0
    )
    assert deepseek == pytest.approx(0.26)  # $0.0867 + $0.1733


def test_kimi_routes_via_openrouter_without_zen_key(monkeypatch):
    from scripts.proactive_eval.twopass import models

    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "test-key")
    assert models.resolve_agent_model_id("kimi-k3") == "moonshotai/kimi-k3"

    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "zen-key")
    assert models.resolve_agent_model_id("kimi-k3") == "kimi-k3"


def test_kimi_fails_fast_with_no_provider_keys(monkeypatch):
    from scripts.proactive_eval.twopass import models

    for name in ("OPENCODE_ZEN_API_KEY", "OPENROUTER_API_KEY",
                 "OPEN_ROUTER", "OPEN_ROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="OPENCODE_ZEN_API_KEY"):
        models.resolve_agent_model_id("kimi-k3")
