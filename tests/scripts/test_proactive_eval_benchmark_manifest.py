from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.proactive_eval import benchmark_manifest
from scripts.proactive_eval.wake_labels import POLICY_VERSION


def _write_bundle(root: Path, name: str, channel_id: str) -> Path:
    fixture = root / f"{name}.jsonl"
    record = {
        "id": f"{channel_id}-1",
        "timestamp": "2026-09-01T12:00:00+00:00",
        "author_id": "A1",
        "author_name": "alice",
        "author_display": "alice",
        "is_bot": False,
        "content": "does anyone know?",
        "reply_to_id": None,
        "mention_user_ids": [],
        "mention_everyone": False,
        "attachment_count": 0,
        "sticker_count": 0,
        "reaction_counts": {},
        "message_type": 0,
    }
    fixture.write_text(json.dumps(record) + "\n", encoding="utf-8")
    fixture.with_name(f"{name}.meta.json").write_text(
        json.dumps(
            {
                "guild_id": "G1",
                "guild_name": "Guild",
                "channel_id": channel_id,
                "channel_name": "general",
                "bot_user_id": "B1",
            }
        ),
        encoding="utf-8",
    )
    fixture.with_name(f"{name}.labels.json").write_text(
        json.dumps({"labels": {record["id"]: {"ok_to_respond": True}}}),
        encoding="utf-8",
    )
    fixture.with_name(f"{name}.wake-labels.json").write_text(
        json.dumps(
            {
                "policy_version": POLICY_VERSION,
                "labels": {
                    record["id"]: {
                        "required_wake": True,
                        "category": "useful_intervention",
                        "reason": "Open question to the room.",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return fixture


def test_freeze_and_verify_hash_every_private_sidecar(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(benchmark_manifest, "DATA_DIR", tmp_path)
    tuning = _write_bundle(tmp_path, "tuning-day", "C1")
    heldout = _write_bundle(tmp_path, "heldout-day", "C2")

    manifest = benchmark_manifest.freeze([tuning], [heldout])

    assert benchmark_manifest.verify(manifest, full=False) == {
        "tuning": 1,
        "heldout": 1,
        "windows": 2,
    }
    assert len(manifest["fixtures"][0]["sha256"]["wake_labels"]) == 64
    assert manifest["price_assumptions_per_million_tokens_usd"] == {
        "typesafe_input": 0.042,
        "typesafe_output": 0.0,
        "glm_input": 0.075,
        "glm_output": 0.25,
        "glm_cache_read": 0.015,
    }

    tuning.write_text(tuning.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since freeze"):
        benchmark_manifest.verify(manifest, full=False)


def test_full_check_enforces_dataset_and_typesafe_price(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(benchmark_manifest, "DATA_DIR", tmp_path)
    manifest = benchmark_manifest.freeze(
        [_write_bundle(tmp_path, "tuning-day", "C1")],
        [_write_bundle(tmp_path, "heldout-day", "C2")],
    )

    with pytest.raises(ValueError, match="at least 6 channel-days"):
        benchmark_manifest.verify(manifest, full=True)
