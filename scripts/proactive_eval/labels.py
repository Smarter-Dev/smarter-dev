"""Ground-truth label schema, chunking, prompt rendering and merge logic.

Pure functions only — no subprocess, no network, importable anywhere. The
judge invocation lives in label_day.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

DIRECTED_AT_CATEGORIES = ("other_user", "anyone", "bot", "ambient")
OK_TO_RESPOND_CATEGORIES = {"anyone", "bot"}
LABELABLE_MESSAGE_TYPES = {0, 19}

CATEGORY_DEFINITIONS = """\
- `other_user` — addressed to a specific person who is not the bot: a Discord \
reply to someone's message continuing that exchange, an @mention of a \
specific user, an answer inside an ongoing back-and-forth between two people, \
a message using someone's name ("bob did you push it?").
- `anyone` — an open bid to the room: questions, help requests, opinions or \
announcements not aimed at one person ("does anyone know…", "TIL…", showing \
off a project).
- `bot` — addressed to the bot: mentions of the bot's user id, replies to a \
bot message, or the bot addressed by name.
- `ambient` — not a conversational bid at all: bare emoji/reactions-as-text, \
slash-command invocations, "lol", link drops with no ask, greetings into the \
void that a bot butting in on would be odd."""

_FENCED_JSON_PATTERN = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


@dataclass
class Chunk:
    """One judge call's worth of messages.

    ``body`` is every record from the first to the last target inclusive, so
    interleaved bot/system messages stay visible in place; ``targets`` is the
    labelable subset; ``context`` is the window of records immediately before
    the body.
    """

    index: int
    context: list[dict]
    body: list[dict]
    targets: list[dict]


def labelable_messages(records: list[dict]) -> list[dict]:
    """Human default/reply messages — what the ground truth covers."""
    return [
        r
        for r in records
        if not r["is_bot"] and r["message_type"] in LABELABLE_MESSAGE_TYPES
    ]


def speaker_tags(records: list[dict]) -> dict[str, str]:
    """Stable per-author letter tags (A, B, … AA, AB) by first appearance."""
    tags: dict[str, str] = {}
    for record in records:
        author_id = record["author_id"]
        if author_id not in tags:
            tags[author_id] = _letter_tag(len(tags))
    return tags


def _letter_tag(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def chunk_messages(
    records: list[dict], *, chunk_size: int, context_size: int
) -> list[Chunk]:
    targets = labelable_messages(records)
    position_by_id = {r["id"]: i for i, r in enumerate(records)}
    chunks = []
    for chunk_index, start in enumerate(range(0, len(targets), chunk_size)):
        chunk_targets = targets[start : start + chunk_size]
        first_position = position_by_id[chunk_targets[0]["id"]]
        last_position = position_by_id[chunk_targets[-1]["id"]]
        chunks.append(
            Chunk(
                index=chunk_index,
                context=records[
                    max(0, first_position - context_size) : first_position
                ],
                body=records[first_position : last_position + 1],
                targets=chunk_targets,
            )
        )
    return chunks


def render_transcript_line(record: dict, tags: dict[str, str]) -> str:
    bot_marker = "[BOT] " if record["is_bot"] else ""
    reply_marker = (
        f" (reply to id={record['reply_to_id']})" if record["reply_to_id"] else ""
    )
    tag = tags[record["author_id"]]
    return (
        f"[id={record['id']}] {bot_marker}{tag}·{record['author_display']}"
        f"{reply_marker}: {record['content']}"
    )


def render_chunk_prompt(
    chunk: Chunk, *, tags: dict[str, str], bot_user_id: str
) -> str:
    context_lines = "\n".join(
        render_transcript_line(r, tags) for r in chunk.context
    )
    body_lines = "\n".join(render_transcript_line(r, tags) for r in chunk.body)
    target_ids = "\n".join(r["id"] for r in chunk.targets)
    return f"""\
You are labeling one day of Discord #general chat for a proactive-bot eval.
The proactive bot's Discord user id is {bot_user_id}; lines it wrote are marked [BOT].
Speaker tags (A·, B·, …) are stable per author for the whole day.

For each message id listed at the end of this prompt, decide who the message is directed at, using exactly one of:

{CATEGORY_DEFINITIONS}

TRANSCRIPT (oldest first; lines above the marker are context only):
{context_lines}
--- messages to label start here ---
{body_lines}

LABEL THESE (label exactly these ids, no others, one label per id):
{target_ids}

Output ONLY a fenced JSON object mapping each listed id to
{{"directed_at": "<category>", "target_user_id": "<Discord user id or null>", "reason": "one short sentence"}}.
`target_user_id` is the id of the person addressed when `directed_at` is `other_user`; otherwise null.
No prose outside the fence."""


def parse_judge_output(result_text: str, expected_ids: list[str]) -> dict:
    """Extract and validate the judge's fenced JSON. Fail fast on drift."""
    fence_match = _FENCED_JSON_PATTERN.search(result_text)
    if fence_match is None:
        raise ValueError(
            f"no fenced JSON block in judge output: {result_text[:200]!r}"
        )
    payload = json.loads(fence_match.group(1))
    expected = set(expected_ids)
    got = set(payload)
    if expected - got:
        raise ValueError(f"judge output missing ids: {sorted(expected - got)}")
    if got - expected:
        raise ValueError(f"judge output has extra ids: {sorted(got - expected)}")
    for message_id, label in payload.items():
        directed_at = label.get("directed_at")
        if directed_at not in DIRECTED_AT_CATEGORIES:
            raise ValueError(
                f"id {message_id}: invalid directed_at {directed_at!r} "
                f"(must be one of {DIRECTED_AT_CATEGORIES})"
            )
    return payload


def derive_ok_to_respond(directed_at: str) -> bool:
    return directed_at in OK_TO_RESPOND_CATEGORIES


def build_labels_document(
    *,
    fixture_name: str,
    judge_model: str,
    labeled_at: str,
    judge_cost_usd: float,
    chunk_outputs: list[dict],
) -> dict:
    merged = {}
    for chunk_output in chunk_outputs:
        for message_id, label in chunk_output.items():
            merged[message_id] = {
                "directed_at": label["directed_at"],
                "target_user_id": label.get("target_user_id"),
                "ok_to_respond": derive_ok_to_respond(label["directed_at"]),
                "reason": label.get("reason", ""),
            }
    return {
        "fixture": fixture_name,
        "judge_model": judge_model,
        "labeled_at": labeled_at,
        "judge_reported_cost_usd": judge_cost_usd,
        "labels": merged,
    }


def category_counts(doc_labels: dict) -> dict[str, int]:
    counts = {category: 0 for category in DIRECTED_AT_CATEGORIES}
    for label in doc_labels.values():
        counts[label["directed_at"]] += 1
    return counts


def format_histogram(doc_labels: dict) -> str:
    total = len(doc_labels)
    lines = []
    for category, count in category_counts(doc_labels).items():
        share = (count / total * 100) if total else 0.0
        lines.append(f"{category:>11}: {count:>4} ({share:.1f}%)")
    ok_count = sum(1 for label in doc_labels.values() if label["ok_to_respond"])
    ok_share = (ok_count / total * 100) if total else 0.0
    lines.append(f"ok_to_respond: {ok_count}/{total} ({ok_share:.1f}%)")
    return "\n".join(lines)
