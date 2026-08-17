"""Tests for scripts/proactive_eval/labels.py and label_day.py.

The judge is always a stub here — no test launches the real claude CLI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval import label_day, labels  # noqa: E402


def _record(
    message_id: str,
    *,
    author_id: str = "1",
    display: str = "alice",
    bot: bool = False,
    message_type: int = 0,
    reply_to_id: str | None = None,
    content: str = "hello",
) -> dict:
    return {
        "id": message_id,
        "timestamp": "2026-07-20T12:00:00+00:00",
        "author_id": author_id,
        "author_name": display,
        "author_display": display,
        "is_bot": bot,
        "content": content,
        "reply_to_id": reply_to_id,
        "mention_user_ids": [],
        "mention_everyone": False,
        "attachment_count": 0,
        "sticker_count": 0,
        "reaction_counts": {},
        "message_type": message_type,
    }


# --- labelable set and chunking ----------------------------------------------


def test_labelable_excludes_bots_and_system_messages():
    records = [
        _record("1"),
        _record("2", bot=True),
        _record("3", message_type=6),  # channel pin notice
        _record("4", message_type=19, reply_to_id="1"),
    ]
    assert [r["id"] for r in labels.labelable_messages(records)] == ["1", "4"]


def test_chunks_have_requested_size_and_context_windows():
    records = [_record(str(n)) for n in range(1, 11)]  # ten human messages
    chunks = labels.chunk_messages(records, chunk_size=4, context_size=3)

    assert [len(c.targets) for c in chunks] == [4, 4, 2]
    assert [c.index for c in chunks] == [0, 1, 2]

    # First chunk has no context; later chunks carry the 3 records
    # immediately before their first target.
    assert chunks[0].context == []
    assert [r["id"] for r in chunks[1].context] == ["2", "3", "4"]
    assert [r["id"] for r in chunks[2].context] == ["6", "7", "8"]


def test_chunk_body_keeps_interleaved_bot_messages_as_context_in_place():
    records = [
        _record("1"),
        _record("2", bot=True, display="smarter-bot"),
        _record("3"),
    ]
    (chunk,) = labels.chunk_messages(records, chunk_size=10, context_size=5)
    assert [r["id"] for r in chunk.body] == ["1", "2", "3"]
    assert [r["id"] for r in chunk.targets] == ["1", "3"]


# --- prompt rendering --------------------------------------------------------


def _one_chunk(records: list[dict]) -> labels.Chunk:
    (chunk,) = labels.chunk_messages(records, chunk_size=100, context_size=100)
    return chunk


def test_prompt_lists_exactly_the_target_ids_in_label_these():
    records = [
        _record("10"),
        _record("11", bot=True, display="smarter-bot"),
        _record("12", author_id="2", display="bob"),
    ]
    chunk = _one_chunk(records)
    prompt = labels.render_chunk_prompt(
        chunk, tags=labels.speaker_tags(records), bot_user_id="B1"
    )

    label_section = prompt.split("LABEL THESE")[1]
    assert "10" in label_section
    assert "12" in label_section
    assert "11" not in label_section  # bot message is context only


def test_prompt_context_ids_appear_in_transcript_not_label_these():
    records = [_record(str(n)) for n in range(1, 8)]
    second_chunk = labels.chunk_messages(records, chunk_size=4, context_size=2)[1]
    prompt = labels.render_chunk_prompt(
        second_chunk, tags=labels.speaker_tags(records), bot_user_id="B1"
    )
    label_section = prompt.split("LABEL THESE")[1]
    assert "[id=3]" in prompt and "[id=4]" in prompt  # context in transcript
    assert "\n3\n" not in label_section and "\n4\n" not in label_section


def test_prompt_contains_category_definitions_and_bot_marker():
    records = [
        _record("1"),
        _record("2", bot=True, display="smarter-bot"),
        _record("3", message_type=19, reply_to_id="1"),
    ]
    chunk = _one_chunk(records)
    prompt = labels.render_chunk_prompt(
        chunk, tags=labels.speaker_tags(records), bot_user_id="B1"
    )
    for category in ("other_user", "anyone", "bot", "ambient"):
        assert f"`{category}`" in prompt
    assert "[BOT]" in prompt
    assert "B1" in prompt
    assert "(reply to id=1)" in prompt


def test_speaker_tags_are_stable_single_letters_by_first_appearance():
    records = [
        _record("1", author_id="9", display="zoe"),
        _record("2", author_id="4", display="amy"),
        _record("3", author_id="9", display="zoe"),
    ]
    tags = labels.speaker_tags(records)
    assert tags == {"9": "A", "4": "B"}


# --- judge output parsing ----------------------------------------------------


def _fenced(payload: dict) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"


def _valid_label(directed_at: str = "anyone") -> dict:
    return {"directed_at": directed_at, "target_user_id": None, "reason": "open question"}


def test_parse_extracts_first_fenced_json_block():
    payload = {"1": _valid_label()}
    text = "Sure, here are the labels:\n" + _fenced(payload) + "\ntrailing prose"
    assert labels.parse_judge_output(text, ["1"]) == payload


def test_parse_accepts_unlabeled_fence():
    payload = {"1": _valid_label("ambient")}
    text = "```\n" + json.dumps(payload) + "\n```"
    assert labels.parse_judge_output(text, ["1"]) == payload


def test_parse_rejects_missing_id():
    with pytest.raises(ValueError, match="missing"):
        labels.parse_judge_output(_fenced({"1": _valid_label()}), ["1", "2"])


def test_parse_rejects_extra_id():
    payload = {"1": _valid_label(), "99": _valid_label()}
    with pytest.raises(ValueError, match="extra"):
        labels.parse_judge_output(_fenced(payload), ["1"])


def test_parse_rejects_unknown_category():
    payload = {"1": {"directed_at": "everyone", "target_user_id": None, "reason": "x"}}
    with pytest.raises(ValueError, match="directed_at"):
        labels.parse_judge_output(_fenced(payload), ["1"])


def test_parse_rejects_output_without_fenced_json():
    with pytest.raises(ValueError, match="fenced"):
        labels.parse_judge_output("no json here", ["1"])


# --- ok_to_respond derivation ------------------------------------------------


@pytest.mark.parametrize(
    ("directed_at", "expected"),
    [("anyone", True), ("bot", True), ("other_user", False), ("ambient", False)],
)
def test_ok_to_respond_derivation(directed_at, expected):
    assert labels.derive_ok_to_respond(directed_at) is expected


# --- merge and histogram -----------------------------------------------------


def test_build_labels_document_merges_chunks_and_derives_ok_to_respond():
    doc = labels.build_labels_document(
        fixture_name="day.jsonl",
        judge_model="claude-sonnet-5",
        labeled_at="2026-08-17T00:00:00+00:00",
        judge_cost_usd=0.42,
        chunk_outputs=[
            {"1": _valid_label("anyone")},
            {"2": {"directed_at": "other_user", "target_user_id": "5", "reason": "reply to bob"}},
        ],
    )
    assert doc["fixture"] == "day.jsonl"
    assert doc["judge_model"] == "claude-sonnet-5"
    assert doc["judge_reported_cost_usd"] == 0.42
    assert doc["labels"]["1"]["ok_to_respond"] is True
    assert doc["labels"]["2"]["ok_to_respond"] is False
    assert doc["labels"]["2"]["target_user_id"] == "5"


def test_histogram_counts_and_percentage():
    doc_labels = {
        "1": {"directed_at": "anyone", "ok_to_respond": True},
        "2": {"directed_at": "other_user", "ok_to_respond": False},
        "3": {"directed_at": "other_user", "ok_to_respond": False},
        "4": {"directed_at": "ambient", "ok_to_respond": False},
    }
    counts = labels.category_counts(doc_labels)
    assert counts == {"other_user": 2, "anyone": 1, "bot": 0, "ambient": 1}
    rendered = labels.format_histogram(doc_labels)
    assert "other_user" in rendered
    assert "25.0%" in rendered  # 1 of 4 ok_to_respond


# --- label_day flow with a stub judge ---------------------------------------


def _write_fixture(tmp_path: Path, records: list[dict]) -> Path:
    # The real fixture name carries a literal emoji; mirror that here.
    fixture_path = tmp_path / "G1-💬general-2026-07-20.jsonl"
    fixture_path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    meta_path = tmp_path / "G1-💬general-2026-07-20.meta.json"
    meta_path.write_text(
        json.dumps({"bot_user_id": "B1", "channel_name": "💬general"}),
        encoding="utf-8",
    )
    return fixture_path


class _StubJudge:
    """Answers every prompt by labeling all LABEL THESE ids as anyone."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, prompt: str, model: str) -> label_day.JudgeReply:
        self.calls.append(prompt)
        label_section = prompt.split("LABEL THESE")[1]
        requested = [
            line for line in label_section.splitlines()
            if line.strip().isdigit()
        ]
        payload = {message_id.strip(): _valid_label() for message_id in requested}
        raw = {
            "result": _fenced(payload),
            "total_cost_usd": 0.01,
        }
        return label_day.judge_reply_from_raw(raw)


