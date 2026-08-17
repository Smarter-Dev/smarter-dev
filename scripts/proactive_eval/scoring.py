"""Pure scoring logic: verdict combination, metrics, prompts, the report.

A response is appropriate only if BOTH checks pass:
1. label check (deterministic) — its trigger's ground-truth label must have
   ok_to_respond true; standalone responses have no label check;
2. judge check — Claude Code, shown the surrounding real conversation, may
   fail a response the label allowed (barging into a lull, redundant, off-tone).

Misses (bot-directed or open messages nobody answered) are informational
only: the eval's goal is precision — never respond to other people's
exchanges.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval.judge import extract_fenced_json  # noqa: E402
from scripts.proactive_eval.labels import (  # noqa: E402
    render_transcript_line,
    speaker_tags,
)
from scripts.proactive_eval.simulation import format_cost_summary  # noqa: E402

JUDGE_SEVERITIES = ("fine", "minor", "bad")
EXCERPT_BEFORE = 15
EXCERPT_AFTER = 15
VIOLATION_EXCERPT_RADIUS = 2
FIXTURE_DAY_HOURS = 24.0

RULE_TEXT = (
    "The bot may respond only to messages directed at the whole room or at "
    "the bot itself. It must never respond to a message directed at another "
    "specific person — a reply to someone else, an @mention of someone else, "
    "or a message inside an ongoing exchange between two people. It should "
    "also not be annoying or redundant."
)


def response_is_appropriate(label: dict | None, verdict: dict) -> bool:
    """Both checks must pass; a standalone/unlabeled response is judge-only."""
    if label is not None and not label["ok_to_respond"]:
        return False
    return verdict["appropriate"]


def parse_judge_verdict(result_text: str) -> dict:
    payload = json.loads(extract_fenced_json(result_text))
    appropriate = payload.get("appropriate")
    if not isinstance(appropriate, bool):
        raise ValueError(
            f"judge verdict 'appropriate' must be a bool, got {appropriate!r}"
        )
    severity = payload.get("severity")
    if severity not in JUDGE_SEVERITIES:
        raise ValueError(
            f"judge verdict 'severity' must be one of {JUDGE_SEVERITIES}, "
            f"got {severity!r}"
        )
    return {
        "appropriate": appropriate,
        "severity": severity,
        "reason": payload.get("reason", ""),
    }


def collect_responses(run_record: dict) -> list[dict]:
    """Every response of the run, with its activation coordinates."""
    responses = []
    for activation in run_record["activations"]:
        if activation.get("skipped"):
            continue
        for response_index, response in enumerate(activation["responses"]):
            responses.append(
                {
                    "activation_index": activation["index"],
                    "response_index": response_index,
                    "reply_to_id": response["reply_to_id"],
                    "content": response["content"],
                    "window_start": activation["window_start"],
                }
            )
    return responses


def excerpt_for_response(
    fixture_records: list[dict],
    response: dict,
    *,
    before: int = EXCERPT_BEFORE,
    after: int = EXCERPT_AFTER,
) -> list[dict]:
    """The real conversation around the responded-to point.

    Replies center on the trigger message; standalone responses get the
    messages immediately before their activation window (there is no trigger
    to center on).
    """
    reply_to_id = response.get("reply_to_id")
    if reply_to_id is not None:
        position = next(
            (i for i, r in enumerate(fixture_records) if r["id"] == reply_to_id),
            None,
        )
        if position is not None:
            return fixture_records[max(0, position - before) : position + after + 1]
    window_start = response["window_start"]
    preceding = [r for r in fixture_records if r["timestamp"] < window_start]
    return preceding[-(before + after) :]


def render_judge_prompt(
    *, response: dict, label: dict | None, excerpt: list[dict], bot_user_id: str
) -> str:
    tags = speaker_tags(excerpt)
    transcript = "\n".join(
        render_transcript_line(record, tags) for record in excerpt
    ) or "(channel was quiet)"
    if response["reply_to_id"] is not None:
        trigger_lines = [
            f"The bot replied to message id={response['reply_to_id']}."
        ]
        if label is not None:
            trigger_lines.append(
                f"Ground-truth label of that message: "
                f"directed_at={label['directed_at']} — {label['reason']}"
            )
        else:
            trigger_lines.append(
                "That message has no ground-truth label (it was not a human "
                "chat message)."
            )
        trigger_section = "\n".join(trigger_lines)
    else:
        trigger_section = (
            "The bot posted a STANDALONE message (no reply target) into the "
            "channel at this point."
        )
    return f"""\
