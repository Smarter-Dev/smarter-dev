"""The incident report: one message in the moderators' channel per incident.

Rendered from the incident's records alone (no model call), so it says what
the operations say: every reason that fired, every target, and for each effect
what was asked and what Discord confirmed. The gateway edits the same message
as the incident grows.

Everything a member wrote is quoted sanitised (no live links, no mentions),
and the message is posted with every mention off except the role the
detectors asked to ping, so quoting a scam cannot re-broadcast it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from smarter_dev.web.moderation_gateway import types as t

DISCORD_MESSAGE_LIMIT = 2000
QUOTE_LIMIT = 300
NOTE_LIMIT = 400
#: A handler's or the triage's own report text can run longer than a note.
ASSESSMENT_LIMIT = 900
#: Reasons shown before the rest are counted.
REASON_LIMIT = 6
#: What :meth:`ModerationPort.find_message` looks for when a post's fate is
#: unknown; the report's last line carries it.
MARKER_PREFIX = "Incident "

_URL = re.compile(r"(?i)\b((?:https?://|www\.)\S+)")


def marker(incident_id: UUID) -> str:
    return f"{MARKER_PREFIX}{str(incident_id)[:8]}"


def shorten(text: str | None, limit: int) -> str:
    """One line of at most ``limit`` characters."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "\u2026"
    return cleaned


def sanitize(text: str | None, limit: int = QUOTE_LIMIT) -> str:
    """Member (or model) text made inert: links unlinked, mass mentions broken.

    Mentions stay readable; the report is posted with user mentions off, so
    they never ping.
    """
    if not text:
        return ""
    cleaned = _URL.sub(
        lambda match: match.group(1).replace(".", "[.]").replace("://", "[://]"), text
    )
    cleaned = cleaned.replace("@everyone", "@\u200beveryone").replace(
        "@here", "@\u200bhere"
    )
    return shorten(cleaned, limit)