def test_label_fixture_writes_labels_file_for_all_human_messages(tmp_path):
    records = [_record(str(n)) for n in range(1, 8)] + [
        _record("8", bot=True, display="smarter-bot")
    ]
    fixture_path = _write_fixture(tmp_path, records)
    judge = _StubJudge()

    doc, labels_path = label_day.label_fixture(
        fixture_path,
        judge=judge,
        judge_model="claude-sonnet-5",
        chunk_size=3,
        context_size=2,
        force=False,
    )

    assert labels_path.name == "G1-💬general-2026-07-20.labels.json"
    written = json.loads(labels_path.read_text())
    assert set(written["labels"]) == {str(n) for n in range(1, 8)}
    assert written["judge_model"] == "claude-sonnet-5"
    assert written["judge_reported_cost_usd"] == pytest.approx(0.03)
    assert len(judge.calls) == 3  # 7 messages / chunk_size 3


def test_label_fixture_reuses_cached_chunks(tmp_path):
    records = [_record(str(n)) for n in range(1, 8)]
    fixture_path = _write_fixture(tmp_path, records)

    first_judge = _StubJudge()
    label_day.label_fixture(
        fixture_path,
        judge=first_judge,
        judge_model="claude-sonnet-5",
        chunk_size=3,
        context_size=2,
        force=False,
    )
    assert len(first_judge.calls) == 3

    cache_dir = tmp_path / ".label_cache" / "G1-💬general-2026-07-20"
    cache_files = sorted(p.name for p in cache_dir.iterdir())
    assert cache_files == ["chunk-00.json", "chunk-01.json", "chunk-02.json"]

    # Drop one cached chunk: only that chunk should re-invoke the judge.
    (cache_dir / "chunk-01.json").unlink()
    second_judge = _StubJudge()
    label_day.label_fixture(
        fixture_path,
        judge=second_judge,
        judge_model="claude-sonnet-5",
        chunk_size=3,
        context_size=2,
        force=False,
    )
    assert len(second_judge.calls) == 1

    # --force clears the cache and re-invokes everything.
    third_judge = _StubJudge()
    label_day.label_fixture(
        fixture_path,
        judge=third_judge,
        judge_model="claude-sonnet-5",
        chunk_size=3,
        context_size=2,
        force=True,
    )
    assert len(third_judge.calls) == 3


