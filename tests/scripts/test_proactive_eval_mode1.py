"""Tests for mode-1 (bot-engaged, multi-turn) scenario fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval import mode1  # noqa: E402
from scripts.proactive_eval.simulation import (  # noqa: E402
    ActivationResult,
    ProposedResponse,
)

SCENARIO_YAML = """\
name: test-scenario
description: alice asks the bot a question and follows up
participants:
  - {key: alice, id: "901", display: alice}
  - {key: bob, id: "902", display: bob}
turns:
  - key: ask
    offset: 0
    author: alice
    content: "hey <@BOT>, what is a coroutine?"
    mentions_bot: true
    directed_at: bot
    reason: direct mention question
  - key: follow
    offset: 60
    author: alice
    reply_to: bot_last
    requires_bot_reply: true
    content: "so it's like a pausable function?"
    directed_at: bot
    reason: follow-up to the bot's answer
  - key: aside
    offset: 65
    author: bob
    reply_to: ask
    content: "alice you should read the asyncio docs"
    directed_at: other_user
    target: alice
    reason: bob answering alice directly
"""


def _scenario(text: str = SCENARIO_YAML, tmp_path: Path | None = None) -> mode1.Scenario:
    return mode1.parse_scenario(text)


class RespondingAdapter:
    """Replies once to the first activation's first new message, then silent."""

    def __init__(self):
        self.activations = 0

    async def activate(self, context) -> ActivationResult:
        self.activations += 1
        responses = []
        if self.activations == 1:
            responses = [
                ProposedResponse(
                    reply_to_id=context.new_messages[0].id,
                    content="A coroutine is a function you can suspend and resume.",
                )
            ]
        return ActivationResult(
            responses=responses,
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=0,
            model_id="stub",
        )


class SilentAdapter:
    async def activate(self, context) -> ActivationResult:
        return ActivationResult(
            responses=[], input_tokens=0, output_tokens=0,
            cache_read_tokens=0, model_id="stub",
        )


# --- parsing and validation --------------------------------------------------


def test_parse_scenario_reads_turns_and_participants():
    scenario = _scenario()
    assert scenario.name == "test-scenario"
    assert set(scenario.participants) == {"alice", "bob"}
    assert len(scenario.turns) == 3
    assert scenario.turns[0].mentions_bot is True
    assert scenario.turns[2].reply_to == "ask"


def test_parse_scenario_rejects_unknown_author():
    with pytest.raises(ValueError, match="author"):
        _scenario(SCENARIO_YAML.replace("author: bob", "author: carol"))


def test_parse_scenario_rejects_bad_directed_at():
    with pytest.raises(ValueError, match="directed_at"):
        _scenario(SCENARIO_YAML.replace(
            "directed_at: other_user", "directed_at: everyone"
        ))


def test_parse_scenario_rejects_unknown_reply_target():
    with pytest.raises(ValueError, match="reply_to"):
        _scenario(SCENARIO_YAML.replace("reply_to: ask", "reply_to: nope"))


# --- materialization ---------------------------------------------------------


async def test_bot_last_binds_to_injected_message_and_mention_is_replaced():
    adapter = RespondingAdapter()
    result = await mode1.run_mode1(_scenario(), adapter, adapter_name="stub",
                                    model_id="stub",
                                    activation_cost=lambda r: 0.0)
    records = result.fixture_records
    ask = next(r for r in records if r["content"].startswith("hey <@"))
    assert f"<@{mode1.SCENARIO_BOT_USER_ID}>" in ask["content"]
    assert ask["mention_user_ids"] == [mode1.SCENARIO_BOT_USER_ID]

    injected = next(r for r in records if r["is_bot"])
    follow = next(r for r in records if "pausable" in r["content"])
    assert follow["reply_to_id"] == injected["id"]
    # bob's aside replies to alice's original turn by key.
    aside = next(r for r in records if "asyncio docs" in r["content"])
    assert aside["reply_to_id"] == ask["id"]