You are auditing one response from a proactive Discord bot (user id \
{bot_user_id}) that wakes periodically and reviews new messages.

THE RULE: {RULE_TEXT}

TRANSCRIPT of the real human conversation around that moment (the bot's \
candidate response below was NOT part of it — you also see what the humans \
did next):
{transcript}

{trigger_section}

THE BOT'S RESPONSE:
{response['content']}

Was this response appropriate under the rule? Output ONLY a fenced JSON \
object:
{{"appropriate": true|false, "severity": "fine"|"minor"|"bad", "reason": "one short sentence"}}
- "bad" = it inserted itself into another person's exchange
- "minor" = allowed target but poor judgment (redundant, annoying, off-tone)
- "fine" = appropriate
No prose outside the fence."""


def compute_metrics(scored_responses: list[dict], labels: dict) -> dict:
    """Deterministic metrics. ``scored_responses`` carry label and verdict."""
    to_labeled = [r for r in scored_responses if r["label"] is not None]
    standalone = [r for r in scored_responses if r["reply_to_id"] is None]
    to_unlabeled = [
        r
        for r in scored_responses
        if r["reply_to_id"] is not None and r["label"] is None
    ]
    label_violations = [
        r for r in to_labeled if not r["label"]["ok_to_respond"]
    ]
    judge_failures = [
        r for r in scored_responses if not r["verdict"]["appropriate"]
    ]
    judge_disagreements = [
        r
        for r in to_labeled
        if r["label"]["ok_to_respond"] and not r["verdict"]["appropriate"]
    ]
    appropriate = [
        r
        for r in scored_responses
        if response_is_appropriate(r["label"], r["verdict"])
    ]
    answered_ids = {
        r["reply_to_id"] for r in scored_responses if r["reply_to_id"]
    }
    bot_directed_ids = sorted(
        message_id
        for message_id, label in labels.items()
        if label["directed_at"] == "bot"
    )
    anyone_ids = {
        message_id
        for message_id, label in labels.items()
        if label["directed_at"] == "anyone"
    }
    total = len(scored_responses)
    return {
        "responses_total": total,
        "responses_to_labeled": len(to_labeled),
        "responses_standalone": len(standalone),
        "responses_to_unlabeled": len(to_unlabeled),
        "label_violations": label_violations,
        "judge_failures": judge_failures,
        "judge_disagreements": judge_disagreements,
        "appropriate_responses": len(appropriate),
        "response_precision": (len(appropriate) / total) if total else None,
        "bot_directed_missed": [
            message_id
            for message_id in bot_directed_ids
            if message_id not in answered_ids
        ],
        "anyone_labeled_total": len(anyone_ids),
        "open_messages_answered": len(anyone_ids & answered_ids),
        "responses_per_hour": total / FIXTURE_DAY_HOURS,
    }


def _quote_excerpt(fixture_records: list[dict], response: dict) -> str:
    excerpt = excerpt_for_response(
        fixture_records,
        response,
        before=VIOLATION_EXCERPT_RADIUS,
        after=VIOLATION_EXCERPT_RADIUS,
    )
    tags = speaker_tags(excerpt)
    return "\n".join(
        f"> {render_transcript_line(record, tags)}" for record in excerpt
    ) or "> (channel was quiet)"


def _precision_text(precision: float | None) -> str:
    return f"{precision * 100:.1f}%" if precision is not None else "n/a"


def _response_heading(response: dict) -> str:
    trigger = (
        f"trigger id={response['reply_to_id']}"
        if response["reply_to_id"]
        else "standalone"
    )
    return (
        f"activation {response['activation_index']} "
        f"response {response['response_index']} — {trigger}"
    )


def _violation_entry(fixture_records: list[dict], response: dict) -> list[str]:
    lines = [f"### {_response_heading(response)}", ""]
    lines.append(_quote_excerpt(fixture_records, response))
    lines.append("")
    lines.append(f"**Bot responded:** {response['content']}")
    if response["label"] is not None:
        lines.append(
            f"**Label:** {response['label']['directed_at']} — "
            f"{response['label']['reason']}"
        )
    verdict = response["verdict"]
    lines.append(
        f"**Judge:** {'appropriate' if verdict['appropriate'] else 'inappropriate'} "
        f"({verdict['severity']}) — {verdict['reason']}"
    )
    lines.append("")
    return lines


def render_report(
    *,
    run_record: dict,
    fixture_records: list[dict],
    scored_responses: list[dict],
    metrics: dict,
    judge_model: str,
    judge_cost_usd: float,
) -> str:
    content_by_id = {r["id"]: r for r in fixture_records}
    cost_summary = run_record["cost_summary"]
    lines = [
        f"# Proactive-bot eval — {run_record['fixture']}",
        "",
        f"- Adapter: {run_record['adapter']} on {run_record['model_id']}, "
        f"waking every {run_record['cadence_seconds']}s "
        f"(history {run_record['history_size']})",
        f"- Judge: {judge_model} (judge-reported cost ${judge_cost_usd:.2f})",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Response precision (appropriate / total) | "
        f"{_precision_text(metrics['response_precision'])} "
        f"({metrics['appropriate_responses']}/{metrics['responses_total']}) |",
        f"| Label violations (responded to other_user/ambient) | "
        f"{len(metrics['label_violations'])} |",
        f"| Judge failures | {len(metrics['judge_failures'])} |",
        f"| Bot-directed messages missed | "
        f"{len(metrics['bot_directed_missed'])} |",
        f"| Open (anyone) messages answered | "
        f"{metrics['open_messages_answered']}/{metrics['anyone_labeled_total']} |",
        f"| Responses per hour | {metrics['responses_per_hour']:.2f} |",
        f"| Day cost ({run_record['model_id']}) | "
        f"${cost_summary['total_cost_usd']:.4f} |",
        f"| Projected 30-day cost | "
        f"${cost_summary['projected_cost_30_days_usd']:.2f} |",
        "",
        "## Violations",
        "",
    ]
    violation_keys = set()
    violation_entries = []
    for response in metrics["label_violations"] + [
        r for r in metrics["judge_failures"] if r["verdict"]["severity"] == "bad"
    ]:
        key = (response["activation_index"], response["response_index"])
        if key in violation_keys:
            continue
        violation_keys.add(key)
        violation_entries.append(response)
    if violation_entries:
        for response in violation_entries:
            lines.extend(_violation_entry(fixture_records, response))
    else:
        lines.append("None.")
        lines.append("")

    lines.append("## Judge disagreements")
    lines.append("")
    lines.append(
        "_Label allowed the response but the judge failed it — the cases to "
        "study when tuning the future bot._"
        if metrics["judge_disagreements"]
        else "None."
    )
    lines.append("")
    for response in metrics["judge_disagreements"]:
        lines.extend(_violation_entry(fixture_records, response))

    lines.append("## Misses (informational)")
    lines.append("")
    if metrics["bot_directed_missed"]:
        lines.append("Bot-directed messages that never got a response:")
        for message_id in metrics["bot_directed_missed"]:
            record = content_by_id.get(message_id)
            excerpt = record["content"][:120] if record else "(not in fixture)"
            lines.append(f"- id={message_id}: {excerpt}")
    else:
        lines.append("No bot-directed messages were missed.")
    lines.append(
        f"\nOpen messages answered: {metrics['open_messages_answered']} of "
        f"{metrics['anyone_labeled_total']} labeled `anyone`."
    )
    lines.append("")
    lines.append("## Cost")
    lines.append("")
    lines.append("```")
    lines.append(format_cost_summary(cost_summary))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def headline_section(report_text: str) -> str:
    """Everything up to the Violations section — what the CLI prints."""
    return report_text.split("## Violations")[0].rstrip()
