"""The chat bot's short-term memory: a rolling 60-minute, guild-wide event log.

This is the layer that lets the bot say "yeah, I timed out mallory earlier" and
be telling the truth. It records what *the bot's own account* did in a guild —
moderation with reasons, announcements, DMs — so the chat agent can own those
actions in conversation instead of denying them or, worse, inventing them.

Storage is one Redis ZSET per guild, ``chat_agent:guild:{guild_id}:events``:
member is a compact JSON payload carrying an 8-hex id minted at write time,
score is the event's epoch second. Every write trims the set back to the last
hour and the newest :data:`MAX_EVENTS_PER_GUILD` entries and refreshes the key's
one-hour TTL, so an idle guild's log simply evaporates. A ZSET rather than a
Stream because window-trim is the primary operation and consumer groups are
actively wrong here — every channel engine must see every event, not a share of
them. The write pipeline is the house idiom from ``user_message_limit.py``.

This module is the ONLY package both tiers may import, so the Redis handle is
injected rather than fetched: the bot's handle is ``decode_responses=False`` and
the worker/web handle is ``True``. Everything read back out of Redis is decoded
defensively for that reason, and a member that doesn't decode or doesn't parse
is skipped rather than raised — one poisoned member must never cost the bot its
whole hour of memory.

What counts as an event — "the bot's account did it"
----------------------------------------------------
Included: slash-command moderation (``source="manual"`` — the bot executes it
for a mod, and the renderer attributes it), every ``ai``/``handler``/automod
action, handler messages and DMs, and scheduled/repeating/challenge/quest
announcements.

Excluded, deliberately:

- ``source="audit_log"`` actions — a human acted with their own account, and
  rendering that as "I banned…" would be a false public claim. Filtered at the
  capture site, since only it knows the provenance.
- The chat agent's own replies and its ``_post_status`` lines — they're already
  in the conversation history; re-feeding them is noise.
- Per-action notice echoes (content-filter, spam, mod-monitor, timeout and warn
  notices) — the underlying action is already logged, so these would double
  every moderation event.
- Audit-logger embeds and social sends (squads, bytes, rules, forum, streaks,
  Advent of Code) — high volume, no personality value.

Reads return ``(events, cursor)``. A channel engine takes the full window at
activation and then only what's strictly newer than its cursor, which is why the
cursor is a score rather than a count: it survives trimming and rank capping.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

__all__ = [
    "CHAT_MEMORY_ENABLED_ENV",
    "EVENT_WINDOW_SECONDS",
    "MAX_EVENTS_PER_GUILD",
    "MAX_REASON_CHARS",
    "MAX_SUMMARY_CHARS",
    "GuildEvent",
    "append_event",
    "bot_message_event",
    "chat_memory_enabled",
    "guild_events_key",
    "mod_action_event",
    "read_since",
    "read_window",
]

KEY_PREFIX = "chat_agent"
EVENT_WINDOW_SECONDS = 3600
MAX_EVENTS_PER_GUILD = 200
MAX_REASON_CHARS = 200
MAX_SUMMARY_CHARS = 200

CHAT_MEMORY_ENABLED_ENV = "CHAT_MEMORY_ENABLED"
_DISABLED_FLAG_VALUES = frozenset({"0", "false", "no", "off"})

MOD_ACTION_KIND = "mod_action"
BOT_MESSAGE_KIND = "bot_message"
BOT_DM_KIND = "bot_dm"


def guild_events_key(guild_id: str) -> str:
    """Redis key holding one guild's rolling hour of bot actions."""
    return f"{KEY_PREFIX}:guild:{guild_id}:events"


def chat_memory_enabled() -> bool:
    """Whether the global chat-memory kill switch is on (default: on).

    Read from the environment on EVERY call rather than cached at import, so
    flipping the deployment variable takes effect without a restart. The switch
    is checked in exactly two chokepoints — :func:`append_event` here, and the
    bot's memory service — so no capture site can forget it.
    """
    raw_flag = os.environ.get(CHAT_MEMORY_ENABLED_ENV, "").strip().lower()
    return raw_flag not in _DISABLED_FLAG_VALUES


@dataclass(frozen=True)
class GuildEvent:
    """One thing the bot's account did in a guild, as the bot would recall it.

    ``summary`` is the prose the renderer leans on for message-shaped events
    (announcements, DMs); moderation events leave it empty and are rendered from
    the structured fields instead, so the renderer can put the reason after the
    colon and the duration inline. ``at`` must be timezone-aware — it becomes the
    ZSET score and the reader's cursor, and a naive value would silently shift
    the whole window by the host's UTC offset.
    """

    kind: str
    guild_id: str
    at: datetime
    summary: str = ""
    channel_id: str | None = None
    channel_name: str | None = None
    action: str | None = None
    target_username: str | None = None
    moderator_username: str | None = None
    reason: str | None = None
    duration_seconds: int | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("GuildEvent.at must be timezone-aware")


