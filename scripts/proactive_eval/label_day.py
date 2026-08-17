#!/usr/bin/env python
"""Label one pulled day of chat with Claude Code as the judge.

For every human default/reply message in a stage-1 fixture, asks a headless
`claude -p … --output-format json` run who the message is directed at
(other_user / anyone / bot / ambient) and writes
<fixture-stem>.labels.json next to the fixture. `ok_to_respond` is derived in
code (anyone or bot), never by the judge.

Chunks are cached under data/.label_cache/<fixture-stem>/ so an interrupted
run resumes where it left off; --force relabels from scratch.

Usage:
    uv run python -m scripts.proactive_eval.label_day scripts/proactive_eval/data/<fixture>.jsonl \
        [--judge-model claude-sonnet-5] [--chunk-size 60] [--context-size 20] [--force]

Requires the `claude` CLI installed and authenticated.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval.labels import (  # noqa: E402
    build_labels_document,
    chunk_messages,
    format_histogram,
    parse_judge_output,
    render_chunk_prompt,
    speaker_tags,
)

JUDGE_TIMEOUT_SECONDS = 300
DEFAULT_JUDGE_MODEL = "claude-sonnet-5"
DEFAULT_CHUNK_SIZE = 60
DEFAULT_CONTEXT_SIZE = 20


@dataclass
class JudgeReply:
    result_text: str
    cost_usd: float
    raw: dict


def judge_reply_from_raw(raw: dict) -> JudgeReply:
    cost = raw.get("total_cost_usd")
    return JudgeReply(
        result_text=raw["result"],
        cost_usd=float(cost) if cost is not None else 0.0,
        raw=raw,
    )


def run_judge(prompt: str, model: str) -> JudgeReply:
    """One headless Claude Code invocation. The only subprocess in the eval."""
    command = ["claude", "-p", prompt, "--output-format", "json", "--model", model]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=JUDGE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise SystemExit(
            "claude CLI not found on PATH — install and authenticate Claude Code"
        ) from error
    if completed.returncode != 0:
        raise SystemExit(
            f"claude exited {completed.returncode}: {completed.stderr[:500]}"
        )
    return judge_reply_from_raw(json.loads(completed.stdout))


def _load_fixture(fixture_path: Path) -> tuple[list[dict], dict]:
    records = [
        json.loads(line)
        for line in fixture_path.read_text(encoding="utf-8").splitlines()
    ]
    meta_path = fixture_path.parent / f"{fixture_path.stem}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return records, meta


def label_fixture(
    fixture_path: Path,
    *,
    judge,
    judge_model: str,
    chunk_size: int,
    context_size: int,
    force: bool,
) -> tuple[dict, Path]:
    """Label every human default/reply message in the fixture.

    ``judge`` is the run_judge callable; tests inject a stub. Returns the
    labels document and the path it was written to.
    """
    records, meta = _load_fixture(fixture_path)
    chunks = chunk_messages(
        records, chunk_size=chunk_size, context_size=context_size
    )
    tags = speaker_tags(records)
    cache_dir = fixture_path.parent / ".label_cache" / fixture_path.stem
    if force and cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    chunk_outputs = []
    total_cost_usd = 0.0
    for chunk in chunks:
        cache_path = cache_dir / f"chunk-{chunk.index:02d}.json"
        if cache_path.exists():
            reply = judge_reply_from_raw(
                json.loads(cache_path.read_text(encoding="utf-8"))
            )
            source = "cache"
        else:
            prompt = render_chunk_prompt(
                chunk, tags=tags, bot_user_id=meta["bot_user_id"]
            )
            reply = judge(prompt, judge_model)
            cache_path.write_text(
                json.dumps(reply.raw, ensure_ascii=False), encoding="utf-8"
            )
            source = "judge"
        expected_ids = [target["id"] for target in chunk.targets]
        try:
            chunk_outputs.append(
                parse_judge_output(reply.result_text, expected_ids)
            )
        except ValueError as error:
            raise ValueError(
                f"chunk {chunk.index} ({source}, {cache_path}): {error}"
            ) from error
        total_cost_usd += reply.cost_usd
        print(
            f"chunk {chunk.index + 1}/{len(chunks)}: "
            f"{len(expected_ids)} messages labeled from {source}",
            file=sys.stderr,
            flush=True,
        )

    document = build_labels_document(
        fixture_name=fixture_path.name,
        judge_model=judge_model,
        labeled_at=datetime.now(UTC).isoformat(),
        judge_cost_usd=round(total_cost_usd, 6),
        chunk_outputs=chunk_outputs,
    )
    labels_path = fixture_path.parent / f"{fixture_path.stem}.labels.json"
    labels_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return document, labels_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="label_day",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--context-size", type=int, default=DEFAULT_CONTEXT_SIZE)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    document, labels_path = label_fixture(
        args.fixture,
        judge=run_judge,
        judge_model=args.judge_model,
        chunk_size=args.chunk_size,
        context_size=args.context_size,
        force=args.force,
    )
    print(f"\nWrote {len(document['labels'])} labels to {labels_path}")
    print(f"Judge-reported cost: ${document['judge_reported_cost_usd']:.4f}\n")
    print(format_histogram(document["labels"]))


if __name__ == "__main__":
    main()
