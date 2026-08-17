#!/usr/bin/env python
"""Score a simulation run with Claude Code as the judge; write the eval report.

Every response in the run record gets a deterministic label check (its
trigger's ground-truth label must allow a response) and a judge check (a
headless claude call shown the surrounding real conversation). The report —
precision headline, violations with transcript excerpts, judge
disagreements, misses, and the cost block — lands in
data/reports/<run-stem>.md; the headline also prints to stdout.

Verdicts are cached under data/.score_cache/<run-stem>/ so reruns are free;
--force re-judges everything.

Usage:
    uv run python -m scripts.proactive_eval.score_run scripts/proactive_eval/data/runs/<run>.json \
        [--judge-model claude-sonnet-5] [--force]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval.judge import (  # noqa: E402
    DEFAULT_JUDGE_MODEL,
    JUDGE_PARSE_RETRIES,
    judge_reply_from_raw,
    run_judge,
)
from scripts.proactive_eval.scoring import (  # noqa: E402
    collect_responses,
    compute_metrics,
    excerpt_for_response,
    headline_section,
    parse_judge_verdict,
    render_judge_prompt,
    render_report,
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _judge_response(
    *,
    response: dict,
    label: dict | None,
    fixture_records: list[dict],
    bot_user_id: str,
    judge_callable,
    judge_model: str,
    cache_path: Path,
) -> tuple[dict, float]:
    """One response's verdict, from cache or the judge. Returns (verdict, cost)."""
    if cache_path.exists():
        reply = judge_reply_from_raw(_load_json(cache_path))
        try:
            return parse_judge_verdict(reply.result_text), reply.cost_usd
        except ValueError as error:
            raise ValueError(
                f"cached verdict {cache_path} is invalid: {error} — delete it "
                f"or rerun with --force"
            ) from error
    prompt = render_judge_prompt(
        response=response,
        label=label,
        excerpt=excerpt_for_response(fixture_records, response),
        bot_user_id=bot_user_id,
    )
    cost_usd = 0.0
    parse_error: ValueError | None = None
    for attempt in range(1 + JUDGE_PARSE_RETRIES):
        reply = judge_callable(prompt, judge_model)
        cost_usd += reply.cost_usd
        try:
            verdict = parse_judge_verdict(reply.result_text)
        except ValueError as error:
            parse_error = error
            print(
                f"response {cache_path.stem}: invalid judge verdict on "
                f"attempt {attempt + 1}: {error}",
                file=sys.stderr,
                flush=True,
            )
            continue
        cache_path.write_text(
            json.dumps(reply.raw, ensure_ascii=False), encoding="utf-8"
        )
        return verdict, cost_usd
    raise ValueError(
        f"response {cache_path.stem}: judge output stayed unparseable after "
        f"{1 + JUDGE_PARSE_RETRIES} attempts: {parse_error}"
    ) from parse_error


def score_run(
    run_path: Path,
    *,
    judge_callable,
    judge_model: str,
    force: bool,
) -> tuple[str, Path, dict]:
    """Score one run record; returns (report text, report path, metrics)."""
    run_record = _load_json(run_path)
    data_dir = run_path.parent.parent
    fixture_path = data_dir / run_record["fixture"]
    fixture_stem = fixture_path.stem
    labels_path = data_dir / f"{fixture_stem}.labels.json"
    if not labels_path.exists():
        raise SystemExit(
            f"No labels file at {labels_path} — run label_day on the fixture "
            f"first."
        )
    meta = _load_json(data_dir / f"{fixture_stem}.meta.json")
    labels = _load_json(labels_path)["labels"]
    fixture_records = [
        json.loads(line)
        for line in fixture_path.read_text(encoding="utf-8").splitlines()
    ]

    cache_dir = data_dir / ".score_cache" / run_path.stem
    if force and cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    responses = collect_responses(run_record)
    scored_responses = []
    judge_cost_usd = 0.0
    for position, response in enumerate(responses):
        label = (
            labels.get(response["reply_to_id"])
            if response["reply_to_id"] is not None
            else None
        )
        verdict, cost_usd = _judge_response(
            response=response,
            label=label,
            fixture_records=fixture_records,
            bot_user_id=meta["bot_user_id"],
            judge_callable=judge_callable,
            judge_model=judge_model,
            cache_path=cache_dir
            / f"response-{response['activation_index']}-{response['response_index']}.json",
        )
        judge_cost_usd += cost_usd
        scored_responses.append({**response, "label": label, "verdict": verdict})
        print(
            f"judged {position + 1}/{len(responses)}: "
            f"{'ok' if verdict['appropriate'] else verdict['severity']}",
            file=sys.stderr,
            flush=True,
        )

    metrics = compute_metrics(scored_responses, labels)
    report_text = render_report(
        run_record=run_record,
        fixture_records=fixture_records,
        scored_responses=scored_responses,
        metrics=metrics,
        judge_model=judge_model,
        judge_cost_usd=judge_cost_usd,
    )
    report_path = data_dir / "reports" / f"{run_path.stem}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    return report_text, report_path, metrics


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="score_run",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("run", type=Path)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report_text, report_path, _ = score_run(
        args.run,
        judge_callable=run_judge,
        judge_model=args.judge_model,
        force=args.force,
    )
    print(headline_section(report_text))
    print(f"\nFull report: {report_path}")


if __name__ == "__main__":
    main()
