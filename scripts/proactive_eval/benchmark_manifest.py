#!/usr/bin/env python
"""Freeze or verify the private real-message benchmark manifest.

The manifest contains paths, hashes, split assignments, counts, run controls,
and price assumptions, but never copies message content out of the gitignored
``data/`` workflow.

Examples:
    uv run python -m scripts.proactive_eval.benchmark_manifest freeze \
      --tuning data/guild-channel-day-a.jsonl \
      --heldout data/guild-channel-day-b.jsonl
    uv run python -m scripts.proactive_eval.benchmark_manifest check \
      data/benchmark-manifest.json --full
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path

from scripts.proactive_eval.wake_labels import POLICY_VERSION
from scripts.proactive_eval.wake_labels import validate_document
from smarter_dev.bot.proactive.windows import two_pass_windows

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_MANIFEST = DATA_DIR / "benchmark-manifest.json"
SCHEMA_VERSION = 1
MIN_FIXTURES_PER_SPLIT = 6
TARGET_WINDOWS = 240


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sidecar(fixture: Path, suffix: str) -> Path:
    return fixture.with_name(f"{fixture.stem}.{suffix}.json")


def _records(fixture: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in fixture.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _manifest_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(DATA_DIR.resolve()))
    except ValueError as error:
        raise ValueError(f"fixture must be under {DATA_DIR}: {path}") from error


def _nonempty_window_count(records: list[dict]) -> int:
    timestamps = [datetime.fromisoformat(record["timestamp"]) for record in records]
    cursor = 0
    count = 0
    for _, window_end in two_pass_windows(timestamps):
        start_cursor = cursor
        while cursor < len(timestamps) and timestamps[cursor] < window_end:
            cursor += 1
        count += cursor > start_cursor
    return count


def fixture_entry(fixture: Path, split: str) -> dict:
    fixture = fixture.resolve()
    meta_path = _sidecar(fixture, "meta")
    labels_path = _sidecar(fixture, "labels")
    wake_labels_path = _sidecar(fixture, "wake-labels")
    required = (fixture, meta_path, labels_path, wake_labels_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"fixture bundle is incomplete: {missing}")
    records = _records(fixture)
    wake_document = json.loads(wake_labels_path.read_text(encoding="utf-8"))
    wake_counts = validate_document(records, wake_document)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return {
        "fixture": _manifest_path(fixture),
        "split": split,
        "guild_id": str(meta.get("guild_id", "")),
        "channel_id": str(meta["channel_id"]),
        "channel_name": meta["channel_name"],
        "utc_date": records[0]["timestamp"][:10] if records else None,
        "message_count": len(records),
        "window_count": _nonempty_window_count(records),
        "wake_label_counts": wake_counts,
        "sha256": {
            "fixture": sha256(fixture),
            "meta": sha256(meta_path),
            "response_labels": sha256(labels_path),
            "wake_labels": sha256(wake_labels_path),
        },
    }


def freeze(tuning: list[Path], heldout: list[Path]) -> dict:
    entries = [fixture_entry(path, "tuning") for path in tuning]
    entries.extend(fixture_entry(path, "heldout") for path in heldout)
    identities: dict[tuple[str, str, str | None], str] = {}
    for entry in entries:
        identity = (entry["guild_id"], entry["channel_id"], entry["utc_date"])
        previous = identities.setdefault(identity, entry["split"])
        if previous != entry["split"]:
            raise ValueError(
                f"channel-day {identity} appears in both tuning and heldout"
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "label_policy_version": POLICY_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "models": {
            "jev": "typesafe:jev-latest",
            "glm": "z-ai/glm-5.3-flash",
            "skim": "z-ai/glm-5.3-flash",
            "responder": "gemini-3.8-flash",
        },
        "design": {
            "target_windows": TARGET_WINDOWS,
            "classifier_samples_per_split": TARGET_WINDOWS // 2,
            "case_selection_seed": 20260920,
            "case_selection": "outcome-blind proportional sample by channel-day",
            "classifier_repeats": 3,
            "end_to_end_repeats": 3,
            "concurrency": 1,
            "cache": "disabled/provider-default prompt caching only",
            "max_classifier_calls": 1440,
            "total_spend_cap_usd": 50.0,
            "phase_spend_caps_usd": {
                "smoke": 1.0,
                "tuning": 10.0,
                "heldout_classifier": 15.0,
                "end_to_end_and_judge": 24.0,
            },
        },
        "price_assumptions_per_million_tokens_usd": {
            # Public Jev pricing published 2026-09-15. Account-specific rates
            # may replace these before freezing the private manifest.
            "typesafe_input": 0.042,
            "typesafe_output": 0.0,
            # OpenRouter promotional list price observed 2026-09-20.
            "glm_input": 0.075,
            "glm_output": 0.25,
            "glm_cache_read": 0.015,
        },
        "fixtures": entries,
    }


def verify(manifest: dict, *, full: bool) -> dict[str, int]:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    if manifest.get("label_policy_version") != POLICY_VERSION:
        raise ValueError(f"label_policy_version must be {POLICY_VERSION}")
    split_counts = {"tuning": 0, "heldout": 0}
    total_windows = 0
    for expected in manifest.get("fixtures", []):
        path = DATA_DIR / expected["fixture"]
        actual = fixture_entry(path, expected["split"])
        if actual != expected:
            raise ValueError(f"fixture bundle changed since freeze: {path}")
        split_counts[expected["split"]] += 1
        total_windows += expected["window_count"]
    if full:
        if any(count < MIN_FIXTURES_PER_SPLIT for count in split_counts.values()):
            raise ValueError(
                f"full benchmark needs at least {MIN_FIXTURES_PER_SPLIT} "
                f"channel-days per split; got {split_counts}"
            )
        if total_windows < TARGET_WINDOWS:
            raise ValueError(
                f"full benchmark needs at least {TARGET_WINDOWS} windows; "
                f"got {total_windows}"
            )
        pricing = manifest["price_assumptions_per_million_tokens_usd"]
        if pricing["typesafe_input"] is None or pricing["typesafe_output"] is None:
            raise ValueError(
                "record the current TypeSafe account rates before a full paid run"
            )
    return {**split_counts, "windows": total_windows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze_parser = commands.add_parser("freeze")
    freeze_parser.add_argument("--tuning", type=Path, action="append", default=[])
    freeze_parser.add_argument("--heldout", type=Path, action="append", default=[])
    freeze_parser.add_argument("--out", type=Path, default=DEFAULT_MANIFEST)
    check_parser = commands.add_parser("check")
    check_parser.add_argument("manifest", type=Path)
    check_parser.add_argument("--full", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "freeze":
        document = freeze(args.tuning, args.heldout)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        counts = verify(document, full=False)
        print(f"Wrote {args.out}: {counts}")
    else:
        document = json.loads(args.manifest.read_text(encoding="utf-8"))
        print(verify(document, full=args.full))


if __name__ == "__main__":
    main()
