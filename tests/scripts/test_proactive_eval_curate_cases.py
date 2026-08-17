"""Tests for the curated softball/edge-case subset builder."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval import curate_cases  # noqa: E402


def _record(message_id: str, minute: int, content: str = "hello") -> dict:
    return {
        "id": message_id,
        "timestamp": f"2026-07-20T10:{minute:02d}:00+00:00",
        "author_id": "1",
        "author_name": "alice",
        "author_display": "alice",
        "is_bot": False,
        "content": content,
        "reply_to_id": None,
        "mention_user_ids": [],
        "mention_everyone": False,
        "attachment_count": 0,
        "sticker_count": 0,
        "reaction_counts": {},
        "message_type": 0,
    }


def _scored(reply_to_id: str | None, *, ok_label: bool | None, judge_ok: bool,
            activation: int = 0, response: int = 0) -> dict:
    label = None
    if ok_label is not None:
        label = {
            "directed_at": "anyone" if ok_label else "other_user",
            "target_user_id": None,
            "ok_to_respond": ok_label,
            "reason": "why",
        }
    return {
        "activation_index": activation,
        "response_index": response,
        "reply_to_id": reply_to_id,
        "content": "bot says hi",
        "window_start": "2026-07-20T10:30:00+00:00",
        "label": label,
        "verdict": {"appropriate": judge_ok, "severity": "fine" if judge_ok else "bad",
                    "reason": "verdict reason"},
    }


def test_select_cases_splits_softballs_from_edge_cases():
    scored = [
        _scored("1", ok_label=True, judge_ok=True, activation=0),
        _scored("2", ok_label=False, judge_ok=True, activation=1),
        _scored(None, ok_label=None, judge_ok=False, activation=2),
    ]
    cases = curate_cases.select_cases(scored)
    assert [c["reply_to_id"] for c in cases["softballs"]] == ["1"]
    assert [c["reply_to_id"] for c in cases["edge_cases"]] == ["2", None]


def test_subset_records_merges_windows_dedup_ascending():
    records = [_record(str(n), n) for n in range(30)]
    cases = {
        "softballs": [_scored("5", ok_label=True, judge_ok=True)],
        "edge_cases": [_scored("8", ok_label=False, judge_ok=False)],
    }
    subset = curate_cases.subset_records(records, cases, radius=2)
    # windows 3..7 and 6..10 merge without duplicates, ascending.
    assert [r["id"] for r in subset] == [str(n) for n in range(3, 11)]


def test_filter_labels_keeps_only_subset_ids():
    labels = {"1": {"directed_at": "anyone"}, "2": {"directed_at": "bot"}}
    assert curate_cases.filter_labels(labels, {"2"}) == {
        "2": {"directed_at": "bot"}
    }


def test_markdown_has_both_sections_with_reasons():
    records = [_record(str(n), n, content=f"msg {n}") for n in range(12)]
    cases = {
        "softballs": [_scored("5", ok_label=True, judge_ok=True)],
        "edge_cases": [_scored("8", ok_label=False, judge_ok=False)],
    }
    rendered = curate_cases.render_cases_markdown(
        cases, records, run_name="run.json"
    )
    assert "## Softballs" in rendered
    assert "## Edge cases" in rendered
    assert "msg 5" in rendered and "msg 8" in rendered
    assert "verdict reason" in rendered
    assert "bot says hi" in rendered


def test_cli_flow_writes_subset_files(tmp_path):
    data_dir = tmp_path / "data"
    runs_dir = data_dir / "runs"
    runs_dir.mkdir(parents=True)
    stem = "G1-💬general-2026-07-20"
    records = [_record(str(n), n) for n in range(20)]
    (data_dir / f"{stem}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records)
    )
    (data_dir / f"{stem}.meta.json").write_text(
        json.dumps({"bot_user_id": "B1", "guild_id": "G1",
                    "channel_name": "💬general", "guild_name": "SD"})
    )
    (data_dir / f"{stem}.labels.json").write_text(json.dumps({
        "labels": {
            "5": {"directed_at": "anyone", "target_user_id": None,
                  "ok_to_respond": True, "reason": "open"},
            "8": {"directed_at": "other_user", "target_user_id": None,
                  "ok_to_respond": False, "reason": "bob's"},
        }
    }))
    run_record = {
        "fixture": f"{stem}.jsonl",
        "adapter": "baseline", "model_id": "m", "cadence_seconds": 300,
        "history_size": 60,
        "activations": [
            {"index": 0, "window_start": "2026-07-20T10:00:00+00:00",
             "window_end": "2026-07-20T10:05:00+00:00", "skipped": False,
             "new_message_count": 1, "history_count": 0,
             "responses": [{"reply_to_id": "5", "content": "sure"},
                            {"reply_to_id": "8", "content": "hi bob"}],
             "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
             "cost_usd": 0.0},
        ],
        "totals": {}, "cost_summary": {},
    }
    run_path = runs_dir / f"{stem}.baseline.m.300s.json"
    run_path.write_text(json.dumps(run_record))
    cache_dir = data_dir / ".score_cache" / run_path.stem
    cache_dir.mkdir(parents=True)
    for n, appropriate in ((0, True), (1, False)):
        payload = {"appropriate": appropriate,
                   "severity": "fine" if appropriate else "bad", "reason": "r"}
        (cache_dir / f"response-0-{n}.json").write_text(json.dumps(
            {"result": "```json\n" + json.dumps(payload) + "\n```",
             "total_cost_usd": 0}
        ))

    written = curate_cases.curate(run_path)

    names = sorted(p.name for p in written)
    assert names == [
        f"{stem}-cases.jsonl", f"{stem}-cases.labels.json",
        f"{stem}-cases.md", f"{stem}-cases.meta.json",
    ]
    cases_dir = data_dir / "cases"
    subset = [json.loads(l) for l in
              (cases_dir / f"{stem}-cases.jsonl").read_text().splitlines()]
    subset_ids = {r["id"] for r in subset}
    assert {"5", "8"} <= subset_ids
    labels = json.loads((cases_dir / f"{stem}-cases.labels.json").read_text())
    assert set(labels["labels"]) <= subset_ids


def test_cli_fails_fast_without_score_cache(tmp_path):
    data_dir = tmp_path / "data"
    runs_dir = data_dir / "runs"
    runs_dir.mkdir(parents=True)
    stem = "G1-💬general-2026-07-20"
    (data_dir / f"{stem}.jsonl").write_text(json.dumps(_record("1", 1)) + "\n")
    (data_dir / f"{stem}.meta.json").write_text(json.dumps({"bot_user_id": "B1"}))
    (data_dir / f"{stem}.labels.json").write_text(json.dumps({"labels": {}}))
    run_path = runs_dir / f"{stem}.baseline.m.300s.json"
    run_path.write_text(json.dumps({
        "fixture": f"{stem}.jsonl", "adapter": "b", "model_id": "m",
        "cadence_seconds": 300, "history_size": 60,
        "activations": [
            {"index": 0, "window_start": "2026-07-20T10:00:00+00:00",
             "window_end": "2026-07-20T10:05:00+00:00", "skipped": False,
             "new_message_count": 1, "history_count": 0,
             "responses": [{"reply_to_id": "1", "content": "x"}],
             "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
             "cost_usd": 0.0}],
        "totals": {}, "cost_summary": {},
    }))
    with pytest.raises(SystemExit, match="score_run"):
        curate_cases.curate(run_path)
