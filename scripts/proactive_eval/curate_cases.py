#!/usr/bin/env python
"""Curate a focused fixture subset from a scored run: softballs + edge cases.

Softballs are responses that passed both checks (the bot is already doing
well there); edge cases are every response that failed the label check or
the judge. The output is a mini-fixture (JSONL + meta + filtered labels)
covering the conversation windows around those responses, plus a readable
markdown walkthrough — everything lands in data/cases/ (gitignored: real
member messages).

Run score_run first — this reads its cached verdicts and makes no judge
calls.

Usage:
    uv run python -m scripts.proactive_eval.curate_cases scripts/proactive_eval/data/runs/<run>.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval.judge import judge_reply_from_raw  # noqa: E402
from scripts.proactive_eval.labels import (  # noqa: E402
    render_transcript_line,
    speaker_tags,
)
from scripts.proactive_eval.scoring import (  # noqa: E402
    collect_responses,
    excerpt_for_response,
    parse_judge_verdict,
    response_is_appropriate,
)

CASE_EXCERPT_RADIUS = 5


def select_cases(scored_responses: list[dict]) -> dict:
    """Split scored responses into softballs (appropriate) and edge cases."""
    softballs = []
    edge_cases = []
    for response in scored_responses:
        if response_is_appropriate(response["label"], response["verdict"]):
            softballs.append(response)
        else:
            edge_cases.append(response)
    return {"softballs": softballs, "edge_cases": edge_cases}


def subset_records(
    fixture_records: list[dict], cases: dict, *, radius: int = CASE_EXCERPT_RADIUS
) -> list[dict]:
    """The ordered, deduplicated union of every case's conversation window."""
    subset_ids = set()
    for response in cases["softballs"] + cases["edge_cases"]:
        excerpt = excerpt_for_response(
            fixture_records, response, before=radius, after=radius
        )
        subset_ids.update(record["id"] for record in excerpt)
    return [r for r in fixture_records if r["id"] in subset_ids]


def filter_labels(labels: dict, subset_ids: set[str]) -> dict:
    return {
        message_id: label
        for message_id, label in labels.items()
        if message_id in subset_ids
    }


def _case_entry(response: dict, fixture_records: list[dict]) -> list[str]:
    excerpt = excerpt_for_response(
        fixture_records, response, before=2, after=2
    )
    tags = speaker_tags(excerpt)
    trigger = (
        f"trigger id={response['reply_to_id']}"
        if response["reply_to_id"]
        else "standalone"
    )
    lines = [
        f"### activation {response['activation_index']} "
        f"response {response['response_index']} — {trigger}",
        "",
    ]
    lines.extend(f"> {render_transcript_line(r, tags)}" for r in excerpt)
    lines.append("")
    lines.append(f"**Bot responded:** {response['content']}")
    if response["label"] is not None:
        lines.append(
            f"**Label:** {response['label']['directed_at']} — "
            f"{response['label']['reason']}"
        )
    verdict = response["verdict"]
    lines.append(
        f"**Judge:** "
        f"{'appropriate' if verdict['appropriate'] else 'inappropriate'} "
        f"({verdict['severity']}) — {verdict['reason']}"
    )
    lines.append("")
    return lines


def render_cases_markdown(
    cases: dict, fixture_records: list[dict], *, run_name: str
) -> str:
    lines = [
        f"# Curated cases — {run_name}",
        "",
        "Softballs the bot already handles well, and every response that "
        "failed a check. See response-policy.md for the rules these are "
        "judged against.",
        "",
        f"## Softballs ({len(cases['softballs'])})",
        "",
    ]
    for response in cases["softballs"]:
        lines.extend(_case_entry(response, fixture_records))
    lines.append(f"## Edge cases ({len(cases['edge_cases'])})")
    lines.append("")
    for response in cases["edge_cases"]:
        lines.extend(_case_entry(response, fixture_records))
    return "\n".join(lines)


def _load_scored_responses(
    run_path: Path,
) -> tuple[dict, list[dict], dict, dict, list[dict]]:
    run_record = json.loads(run_path.read_text(encoding="utf-8"))
    data_dir = run_path.parent.parent
    fixture_path = data_dir / run_record["fixture"]
    fixture_stem = fixture_path.stem
    fixture_records = [
        json.loads(line)
        for line in fixture_path.read_text(encoding="utf-8").splitlines()
    ]
    labels = json.loads(
        (fixture_path.parent / f"{fixture_stem}.labels.json").read_text(
            encoding="utf-8"
        )
    )["labels"]
    meta = json.loads(
        (fixture_path.parent / f"{fixture_stem}.meta.json").read_text(
            encoding="utf-8"
        )
    )
    cache_dir = data_dir / ".score_cache" / run_path.stem
    scored = []
    for response in collect_responses(run_record):
        cache_path = (
            cache_dir
            / f"response-{response['activation_index']}-{response['response_index']}.json"
        )
        if not cache_path.exists():
            raise SystemExit(
                f"No cached verdict at {cache_path} — run score_run on this "
                f"run first."
            )
        reply = judge_reply_from_raw(
            json.loads(cache_path.read_text(encoding="utf-8"))
        )
        label = (
            labels.get(response["reply_to_id"])
            if response["reply_to_id"] is not None
            else None
        )
        scored.append(
            {
                **response,
                "label": label,
                "verdict": parse_judge_verdict(reply.result_text),
            }
        )
    return run_record, fixture_records, labels, meta, scored


def curate(run_path: Path) -> list[Path]:
    """Write the subset fixture, labels, meta and markdown; return the paths."""
    run_record, fixture_records, labels, meta, scored = _load_scored_responses(
        run_path
    )
    cases = select_cases(scored)
    subset = subset_records(fixture_records, cases)
    subset_ids = {record["id"] for record in subset}

    data_dir = run_path.parent.parent
    cases_dir = data_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    fixture_stem = (data_dir / run_record["fixture"]).stem
    stem = f"{fixture_stem}-cases"

    jsonl_path = cases_dir / f"{stem}.jsonl"
    jsonl_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in subset),
        encoding="utf-8",
    )
    meta_path = cases_dir / f"{stem}.meta.json"
    meta_path.write_text(
        json.dumps(
            {**meta, "curated_from_run": run_path.name,
             "message_count": len(subset)},
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    labels_path = cases_dir / f"{stem}.labels.json"
    labels_path.write_text(
        json.dumps(
            {"fixture": jsonl_path.name,
             "labels": filter_labels(labels, subset_ids)},
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    markdown_path = cases_dir / f"{stem}.md"
    markdown_path.write_text(
        render_cases_markdown(cases, fixture_records, run_name=run_path.name)
        + "\n",
        encoding="utf-8",
    )
    return [jsonl_path, meta_path, labels_path, markdown_path]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="curate_cases",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    written = curate(args.run)
    print("Wrote:")
    for path in written:
        print(f"  {path}")


if __name__ == "__main__":
    main()