def _truncated(text: str, limit: int) -> str:
    """``text`` clamped to ``limit`` characters, ellipsised when it was cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _optional_text(value: Any, limit: int) -> str | None:
    """A truncated string for a present, non-empty value; ``None`` otherwise."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _truncated(text, limit)


def _parsed_timestamp(raw_value: Any) -> datetime | None:
    """An aware datetime for an ISO-8601 string, or ``None`` if it isn't one."""
    if not isinstance(raw_value, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def mod_action_event(
    context: dict[str, Any],
    *,
    guild_id: str,
    channel_name: str | None = None,
    at: datetime | None = None,
) -> GuildEvent:
    """Build the event for a moderation action from its ``mod_action`` context.

    ``context`` is the trigger context built by
    :func:`smarter_dev.web.handler_dispatch.build_mod_action_context` — the same
    dict the handler fan-out receives — so a capture site that already has one
    spends nothing extra. It is read, never modified.

    The caller supplies ``guild_id`` (the context is guild-scoped by the
    dispatch, not by the payload) and, where it's cheap to know,
    ``channel_name`` so the renderer can say "#general" without a Discord round
    trip. The timestamp comes from ``at`` if given, else the context's
    ``created_at``, else now. Actions ingested from the audit log must be
    filtered out BEFORE this call: only the capture site knows the provenance,
    and claiming a human's action as the bot's own is the one unrecoverable lie.
    """
    duration = context.get("duration_seconds")
    return GuildEvent(
        kind=MOD_ACTION_KIND,
        guild_id=guild_id,
        at=at or _parsed_timestamp(context.get("created_at")) or datetime.now(UTC),
        channel_id=_optional_text(context.get("channel_id"), MAX_SUMMARY_CHARS),
        channel_name=_optional_text(channel_name, MAX_SUMMARY_CHARS),
        action=_optional_text(context.get("action_type"), MAX_SUMMARY_CHARS),
        target_username=_optional_text(context.get("target_username"), MAX_SUMMARY_CHARS),
        moderator_username=_optional_text(
            context.get("moderator_username"), MAX_SUMMARY_CHARS
        ),
        reason=_optional_text(context.get("reason"), MAX_REASON_CHARS),
        duration_seconds=int(duration) if isinstance(duration, int | float) else None,
        source=_optional_text(context.get("source"), MAX_SUMMARY_CHARS),
    )


def bot_message_event(
    *,
    guild_id: str,
    summary: str,
    kind: str = BOT_MESSAGE_KIND,
    channel_id: str | None = None,
    channel_name: str | None = None,
    target_username: str | None = None,
    source: str | None = None,
    at: datetime | None = None,
) -> GuildEvent:
    """Build the event for something the bot's account said.

    ``summary`` is the *purpose* of the message, never its body — "the weekly
    challenge announcement", "the timeout notice". That's the register rule of
    the whole memory system (remember the person, not the transcript) and, for
    DMs, the privacy rule: pass ``kind=`` :data:`BOT_DM_KIND` with the recipient
    in ``target_username`` and the reason for writing in ``summary``.
    """
    return GuildEvent(
        kind=kind,
        guild_id=guild_id,
        at=at or datetime.now(UTC),
        summary=_truncated(summary.strip(), MAX_SUMMARY_CHARS),
        channel_id=_optional_text(channel_id, MAX_SUMMARY_CHARS),
        channel_name=_optional_text(channel_name, MAX_SUMMARY_CHARS),
        target_username=_optional_text(target_username, MAX_SUMMARY_CHARS),
        source=_optional_text(source, MAX_SUMMARY_CHARS),
    )


def _serialized(event: GuildEvent) -> str:
    """``event`` as the compact JSON member stored in the ZSET.

    Empty fields are dropped to keep the member small, and an 8-hex id is minted
    per write: ZADD is set semantics, so without it two identical actions in the
    same second would collapse into one remembered event.
    """
    payload = {
        name: value
        for name, value in asdict(event).items()
        if value is not None and value != "" and name != "at"
    }
    payload["at"] = event.at.timestamp()
    payload["event_id"] = uuid.uuid4().hex[:8]
    return json.dumps(payload, separators=(",", ":"))


def _deserialized(member: bytes | str) -> GuildEvent | None:
    """A :class:`GuildEvent` for one ZSET member, or ``None`` if it's unusable.

    Handles both handle flavours (``decode_responses`` True gives ``str``, False
    gives ``bytes``) and treats every malformed shape as a skip: a member written
    by an older or newer build must cost the bot one memory, not the hour.
    """
    try:
        text = member.decode("utf-8") if isinstance(member, bytes) else member
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return None
        return GuildEvent(
            kind=str(payload["kind"]),
            guild_id=str(payload["guild_id"]),
            at=datetime.fromtimestamp(float(payload["at"]), UTC),
            summary=str(payload.get("summary", "")),
            channel_id=_nullable_string(payload.get("channel_id")),
            channel_name=_nullable_string(payload.get("channel_name")),
            action=_nullable_string(payload.get("action")),
            target_username=_nullable_string(payload.get("target_username")),
            moderator_username=_nullable_string(payload.get("moderator_username")),
            reason=_nullable_string(payload.get("reason")),
            duration_seconds=(
                int(payload["duration_seconds"])
                if payload.get("duration_seconds") is not None
                else None
            ),
            source=_nullable_string(payload.get("source")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.debug("skipping malformed guild event member", exc_info=True)
        return None


def _nullable_string(value: Any) -> str | None:
    """``str(value)`` unless the field was absent."""
    return None if value is None else str(value)


async def append_event(
    redis: Redis, event: GuildEvent, *, now: datetime | None = None
) -> bool:
    """Record ``event`` in its guild's log, returning whether it was written.

    One transactional pipeline does the whole maintenance sweep: add the member,
    drop everything that fell out of the hour, drop everything past the newest
    :data:`MAX_EVENTS_PER_GUILD` (a raid can generate hundreds of actions a
    minute — the cap is what stops one guild's log from growing without bound),
    and push the key's TTL back out to a full window.

    Returns ``False`` without touching Redis when the global kill switch is off.
    Checking it HERE rather than at each capture site is the point: fifteen sites
    can't all remember, and one that forgets keeps writing memory a deployment
    asked to stop keeping.
    """
    if not chat_memory_enabled():
        return False
    key = guild_events_key(event.guild_id)
    current_epoch = (now or datetime.now(UTC)).timestamp()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zadd(key, {_serialized(event): event.at.timestamp()})
        pipe.zremrangebyscore(key, "-inf", f"({current_epoch - EVENT_WINDOW_SECONDS}")
        pipe.zremrangebyrank(key, 0, -(MAX_EVENTS_PER_GUILD + 1))
        pipe.expire(key, EVENT_WINDOW_SECONDS)
        await pipe.execute()
    return True


def _events_with_cursor(
    scored_members: list[tuple[bytes | str, float]], fallback_cursor: float
) -> tuple[list[GuildEvent], float]:
    """Decode a scored range oldest-first, paired with the cursor to read from next.

    The cursor is the highest score SEEN, including a member that failed to
    decode: a poisoned event still has to advance the cursor past itself, or
    every later read would keep re-fetching it.
    """
    events = [
        event for member, _ in scored_members if (event := _deserialized(member))
    ]
    highest_score = max((score for _, score in scored_members), default=None)
    return events, fallback_cursor if highest_score is None else highest_score


async def read_window(
    redis: Redis, guild_id: str, *, now: datetime | None = None
) -> tuple[list[GuildEvent], float]:
    """The last hour of ``guild_id``'s events, oldest-first, plus a fresh cursor.

    This is the activation read: a channel engine waking up wants the whole
    window, then only what happens next. The returned cursor is the newest score
    in the window, or — for a guild that has done nothing — ``now``, so the first
    follow-up doesn't re-deliver an event that landed before the engine existed.
    """
    current_epoch = (now or datetime.now(UTC)).timestamp()
    scored_members = await redis.zrangebyscore(
        guild_events_key(guild_id),
        current_epoch - EVENT_WINDOW_SECONDS,
        "+inf",
        withscores=True,
    )
    return _events_with_cursor(scored_members, current_epoch)


async def read_since(
    redis: Redis, guild_id: str, cursor: float
) -> tuple[list[GuildEvent], float]:
    """Events strictly newer than ``cursor``, oldest-first, plus the next cursor.

    The follow-up read. The bound is exclusive so a turn never shows the agent an
    action it already narrated; the trade is that a second event landing in the
    same second as the cursor is not delivered, which costs at most one line of
    context and is far cheaper than the bot claiming the same timeout twice. When
    nothing is new the cursor comes back unchanged.
    """
    scored_members = await redis.zrangebyscore(
        guild_events_key(guild_id), f"({cursor}", "+inf", withscores=True
    )
    return _events_with_cursor(scored_members, cursor)