def test_label_fixture_fails_fast_on_invalid_cached_chunk(tmp_path):
    records = [_record("1")]
    fixture_path = _write_fixture(tmp_path, records)
    cache_dir = tmp_path / ".label_cache" / "G1-💬general-2026-07-20"
    cache_dir.mkdir(parents=True)
    bad_payload = {"result": _fenced({"999": _valid_label()}), "total_cost_usd": 0}
    (cache_dir / "chunk-00.json").write_text(json.dumps(bad_payload))

    with pytest.raises(ValueError, match="chunk 0"):
        label_day.label_fixture(
            fixture_path,
            judge=_StubJudge(),
            judge_model="claude-sonnet-5",
            chunk_size=10,
            context_size=2,
            force=False,
        )


# --- judge reply plumbing ----------------------------------------------------


def test_judge_reply_from_raw_reads_result_and_cost():
    reply = label_day.judge_reply_from_raw(
        {"result": "text", "total_cost_usd": 1.25, "usage": {}}
    )
    assert reply.result_text == "text"
    assert reply.cost_usd == 1.25
    assert reply.raw["usage"] == {}


def test_judge_reply_from_raw_tolerates_missing_cost():
    reply = label_day.judge_reply_from_raw({"result": "text"})
    assert reply.cost_usd == 0.0