def format_duration(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return "0 minutes"
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def jump_link(
    guild_id: str, channel_id: str | None, message_id: str | None
) -> str | None:
    if not channel_id or not message_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def actor_label(actor: str) -> str:
    """``handler:scam-banner`` reads as "scam-banner"; ``spam_engine`` as "spam engine"."""
    if actor.startswith("handler:"):
        return actor.split(":", 1)[1]
    if actor.startswith("manual:"):
        return f"moderator {actor.split(':', 1)[1]}"
    return actor.replace("_", " ")


@dataclass(frozen=True, slots=True)
class ReportEntry:
    kind: str
    source: str
    rule: str | None
    content: str | None
    target_user_id: str | None
    channel_id: str | None
    message_id: str | None


@dataclass(frozen=True, slots=True)
class ReportOperation:
    kind: str
    state: str
    actor: str
    target_user_id: str
    target_username: str | None
    detail: str | None
    channel_id: str | None
    message_ids: tuple[str, ...]
    deleted: tuple[str, ...]
    duration_seconds: int | None
    expires_at: float | None


@dataclass(frozen=True, slots=True)
class ReportFacts:
    incident_id: UUID
    guild_id: str
    subject_user_id: str | None
    channel_id: str | None
    trigger_message_id: str | None
    opened_at: float
    ping_role_id: str | None
    entries: tuple[ReportEntry, ...] = ()
    operations: tuple[ReportOperation, ...] = ()
    has_transcript: bool = False


_STATE_WORDS = {
    t.CONFIRMED: "confirmed",
    t.COVERED: "already covered",
    t.FAILED: "**failed**",
    t.ABSENT: "already gone",
    t.SKIPPED: "not done",
    t.UNKNOWN: "**outcome unknown**, being checked",
    t.CLAIMED: "in progress",
}


#: How an effect ranks when the report must be trimmed.
_IDLE = 0  # did nothing: skipped, already gone, already covered
_DONE = 1  # confirmed
_PINNED = 2  # failed, unknown or in progress: a moderator may have to act


def _effect_rank(op: ReportOperation) -> int:
    if op.state in (t.SKIPPED, t.ABSENT, t.COVERED):
        return _IDLE
    if op.state == t.CONFIRMED:
        return _DONE
    return _PINNED


def _effect_line(guild_id: str, op: ReportOperation) -> str:
    who = f"<@{op.target_user_id}>"
    if op.target_username and op.target_username != op.target_user_id:
        who += f" {sanitize(op.target_username, 40)}"
    if op.kind == t.TIMEOUT:
        what = f"timeout {format_duration(op.duration_seconds)}"
        if op.state == t.CONFIRMED and op.expires_at:
            what += f" (until <t:{int(op.expires_at)}:f>)"
    elif op.kind == t.UNTIMEOUT:
        what = "timeout lifted" if op.state == t.CONFIRMED else "timeout release"
    elif op.kind in t.CLEANUP_KINDS:
        asked = len(op.message_ids)
        if len(op.message_ids) == 1:
            link = jump_link(guild_id, op.channel_id, op.message_ids[0])
            what = f"delete message {link}" if link else "delete 1 message"
        else:
            what = f"delete {asked} messages in <#{op.channel_id}>"
        if op.state == t.CONFIRMED and asked > 1:
            what += f" ({len(op.deleted)} deleted)"
    else:
        what = op.kind
    state = _STATE_WORDS.get(op.state, op.state)
    line = f"- {who}: {what}: {state}"
    if op.detail and op.state not in (t.CONFIRMED,):
        line += f" ({sanitize(op.detail, 160)})"
    return f"{line} · {actor_label(op.actor)}"


def render_report(facts: ReportFacts, *, limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    """The report text, at most ``limit`` characters."""
    head: list[str] = []
    if facts.ping_role_id:
        head.append(f"<@&{facts.ping_role_id}>")
    where = f" in <#{facts.channel_id}>" if facts.channel_id else ""
    head.append(f"🛡️ **Moderation incident**{where} · <t:{int(facts.opened_at)}:f>")
    if facts.subject_user_id:
        head.append(f"**Member:** <@{facts.subject_user_id}> ({facts.subject_user_id})")
    link = jump_link(facts.guild_id, facts.channel_id, facts.trigger_message_id)
    if link:
        head.append(f"**Message:** {link}")

    reasons: list[str] = []
    seen: set[tuple[str, str]] = set()
    for entry in facts.entries:
        if entry.kind != t.ENTRY_REASON or not entry.rule:
            continue
        key = (entry.source, entry.rule)
        if key in seen:
            continue
        seen.add(key)
        reasons.append(f"- {actor_label(entry.source)}: {sanitize(entry.rule, 200)}")

    effects = [
        (_effect_rank(op), _effect_line(facts.guild_id, op)) for op in facts.operations
    ]

    quotes = [
        f"> {sanitize(entry.content)}"
        for entry in facts.entries
        if entry.kind == t.ENTRY_EVIDENCE and entry.content
    ][:2]

    # Alerts first: what failed or was left for a moderator.
    alerts = [
        f"- ⚠️ {actor_label(entry.source)}: "
        + shorten(entry.rule.removeprefix("- "), NOTE_LIMIT)
        for entry in facts.entries
        if entry.kind == t.ENTRY_ALERT and entry.rule
    ]
    notes = [
        f"- {actor_label(entry.source)}: "
        + (
            shorten(entry.rule.removeprefix("- "), NOTE_LIMIT)
            if entry.kind == t.ENTRY_NOTE
            else sanitize(entry.content, ASSESSMENT_LIMIT)
        )
        for entry in facts.entries
        if (entry.kind == t.ENTRY_NOTE and entry.rule)
        or (entry.kind == t.ENTRY_ASSESSMENT and entry.content)
    ]

    tail: list[str] = []
    if facts.has_transcript:
        tail.append("The transcript is attached.")
    if any(op.kind == t.TIMEOUT and op.state == t.CONFIRMED for op in facts.operations):
        tail.append("To undo a timeout: open the member and choose Remove Timeout.")
    if any(op.kind == t.BAN and op.state == t.CONFIRMED for op in facts.operations):
        tail.append("To undo a ban: Server Settings → Bans.")
    tail.append(f"-# {marker(facts.incident_id)} · edited as it develops")

    def render(reason_lines, effect_lines, quote_lines, note_lines) -> str:
        parts = list(head)
        if reason_lines:
            parts += ["**Why:**", *reason_lines]
        parts += ["**Actions:**", *(effect_lines or ["- none"])]
        parts += quote_lines
        if alerts or note_lines:
            parts += ["**Notes:**", *alerts, *note_lines]
        parts += tail
        return "\n".join(parts)

    def effect_lines(kept: list[int]) -> list[str]:
        lines = [effects[i][1] for i in kept]
        dropped = [effects[i][0] for i in range(len(effects)) if i not in kept]
        if dropped:
            counts = []
            done = dropped.count(_DONE)
            idle = dropped.count(_IDLE)
            if done:
                counts.append(f"{done} confirmed")
            if idle:
                counts.append(f"{idle} not needed")
            lines.append(f"- …and {len(dropped)} more ({', '.join(counts)})")
        return lines

    def reason_lines(count: int) -> list[str]:
        shown = reasons[:count]
        if len(reasons) > count:
            shown = [*shown, f"- …and {len(reasons) - count} more reason(s)"]
        return shown

    kept_reasons = min(len(reasons), REASON_LIMIT)
    kept_quotes = list(quotes)
    kept_notes = list(notes)
    kept_effects = list(range(len(effects)))

    def current() -> str:
        return render(
            reason_lines(kept_reasons),
            effect_lines(kept_effects),
            kept_quotes,
            kept_notes,
        )

    def drop_oldest(rank: int) -> bool:
        """Drop the oldest kept effect of ``rank``, never the newest effect."""
        for i in kept_effects[:-1]:
            if effects[i][0] == rank:
                kept_effects.remove(i)
                return True
        return False

    # Too long: the least important goes first. Quotes, then effects that did
    # nothing, then notes and assessments (oldest first), then confirmed
    # effects (oldest first), then reasons. Failed, unknown and in-progress
    # effects, alerts, the newest effect and the marker line always stay.
    content = current()
    while len(content) > limit and kept_quotes:
        kept_quotes.pop()
        content = current()
    while len(content) > limit and drop_oldest(_IDLE):
        content = current()
    while len(content) > limit and kept_notes:
        kept_notes.pop(0)
        content = current()
    while len(content) > limit and drop_oldest(_DONE):
        content = current()
    while len(content) > limit and kept_reasons > 1:
        kept_reasons -= 1
        content = current()
    if len(content) > limit:
        # Whatever is left over comes out of the middle, never the marker.
        ending = "\n".join(tail)
        content = content[: limit - len(ending) - 2] + "…\n" + ending
    return content
