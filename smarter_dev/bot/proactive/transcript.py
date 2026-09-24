"""Transcript rendering shared by the eval labeler and the proactive bot.

Stable single-letter speaker tags plus real display names, message ids,
reply markers and a [BOT] prefix for bot-authored lines.
"""

from __future__ import annotations

from datetime import datetime

from smarter_dev.bot.proactive.timestamps import utc_timestamp


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


def render_transcript_line(
    record: dict, tags: dict[str, str], *, attachment_urls: bool = True
) -> str:
    bot_marker = "[BOT] " if record["is_bot"] else ""
    reply_marker = (
        f" (reply to id={record['reply_to_id']})" if record["reply_to_id"] else ""
    )
    tag = tags[record["author_id"]]
    stamp = utc_timestamp(datetime.fromisoformat(record["timestamp"]))
    return (
        f"[{stamp}] [id={record['id']}] {bot_marker}{tag}·{record['author_display']}"
        f"{reply_marker}: {with_attachments(record, urls=attachment_urls)}"
    )


def with_attachments(record: dict, *, urls: bool = True) -> str:
    """A message's text followed by a marker per attachment, each with the
    URL the agent passes to ``web_read``. Records without details (older
    fixtures) still say how many files there were."""
    attachments = record.get("attachments") or ()
    parts = [f"[attachment: {_describe(a, url=urls)}]" for a in attachments]
    unlisted = record.get("attachment_count", 0) - len(attachments)
    if unlisted > 0:
        noun = "attachment" if unlisted == 1 else "attachments"
        parts.append(f"[{unlisted} {noun}, no details]")
    return " ".join(part for part in (record["content"], *parts) if part)


def _describe(attachment: dict, *, url: bool) -> str:
    details = [attachment["filename"]]
    if attachment.get("content_type"):
        details.append(attachment["content_type"])
    if attachment.get("size"):
        details.append(_format_size(attachment["size"]))
    if url:
        details.append(f"url={attachment['url']}")
    return ", ".join(details)


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
