"""Where a member posted in the last couple of minutes, for burst detection.

A compromised account posts the same scam in channel after channel within
seconds, faster than one AI review can finish. A handler that only sees the
message in hand cannot tell that from one ordinary post, and once a review does
confirm the scam it has no way to find the other copies. Dispatch therefore
notes every human guild message here, and an admin handler script reads the
author's notes back with ``list_recent_messages`` — once when a message arrives
(is this a burst?) and again when the verdict lands (which messages to remove).

What is kept: the channel, the message id, when it was posted, how many files
it carried, whether it carried a link, and a short hash of what it carried so
copies of one post can be told from two different posts. Never the text. Each author's set holds at most :data:`MAX_RECENT_MESSAGES` notes. A
note is never read back after :data:`RECENT_MESSAGES_TTL_SECONDS`; it is erased
by the author's next message after that, or with the whole key that long after
their last message — so no later than twice that after it was written.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC
from datetime import datetime

RECENT_MESSAGES_TTL_SECONDS = 120
MAX_RECENT_MESSAGES = 30

_KEY_PREFIX = "hrecent:"
_DISCORD_EPOCH_MS = 1420070400000
_LINK_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def recent_messages_key(guild_id: str, author_id: str) -> str:
    return f"{_KEY_PREFIX}{guild_id}:{author_id}"


def message_posted_at(message_id: str, fallback: datetime) -> datetime:
    """When Discord minted ``message_id``, or ``fallback`` for a non-snowflake.

    The snowflake's own timestamp is used rather than the dispatch time so the
    gap between two messages is the gap between the posts, not between two API
    calls that may have queued behind each other.
    """
    try:
        posted_ms = (int(message_id) >> 22) + _DISCORD_EPOCH_MS
    except (TypeError, ValueError):
        return fallback
    return datetime.fromtimestamp(posted_ms / 1000, tz=UTC)


def content_hash(trigger_context: dict) -> str:
    """A short hash of what a message carried: its text and its files.

    Two messages share it only when the text matches (ignoring case and
    spacing) and the files have the same names and sizes. That is what one scam
    pasted into several channels looks like, and what two different screenshots
    do not. The author id is mixed in, so a hash is only comparable within one
    member's notes.
    """
    files = sorted(
        [str(item.get("filename") or ""), item.get("size")]
        for item in trigger_context.get("attachments") or []
    )
    text = " ".join((trigger_context.get("message_content") or "").split())
    carried = json.dumps(
        [str(trigger_context.get("author_id") or ""), text.casefold(), files],
        default=str,
    )
    return hashlib.sha256(carried.encode("utf-8")).hexdigest()[:16]


def recent_message_entry(trigger_context: dict, channel_id: str, now: datetime) -> dict:
    """The note kept for one message trigger.

    ``channel_id`` is the dispatch channel, which for a message inside a thread
    is the thread's PARENT; the note records the thread itself, because that is
    where the message lives and where ``delete_message`` has to be aimed.
    """
    message_id = str(trigger_context.get("message_id") or "")
    posted_in = (
        str(trigger_context.get("thread_id") or channel_id)
        if trigger_context.get("is_thread")
        else channel_id
    )
    return {
        "channel_id": posted_in,
        "message_id": message_id,
        "posted_at": message_posted_at(message_id, now).timestamp(),
        "attachment_count": len(trigger_context.get("attachments") or []),
        "has_link": bool(
            _LINK_PATTERN.search(trigger_context.get("message_content") or "")
        ),
        "content_hash": content_hash(trigger_context),
    }


async def record_recent_message(
    redis, guild_id: str, author_id: str, entry: dict, now: datetime | None = None
) -> None:
    """Note one message, and erase this author's notes that are past the window.

    The key's TTL is refreshed by every message, so without the erase an author
    who keeps posting would keep every old note alive until the cap pushed it
    out.
    """
    key = recent_messages_key(guild_id, author_id)
    written_at = (now or datetime.now(UTC)).timestamp()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zadd(key, {json.dumps(entry, sort_keys=True): float(entry["posted_at"])})
        pipe.zremrangebyscore(
            key, "-inf", f"({written_at - RECENT_MESSAGES_TTL_SECONDS}"
        )
        pipe.zremrangebyrank(key, 0, -(MAX_RECENT_MESSAGES + 1))
        pipe.expire(key, RECENT_MESSAGES_TTL_SECONDS)
        await pipe.execute()


async def read_recent_messages(
    redis, guild_id: str, author_id: str, now: datetime | None = None
) -> list[dict]:
    """This author's messages from the last two minutes, newest first.

    Each row is ``{"channel_id", "message_id", "age_seconds",
    "attachment_count", "has_link", "content_hash"}``. A note past the window
    that no later message has erased yet is not returned.
    """
    read_at = (now or datetime.now(UTC)).timestamp()
    rows = []
    for raw in await redis.zrangebyscore(
        recent_messages_key(guild_id, author_id),
        read_at - RECENT_MESSAGES_TTL_SECONDS,
        "+inf",
    ):
        entry = json.loads(raw)
        rows.append(
            {
                "channel_id": entry["channel_id"],
                "message_id": entry["message_id"],
                "age_seconds": round(max(0.0, read_at - float(entry["posted_at"])), 1),
                "attachment_count": entry["attachment_count"],
                "has_link": entry["has_link"],
                "content_hash": entry["content_hash"],
            }
        )
    rows.sort(key=lambda row: row["age_seconds"])
    return rows
