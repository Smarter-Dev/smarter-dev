#!/usr/bin/env python
"""Create a private, message-level benchmark review packet from saved results.

The output belongs under the gitignored proactive evaluation data directory.
It never changes frozen labels or result artifacts. Optional reviewer
adjudications live in a separate sidecar and any rescoring is explicitly
reported as post-review analysis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.proactive_eval.benchmark_manifest import DATA_DIR
from scripts.proactive_eval.benchmark_watchers import _cases
from scripts.proactive_eval.wake_labels import POLICY_VERSION
from smarter_dev.bot.proactive.agent import OPERATING_POLICY_BRIEF
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.watcher import build_jev_watcher_instructions
from smarter_dev.bot.proactive.watcher import build_jev_watcher_material

JUDGMENT_FIELDS = (
    "addressed_to_bot_by_name",
    "matches_watch_criteria",
    "useful_intervention",
    "specific_people_exchange",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _indented(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines()) or "    (empty)"


def _display(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _labels_for_fixture(fixture: str) -> dict[str, dict]:
    path = DATA_DIR / fixture
    sidecar = path.with_name(f"{path.stem}.wake-labels.json")
    return json.loads(sidecar.read_text(encoding="utf-8"))["labels"]


def _judgments(record: dict) -> dict[str, Any] | None:
    if record.get("judgments") is not None:
        return record["judgments"]
    return (record.get("classifier") or {}).get("judgments")


def _model_id(record: dict, result: dict) -> str:
    if record.get("model_id") or record.get("requested_model"):
        return str(record.get("model_id") or record.get("requested_model"))
    if str(result.get("plan_version", "")).startswith("glm-"):
        return "glm-5.3-flash"
    return str(result.get("plan_version") or "unknown")


def _provider(record: dict) -> str:
    classifier = record.get("classifier") or {}
    return str(
        record.get("executed_provider")
        or record.get("requested_provider")
        or classifier.get("provider")
        or "not reported"
    )


def _is_failure(record: dict) -> bool:
    return record.get("failure") is not None or bool(record.get("abstained"))


def _decision_disagrees(record: dict) -> bool:
    expected = record.get("expected_wake")
    return (
        expected is not None
        and not _is_failure(record)
        and record.get("wake") != expected
    )


def _case_review_id(case_id: str) -> str:
    return hashlib.sha256(case_id.encode()).hexdigest()[:12]


def _relationship_metadata(messages: list) -> str:
    payload = [
        {
            "message_id": message.id,
            "reply_to_id": message.reply_to_id,
            "mention_user_ids": list(message.mention_user_ids),
            "mention_everyone": message.mention_everyone,
            "is_bot": message.is_bot,
        }
        for message in messages
    ]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _label_evidence_scope(case: dict, labels: dict[str, dict]) -> dict[str, Any]:
    """Report only explicit evidence location; do not reinterpret the label."""
    expected = case["expected_wake"]
    supporting = []
    for message in case["new_messages"]:
        label = labels.get(message.id)
        if label is None:
            continue
        if expected is True and label["required_wake"] is not True:
            continue
        if expected is False and label["required_wake"] is not False:
            continue
        if expected is None and label["required_wake"] is not None:
            continue
        supporting.append((message, label))
    actual_messages = [*case["history"][-30:], *case["new_messages"]]
    actual_ids = {message.id for message in actual_messages}
    omitted_ids = {message.id for message in case["history"][:-30]}
    reply_targets_outside = sorted(
        {
            message.reply_to_id
            for message, _ in supporting
            if message.reply_to_id is not None
            and message.reply_to_id not in actual_ids
            and message.reply_to_id in omitted_ids
        }
    )
    reason_references_outside = sorted(
        {
            omitted_id
            for _, label in supporting
            for omitted_id in omitted_ids
            if omitted_id in str(label.get("reason", ""))
        }
    )
    explicit_outside = bool(reply_targets_outside or reason_references_outside)
    return {
        "supporting_label_target_ids": [message.id for message, _ in supporting],
        "all_supporting_targets_inside_actual_input": all(
            message.id in actual_ids for message, _ in supporting
        ),
        "supporting_reply_targets_in_stored_but_omitted_history": reply_targets_outside,
        "label_reason_explicit_stored_but_omitted_ids": reason_references_outside,
        "explicit_label_support_outside_actual_input": explicit_outside,
        "bounded_interpretation": (
            "explicit structured/referenced support exists outside actual input"
            if explicit_outside
            else "no supporting target or explicit ID/reference lies outside actual input; free-text semantic dependence was not inferred"
        ),
    }


def _post_review_metrics(records: list[dict], adjudications: dict) -> dict | None:
    corrections = {
        case_id: entry.get("proposed_expected_wake")
        for case_id, entry in adjudications.get("cases", {}).items()
        if entry.get("label_review") == "incorrect"
        and entry.get("proposed_expected_wake") in (True, False, None)
    }
    if not corrections:
        return None
    first_observation = {}
    for record in records:
        first_observation.setdefault(record["case_id"], record)
    tp = fp = tn = fn = 0
    reviewed = 0
    for case_id, record in first_observation.items():
        expected = corrections.get(case_id, record.get("expected_wake"))
        if expected is None or _is_failure(record):
            continue
        reviewed += 1
        wake = bool(record.get("wake"))
        tp += expected is True and wake
        fn += expected is True and not wake
        fp += expected is False and wake
        tn += expected is False and not wake
    return {
        "notice": "post-review rescore; frozen labels and original metrics are unchanged",
        "corrected_case_count": len(corrections),
        "evaluable_distinct_cases": reviewed,
        "true_positive": tp,
        "false_negative": fn,
        "false_positive": fp,
        "true_negative": tn,
        "wake_recall": tp / (tp + fn) if tp + fn else None,
        "false_wake_rate": fp / (fp + tn) if fp + tn else None,
    }


def build_report(
    *,
    manifest_path: Path,
    result_path: Path,
    split: str,
    out_path: Path,
    adjudications_path: Path,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    records = result["records"]
    history_size = 60
    by_id = {case["case_id"]: case for case in _cases(manifest, split, history_size)}
    record_ids = {record["case_id"] for record in records}
    missing = sorted(record_ids - set(by_id))
    if missing:
        raise ValueError(f"result contains {len(missing)} cases outside {split}")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        grouped[record["case_id"]].append(record)

    if adjudications_path.exists():
        adjudications = json.loads(adjudications_path.read_text(encoding="utf-8"))
    else:
        adjudications = {
            "schema_version": "benchmark-label-review-v1",
            "source_result_sha256": _sha256(result_path),
            "notice": "Separate post-run review; does not mutate frozen labels or scores.",
            "cases": {
                case_id: {
                    "review_id": _case_review_id(case_id),
                    "original_expected_wake": grouped[case_id][0].get("expected_wake"),
                    "label_review": None,
                    "proposed_expected_wake": None,
                    "reason": "",
                }
                for case_id in sorted(grouped)
            },
        }
        adjudications_path.parent.mkdir(parents=True, exist_ok=True)
        adjudications_path.write_text(
            json.dumps(adjudications, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    failure_cases = sorted(
        case_id
        for case_id, case_records in grouped.items()
        if any(_is_failure(record) for record in case_records)
    )
    disagreement_cases = sorted(
        case_id
        for case_id, case_records in grouped.items()
        if any(_decision_disagrees(record) for record in case_records)
    )
    unstable_cases = sorted(
        case_id
        for case_id, case_records in grouped.items()
        if len(
            {record.get("wake") for record in case_records if not _is_failure(record)}
        )
        > 1
    )
    disagreement_evidence = {
        case_id: _label_evidence_scope(
            by_id[case_id], _labels_for_fixture(by_id[case_id]["fixture"])
        )
        for case_id in disagreement_cases
    }
    disagreement_evidence_outside = sorted(
        case_id
        for case_id, evidence in disagreement_evidence.items()
        if evidence["explicit_label_support_outside_actual_input"]
    )

    lines = [
        "# Private benchmark human-review packet",
        "",
        "> PRIVATE: contains original message content and identifiers. Keep under the gitignored evaluation data directory.",
        "",
        f"- Source manifest: `{manifest_path}` (SHA-256 `{_sha256(manifest_path)}`)",
        f"- Source result: `{result_path}` (SHA-256 `{_sha256(result_path)}`)",
        f"- Frozen split: `{split}`",
        f"- Wake-label policy: `{POLICY_VERSION}`",
        f"- Distinct windows: {len(grouped)}",
        f"- Recorded decisions: {len(records)}",
        f"- Separate adjudication sidecar: `{adjudications_path}`",
        "- Blank reviewer fields do not change the frozen labels or original scores.",
        "- Each case distinguishes the stored case history (up to 60 messages) from the actual classifier input (last 30 stored messages plus the complete new burst).",
        "",
        "## Priority review index",
        "",
        f"- Errors/abstentions ({len(failure_cases)}): "
        + (", ".join(_case_review_id(case_id) for case_id in failure_cases) or "none"),
        f"- Label/decision disagreements ({len(disagreement_cases)}): "
        + (
            ", ".join(_case_review_id(case_id) for case_id in disagreement_cases)
            or "none"
        ),
        f"- Repeat-instability cases ({len(unstable_cases)}): "
        + (", ".join(_case_review_id(case_id) for case_id in unstable_cases) or "none"),
        f"- Disagreements with explicit label target/reply/ID support outside the actual classifier input ({len(disagreement_evidence_outside)}): "
        + (
            ", ".join(
                _case_review_id(case_id) for case_id in disagreement_evidence_outside
            )
            or "none"
        ),
        "",
        "Errors are transport/schema/abstention outcomes and are not scored as wrong decisions. Disagreements are valid decisions that differ from the frozen label.",
        "The evidence-location index is intentionally bounded to explicit targets, reply links, and message IDs in frozen rationales; it does not infer whether free-text semantics depended on stored-but-omitted history.",
        "",
    ]
    post_review = _post_review_metrics(records, adjudications)
    if post_review is not None:
        lines.extend(
            [
                "## Post-review rescore (separate from frozen result)",
                "",
                _indented(json.dumps(post_review, indent=2)),
                "",
            ]
        )
    lines.extend(["## Evaluated windows", ""])

    for case_id in sorted(grouped):
        case = by_id[case_id]
        case_records = sorted(
            grouped[case_id], key=lambda record: int(record.get("repeat", 0))
        )
        review_id = _case_review_id(case_id)
        labels = _labels_for_fixture(case["fixture"])
        message_labels = [
            {"message_id": message.id, **labels[message.id]}
            for message in case["new_messages"]
            if message.id in labels
        ]
        env = ChannelEnvironment(
            visible=[*case["history"], *case["new_messages"]],
            bot_user_id=str(case["meta"]["bot_user_id"]),
        )
        context_transcript = env.render(case["history"][-30:])
        stored_history_transcript = env.render(case["history"])
        omitted_history_transcript = env.render(case["history"][:-30])
        new_transcript = env.render(case["new_messages"])
        material = build_jev_watcher_material(
            context_transcript=context_transcript,
            new_transcript=new_transcript,
        )
        instructions = build_jev_watcher_instructions(
            instructions=OPERATING_POLICY_BRIEF,
            bot_user_id=str(case["meta"]["bot_user_id"]),
            bot_display_name="the bot",
        )
        deterministic = bool(case_records[0].get("deterministic_engagement"))
        evidence_scope = _label_evidence_scope(case, labels)
        lines.extend(
            [
                f"### {review_id}",
                "",
                f"- Private case id: `{case_id}`",
                f"- Original expected wake: **{_display(case_records[0].get('expected_wake'))}**",
                f"- Window categories: `{', '.join(case_records[0].get('categories') or []) or 'none'}`",
                f"- Execution path: `{'deterministic direct engagement' if deterministic else 'model inference'}`",
                f"- Stored prior-message count: `{len(case['history'])}`",
                f"- Actual classifier prior-message count: `{min(30, len(case['history']))}`",
                f"- Stored-but-omitted prior-message count: `{max(0, len(case['history']) - 30)}`",
                "- Reviewer label assessment: `[ ] correct  [ ] incorrect  [ ] ambiguous`",
                "- Proposed corrected expected wake: `unset`",
                "- Reviewer reason: _unset_",
                "",
                "#### Original per-message labels/rationales",
                "",
                _indented(json.dumps(message_labels, indent=2, ensure_ascii=False)),
                "",
                "#### Label-evidence location check",
                "",
                _indented(json.dumps(evidence_scope, indent=2, ensure_ascii=False)),
                "",
                "This check reports only explicit target/reply/ID location. It does not relabel the case or infer whether free-text semantics depended on omitted history.",
                "",
                "#### Stored case history (up to 60; not all sent)",
                "",
                _indented(stored_history_transcript),
                "",
                "#### Stored history omitted from classifier input",
                "",
                _indented(
                    omitted_history_transcript if len(case["history"]) > 30 else "none"
                ),
                "",
                "#### Exact classifier instructions",
                "",
                _indented(
                    "not applicable — classifier was not invoked"
                    if deterministic
                    else instructions
                ),
                "",
                "#### Exact classifier material",
                "",
                _indented(
                    "not applicable — classifier was not invoked; the replay window is reproduced below for human review"
                    if deterministic
                    else material
                ),
                "",
                "#### Actual last-30 classifier context plus complete new burst",
                "",
                _indented(material),
                "",
                "#### Reply/mention relationships",
                "",
                _indented(
                    _relationship_metadata(
                        [*case["history"][-30:], *case["new_messages"]]
                    )
                ),
                "",
                "#### Recorded decisions",
                "",
            ]
        )
        for record in case_records:
            judgments = _judgments(record)
            classifier = record.get("classifier") or {}
            lines.extend(
                [
                    f"- repeat/attempt: `{record.get('repeat', record.get('global_attempt', 0))}`",
                    f"  - model: `{_model_id(record, result)}`",
                    f"  - provider: `{_provider(record)}`",
                    f"  - provider confirmed: `{_display(record.get('provider_confirmed'))}`",
                    f"  - deterministic bypass: `{_display(record.get('deterministic_engagement', False))}`",
                    f"  - actual wake: `{_display(record.get('wake'))}`",
                    f"  - error: `{json.dumps(record.get('failure'), ensure_ascii=False) if record.get('failure') is not None else 'none'}`",
                    f"  - abstained: `{_display(record.get('abstained', False))}`",
                    f"  - four judgments: `{json.dumps(judgments, sort_keys=True) if judgments is not None else 'not applicable'}`",
                    f"  - confidence: `{json.dumps(classifier.get('confidence') or {}, sort_keys=True)}`",
                    f"  - recorded decision reason: `{classifier.get('reason') or 'none'}`",
                ]
            )
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "report_path": str(out_path),
        "report_sha256": _sha256(out_path),
        "adjudications_path": str(adjudications_path),
        "adjudications_sha256": _sha256(adjudications_path),
        "distinct_windows": len(grouped),
        "decisions": len(records),
        "failure_cases": len(failure_cases),
        "disagreement_cases": len(disagreement_cases),
        "disagreement_cases_with_explicit_support_outside_actual_input": len(
            disagreement_evidence_outside
        ),
        "unstable_cases": len(unstable_cases),
        "post_review_rescore": post_review,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("--split", choices=("tuning", "heldout"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--adjudications", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    summary = build_report(
        manifest_path=args.manifest,
        result_path=args.result,
        split=args.split,
        out_path=args.out,
        adjudications_path=args.adjudications,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