async def test_requires_bot_reply_skips_turn_when_bot_stayed_silent():
    result = await mode1.run_mode1(_scenario(), SilentAdapter(),
                                    adapter_name="stub", model_id="stub",
                                    activation_cost=lambda r: 0.0)
    contents = [r["content"] for r in result.fixture_records]
    assert not any("pausable" in c for c in contents)  # follow-up skipped
    assert result.skipped_turn_keys == ["follow"]
    assert "follow" not in {
        turn_key for turn_key in result.labels_doc["labels"]
    }


async def test_burst_grouping_by_scripted_gaps():
    close_turns = """\
name: close-turns
description: three turns inside one quiet window
participants:
  - {key: alice, id: "901", display: alice}
turns:
  - {offset: 0, author: alice, content: "one", directed_at: anyone, reason: a}
  - {offset: 5, author: alice, content: "two", directed_at: anyone, reason: b}
  - {offset: 8, author: alice, content: "three", directed_at: anyone, reason: c}
"""
    adapter = RespondingAdapter()
    result = await mode1.run_mode1(_scenario(close_turns), adapter,
                                    adapter_name="stub", model_id="stub",
                                    activation_cost=lambda r: 0.0)
    # All three turns land within one 15s-quiet burst → one activation.
    assert adapter.activations == 1
    assert result.run_record["totals"]["activations"] == 1


async def test_labels_derive_ok_to_respond_and_targets():
    result = await mode1.run_mode1(_scenario(), RespondingAdapter(),
                                    adapter_name="stub", model_id="stub",
                                    activation_cost=lambda r: 0.0)
    labels = result.labels_doc["labels"]
    by_reason = {l["reason"]: l for l in labels.values()}
    ask = by_reason["direct mention question"]
    assert ask["directed_at"] == "bot" and ask["ok_to_respond"] is True
    aside = by_reason["bob answering alice directly"]
    assert aside["ok_to_respond"] is False
    assert aside["target_user_id"] == "901"  # alice's participant id


async def test_run_record_matches_simulator_schema():
    result = await mode1.run_mode1(_scenario(), RespondingAdapter(),
                                    adapter_name="stub", model_id="stub",
                                    activation_cost=lambda r: 0.001)
    record = result.run_record
    assert set(record) == {
        "fixture", "adapter", "model_id", "cadence_seconds", "history_size",
        "started_at", "finished_at", "activations", "totals", "cost_summary",
    }
    active = next(a for a in record["activations"] if not a["skipped"])
    assert {"index", "window_start", "window_end", "responses",
            "input_tokens", "cost_usd"} <= set(active)
    assert record["totals"]["responses"] == 1


# --- artifact writing + scoring integration ----------------------------------


async def test_write_artifacts_and_score_with_stub_judge(tmp_path):
    import json

    from scripts.proactive_eval import score_run
    from scripts.proactive_eval.judge import judge_reply_from_raw

    result = await mode1.run_mode1(_scenario(), RespondingAdapter(),
                                    adapter_name="stub", model_id="stub",
                                    activation_cost=lambda r: 0.0)
    data_dir = tmp_path / "data"
    run_path = mode1.write_artifacts(result, data_dir=data_dir)
    assert run_path.parent == data_dir / "runs"
    fixture_path = data_dir / "mode1" / "test-scenario.jsonl"
    assert fixture_path.exists()
    assert (data_dir / "mode1" / "test-scenario.labels.json").exists()
    assert (data_dir / "mode1" / "test-scenario.meta.json").exists()

    class StubJudge:
        def __call__(self, prompt, model):
            payload = {"appropriate": True, "severity": "fine", "reason": "ok"}
            return judge_reply_from_raw({
                "result": "```json\n" + json.dumps(payload) + "\n```",
                "total_cost_usd": 0,
            })

    _, report_path, metrics = score_run.score_run(
        run_path, judge_callable=StubJudge(),
        judge_model="claude-sonnet-5", force=False,
    )
    assert metrics["responses_total"] == 1
    assert metrics["label_violations"] == []
    assert report_path.exists()
