"""Tests for stage 4: judge scoring of a run and the final report.

The judge is stubbed everywhere — no subprocess, no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval import judge, label_day, score_run, scoring  # noqa: E402


def _record(message_id: str, *, minute: int, author_id: str = "1",
            display: str = "alice", bot: bool = False, content: str = "hello") -> dict:
    return {
        "id": message_id,
        "timestamp": f"2026-07-20T10:{minute:02d}:00+00:00",
        "author_id": author_id,
        "author_name": display,
        "author_display": display,
        "is_bot": bot,
        "content": content,
        "reply_to_id": None,
        "mention_user_ids": [],
        "mention_everyone": False,
        "attachment_count": 0,
        "sticker_count": 0,
        "reaction_counts": {},
        "message_type": 0,
    }


def _label(directed_at: str, reason: str = "because") -> dict:
    return {
        "directed_at": directed_at,
        "target_user_id": None,
        "ok_to_respond": directed_at in ("anyone", "bot"),
        "reason": reason,
    }


def _verdict(appropriate: bool, severity: str = "fine", reason: str = "ok") -> dict:
    return {"appropriate": appropriate, "severity": severity, "reason": reason}


# --- run_judge import move ---------------------------------------------------


def test_label_day_reexports_judge_plumbing():
    assert label_day.run_judge is judge.run_judge
    assert label_day.judge_reply_from_raw is judge.judge_reply_from_raw
    assert label_day.JudgeReply is judge.JudgeReply
    assert label_day.JUDGE_PARSE_RETRIES == judge.JUDGE_PARSE_RETRIES


# --- verdict combination -----------------------------------------------------


@pytest.mark.parametrize(
    ("label", "judge_ok", "expected"),
    [
        (_label("anyone"), True, True),        # both pass
        (_label("other_user"), True, False),   # label fails
        (_label("anyone"), False, False),      # judge fails
        (_label("ambient"), False, False),     # both fail
        (None, True, True),                    # standalone: judge-only
        (None, False, False),
    ],
)
def test_response_is_appropriate_combines_both_checks(label, judge_ok, expected):
    assert scoring.response_is_appropriate(label, _verdict(judge_ok)) is expected


# --- judge output parsing ----------------------------------------------------


def _fenced(payload: dict) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"


def test_parse_judge_verdict_accepts_valid_payload():
    verdict = scoring.parse_judge_verdict(
        _fenced({"appropriate": False, "severity": "bad", "reason": "barged in"})
    )
    assert verdict == {"appropriate": False, "severity": "bad", "reason": "barged in"}


def test_parse_judge_verdict_rejects_missing_key():
    with pytest.raises(ValueError, match="severity"):
        scoring.parse_judge_verdict(_fenced({"appropriate": True, "reason": "x"}))


def test_parse_judge_verdict_rejects_unknown_severity():
    with pytest.raises(ValueError, match="severity"):
        scoring.parse_judge_verdict(
            _fenced({"appropriate": True, "severity": "awful", "reason": "x"})
        )


def test_parse_judge_verdict_rejects_non_bool_appropriate():
    with pytest.raises(ValueError, match="appropriate"):
        scoring.parse_judge_verdict(
            _fenced({"appropriate": "yes", "severity": "fine", "reason": "x"})
        )


def test_parse_judge_verdict_requires_fenced_json():
    with pytest.raises(ValueError, match="fenced"):
        scoring.parse_judge_verdict("looks fine to me")


# --- excerpt and prompt rendering --------------------------------------------


def test_excerpt_centers_on_trigger_and_respects_day_edges():
    records = [_record(str(n), minute=n) for n in range(40)]

    middle = scoring.excerpt_for_response(
        records, {"reply_to_id": "20", "window_start": None}, before=15, after=15
    )
    assert [r["id"] for r in middle][0] == "5"
    assert [r["id"] for r in middle][-1] == "35"

    at_start = scoring.excerpt_for_response(
        records, {"reply_to_id": "2", "window_start": None}, before=15, after=15
    )
    assert [r["id"] for r in at_start][0] == "0"  # clamped, no error
    assert [r["id"] for r in at_start][-1] == "17"


def test_excerpt_for_standalone_uses_messages_before_the_window():
    records = [_record(str(n), minute=n) for n in range(40)]
    excerpt = scoring.excerpt_for_response(
        records,
        {"reply_to_id": None, "window_start": "2026-07-20T10:10:00+00:00"},
        before=15,
        after=15,
    )
    assert [r["id"] for r in excerpt][-1] == "9"  # strictly before the window
    assert len(excerpt) == 10


def test_judge_prompt_contains_rule_label_and_response():
    records = [_record(str(n), minute=n) for n in range(10)]
    prompt = scoring.render_judge_prompt(
        response={"reply_to_id": "5", "content": "let me help!",
                  "window_start": "2026-07-20T10:08:00+00:00"},
        label=_label("other_user", reason="reply into bob's exchange"),
        excerpt=records,
        bot_user_id="B1",
    )
    assert "never" in prompt.lower()  # rule text
    assert "[id=5]" in prompt
    assert "let me help!" in prompt
    assert "other_user" in prompt
    assert "reply into bob's exchange" in prompt
    assert '"appropriate"' in prompt


def test_judge_prompt_states_standalone_when_no_trigger():
    records = [_record(str(n), minute=n) for n in range(3)]
    prompt = scoring.render_judge_prompt(
        response={"reply_to_id": None, "content": "hello room",
                  "window_start": "2026-07-20T10:08:00+00:00"},
        label=None,
        excerpt=records,
        bot_user_id="B1",
    )
    assert "standalone" in prompt.lower()
    assert "hello room" in prompt


# --- metrics -----------------------------------------------------------------


def _synthetic_scored_responses() -> list[dict]:
    """Four responses: violation+bad, ok+ok, standalone+ok, label-ok+judge-fail."""
    return [
        {
            "activation_index": 0, "response_index": 0, "reply_to_id": "2",
            "content": "sure bob!", "window_start": "2026-07-20T10:05:00+00:00",
            "label": _label("other_user", "bob's exchange"),
            "verdict": _verdict(False, "bad", "inserted itself"),
        },
        {
            "activation_index": 1, "response_index": 0, "reply_to_id": "3",
            "content": "try uv", "window_start": "2026-07-20T10:10:00+00:00",
            "label": _label("anyone"),
            "verdict": _verdict(True),
        },
        {
            "activation_index": 2, "response_index": 0, "reply_to_id": None,
            "content": "tip of the day", "window_start": "2026-07-20T10:15:00+00:00",
            "label": None,
            "verdict": _verdict(True),
        },
        {
            "activation_index": 3, "response_index": 0, "reply_to_id": "4",
            "content": "I sure am a bot", "window_start": "2026-07-20T10:20:00+00:00",
            "label": _label("bot"),
            "verdict": _verdict(False, "minor", "redundant"),
        },
    ]


def _synthetic_labels() -> dict:
    return {
        "2": _label("other_user", "bob's exchange"),
        "3": _label("anyone"),
        "4": _label("bot"),
        "6": _label("bot", "asked the bot directly"),
        "7": _label("anyone"),
        "8": _label("ambient"),
    }


def test_metrics_math():
    metrics = scoring.compute_metrics(
        _synthetic_scored_responses(), _synthetic_labels()
    )
    assert metrics["responses_total"] == 4
    assert metrics["responses_to_labeled"] == 3
    assert metrics["responses_standalone"] == 1
    assert [v["reply_to_id"] for v in metrics["label_violations"]] == ["2"]
    assert [f["reply_to_id"] for f in metrics["judge_failures"]] == ["2", "4"]
    assert metrics["appropriate_responses"] == 2
    assert metrics["response_precision"] == pytest.approx(0.5)
    # id 4 was answered; id 6 (bot-directed) never was.
    assert metrics["bot_directed_missed"] == ["6"]
    assert metrics["anyone_labeled_total"] == 2
    assert metrics["open_messages_answered"] == 1
    assert metrics["responses_per_hour"] == pytest.approx(4 / 24)
    # Judge disagreements: label allowed but judge failed → the id-4 response.
    assert [d["reply_to_id"] for d in metrics["judge_disagreements"]] == ["4"]


def test_metrics_with_no_responses():
    metrics = scoring.compute_metrics([], _synthetic_labels())
    assert metrics["responses_total"] == 0
    assert metrics["response_precision"] is None
    assert metrics["bot_directed_missed"] == ["4", "6"]


# --- report ------------------------------------------------------------------


def test_report_has_sections_and_violation_excerpts():
    records = [
        _record("1", minute=1, content="hey bob"),
        _record("2", minute=2, author_id="2", display="bob", content="hi carol, yes"),
        _record("3", minute=3, content="anyone tried uv?"),
        _record("4", minute=4, content="hey bot"),
    ]
    responses = _synthetic_scored_responses()
    metrics = scoring.compute_metrics(responses, _synthetic_labels())
    report = scoring.render_report(
        run_record={
            "fixture": "G1-general-2026-07-20.jsonl",
            "adapter": "baseline",
            "model_id": "gemini-3.5-flash-lite",
            "cadence_seconds": 300,
            "history_size": 60,
            "cost_summary": {
                "total_cost_usd": 0.1, "input_tokens": 100, "output_tokens": 10,
                "non_empty_activation_input_tokens": {"mean": 100, "median": 100, "p95": 100},
                "projected_cost_per_day_usd": 0.1,
                "projected_cost_30_days_usd": 3.0,
                "cadence_sensitivity": [
                    {"cadence_seconds": 300, "total_windows": 4, "windows_with_messages": 4}
                ],
                "note": "measured only at 300s",
            },
        },
        fixture_records=records,
        scored_responses=responses,
        metrics=metrics,
        judge_model="claude-sonnet-5",
        judge_cost_usd=1.25,
    )
    for heading in ("## Headline", "## Violations", "## Judge disagreements",
                    "## Misses", "## Cost"):
        assert heading in report
    assert "gemini-3.5-flash-lite" in report
    assert "50.0%" in report            # precision
    # The violation excerpt shows the trigger message line.
    assert "hi carol, yes" in report
    assert "sure bob!" in report        # the offending bot response
    assert "$3.00" in report            # 30-day projection
    assert "claude-sonnet-5" in report


# --- end-to-end flow with cache ---------------------------------------------


def _write_scenario(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    runs_dir = data_dir / "runs"
    runs_dir.mkdir(parents=True)
    stem = "G1-💬general-2026-07-20"
    records = [
        _record("1", minute=1, content="hey bob"),
        _record("2", minute=2, author_id="2", display="bob", content="hi carol"),
        _record("3", minute=3, content="anyone tried uv?"),
        _record("4", minute=4, content="hey bot, you alive?"),
    ]
    (data_dir / f"{stem}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    (data_dir / f"{stem}.meta.json").write_text(
        json.dumps({"bot_user_id": "B1", "channel_name": "💬general",
                    "guild_name": "Smarter Dev"}), encoding="utf-8"
    )
    (data_dir / f"{stem}.labels.json").write_text(
        json.dumps({"labels": _synthetic_labels(), "judge_model": "claude-sonnet-5"}),
        encoding="utf-8",
    )
    run_record = {
        "fixture": f"{stem}.jsonl",
        "adapter": "baseline",
        "model_id": "stub-model",
        "cadence_seconds": 300,
        "history_size": 60,
        "started_at": "x", "finished_at": "y",
        "activations": [
            {"index": 0, "window_start": "2026-07-20T10:00:00+00:00",
             "window_end": "2026-07-20T10:05:00+00:00", "skipped": False,
             "new_message_count": 2, "history_count": 0,
             "responses": [{"reply_to_id": "2", "content": "sure bob!"}],
             "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 0,
             "cost_usd": 0.001},
            {"index": 1, "window_start": "2026-07-20T10:05:00+00:00",
             "window_end": "2026-07-20T10:10:00+00:00", "skipped": True,
             "new_message_count": 0},
            {"index": 2, "window_start": "2026-07-20T10:10:00+00:00",
             "window_end": "2026-07-20T10:15:00+00:00", "skipped": False,
             "new_message_count": 2, "history_count": 2,
             "responses": [{"reply_to_id": "3", "content": "try uv"},
                            {"reply_to_id": None, "content": "tip of the day"}],
             "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 0,
             "cost_usd": 0.001},
        ],
        "totals": {"activations": 3, "activations_with_messages": 2,
                   "activations_with_responses": 2, "responses": 3,
                   "input_tokens": 200, "output_tokens": 20,
                   "cache_read_tokens": 0, "cost_usd": 0.002},
        "cost_summary": {
            "total_cost_usd": 0.002, "input_tokens": 200, "output_tokens": 20,
            "non_empty_activation_input_tokens": {"mean": 100, "median": 100, "p95": 100},
            "projected_cost_per_day_usd": 0.002,
            "projected_cost_30_days_usd": 0.06,
            "cadence_sensitivity": [
                {"cadence_seconds": 300, "total_windows": 3, "windows_with_messages": 2}
            ],
            "note": "measured only at 300s",
        },
    }
    run_path = runs_dir / f"{stem}.baseline.stub-model.300s.json"
    run_path.write_text(json.dumps(run_record), encoding="utf-8")
    return run_path


class StubJudge:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, prompt: str, model: str) -> judge.JudgeReply:
        self.calls.append(prompt)
        payload = {"appropriate": True, "severity": "fine", "reason": "ok"}
        return judge.judge_reply_from_raw(
            {"result": _fenced(payload), "total_cost_usd": 0.25}
        )


def test_score_run_end_to_end_writes_report_and_caches(tmp_path):
    run_path = _write_scenario(tmp_path)
    stub = StubJudge()

    report_text, report_path, metrics = score_run.score_run(
        run_path, judge_callable=stub, judge_model="claude-sonnet-5", force=False
    )

    assert len(stub.calls) == 3  # one per response
    assert report_path == tmp_path / "data" / "reports" / f"{run_path.stem}.md"
    assert report_path.exists()
    assert "## Headline" in report_text
    assert metrics["responses_total"] == 3
    # Label violation (id 2 is other_user) counted even though judge said fine.
    assert [v["reply_to_id"] for v in metrics["label_violations"]] == ["2"]
    assert metrics["appropriate_responses"] == 2
    cache_dir = tmp_path / "data" / ".score_cache" / run_path.stem
    assert sorted(p.name for p in cache_dir.iterdir()) == [
        "response-0-0.json", "response-2-0.json", "response-2-1.json",
    ]

    # Warm cache: zero judge calls on rerun.
    rerun_stub = StubJudge()
    score_run.score_run(
        run_path, judge_callable=rerun_stub, judge_model="claude-sonnet-5", force=False
    )
    assert rerun_stub.calls == []

    # --force clears the cache and re-judges everything.
    forced_stub = StubJudge()
    score_run.score_run(
        run_path, judge_callable=forced_stub, judge_model="claude-sonnet-5", force=True
    )
    assert len(forced_stub.calls) == 3


def test_score_run_fails_fast_without_labels(tmp_path):
    run_path = _write_scenario(tmp_path)
    (tmp_path / "data" / "G1-💬general-2026-07-20.labels.json").unlink()
    with pytest.raises(SystemExit, match="label_day"):
        score_run.score_run(
            run_path, judge_callable=StubJudge(),
            judge_model="claude-sonnet-5", force=False,
        )


class FlakyJudge(StubJudge):
    """First call returns prose (unparseable), then behaves."""

    def __call__(self, prompt: str, model: str) -> judge.JudgeReply:
        first_call = not self.calls
        reply = super().__call__(prompt, model)
        if first_call:
            return judge.judge_reply_from_raw(
                {"result": "seems fine to me!", "total_cost_usd": 0.25}
            )
        return reply


def test_score_run_retries_unparseable_verdicts(tmp_path):
    run_path = _write_scenario(tmp_path)
    flaky = FlakyJudge()
    _, _, metrics = score_run.score_run(
        run_path, judge_callable=flaky, judge_model="claude-sonnet-5", force=False
    )
    assert metrics["responses_total"] == 3
    assert len(flaky.calls) == 4  # one retry for the flaked first verdict
