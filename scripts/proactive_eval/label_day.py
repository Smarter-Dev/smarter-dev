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
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# JudgeReply/judge_reply_from_raw/run_judge/JUDGE_PARSE_RETRIES are
# re-exported here: they lived in this module before judge.py existed and
# callers (including the stage-2 tests) still import them from label_day.
from scripts.proactive_eval.judge import (  # noqa: E402,F401
    DEFAULT_JUDGE_MODEL,
    JUDGE_PARSE_RETRIES,
    JudgeReply,
    judge_reply_from_raw,
    run_judge,
)
from scripts.proactive_eval.labels import (  # noqa: E402
    build_labels_document,
    chunk_messages,
    format_histogram,
    parse_judge_output,
    render_chunk_prompt,
    speaker_tags,
)

DEFAULT_CHUNK_SIZE = 60
DEFAULT_CONTEXT_SIZE = 20


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
        expected_ids = [target["id"] for target in chunk.targets]
        if cache_path.exists():
            reply = judge_reply_from_raw(
                json.loads(cache_path.read_text(encoding="utf-8"))
            )
            total_cost_usd += reply.cost_usd
            try:
                parsed = parse_judge_output(reply.result_text, expected_ids)
            except ValueError as error:
                raise ValueError(
                    f"chunk {chunk.index} (cache, {cache_path}): {error} "
                    f"— delete the file or rerun with --force"
                ) from error
            source = "cache"
        else:
            prompt = render_chunk_prompt(
                chunk, tags=tags, bot_user_id=meta["bot_user_id"]
            )
            parsed = None
            parse_error: ValueError | None = None
            for attempt in range(1 + JUDGE_PARSE_RETRIES):
                reply = judge(prompt, judge_model)
                total_cost_usd += reply.cost_usd
                try:
                    parsed = parse_judge_output(reply.result_text, expected_ids)
                except ValueError as error:
                    parse_error = error
                    print(
                        f"chunk {chunk.index}: invalid judge reply on attempt "
                        f"{attempt + 1}: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                break
            if parsed is None:
                raise ValueError(
                    f"chunk {chunk.index} (judge, after "
                    f"{1 + JUDGE_PARSE_RETRIES} attempts): {parse_error}"
                ) from parse_error
            # Only validated replies are cached, so a rerun never resumes
            # from a reply the parser already rejected.
            cache_path.write_text(
                json.dumps(reply.raw, ensure_ascii=False), encoding="utf-8"
            )
            source = "judge"
        chunk_outputs.append(parsed)
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
