"""The incident report: one message in the moderators' channel per incident.

Rendered from the incident's records alone (no model call), so it says what
the operations say. Since #333 it reads as a timeline: one line per thing
that happened, in time order and stamped, then a one-line summary (what it
was, what was done, whether the member was told, their history). Effects that
did nothing (already covered, already gone, a duplicate) are left out; a
reason an action's line already gives is not repeated. The gateway edits the
same message as the incident grows.

Everything a member wrote is quoted sanitised (no live links, no mentions),
and the message is posted with every mention off except the role the
detectors asked to ping, so quoting a scam cannot re-broadcast it.

What a deleted message was (#378) is a model's one line, written before the
delete and recorded as a ``deleted_summary`` entry: it follows the delete's
line in the timeline and is repeated in a **Deleted:** line under the
summary. Copies of one post share one line ("×3 in #a, #b"). The line is
sanitised like member text.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from smarter_dev.web.moderation_gateway import types as t

DISCORD_MESSAGE_LIMIT = 2000
QUOTE_LIMIT = 300
NOTE_LIMIT = 400
#: A handler's or the triage's own report text can run longer than a note.
ASSESSMENT_LIMIT = 900
#: Reasons shown before the rest are counted.
REASON_LIMIT = 6
#: A deleted message's description, and the **Deleted:** line they make.
DELETED_LIMIT = 240
DELETED_LINE_LIMIT = 600
#: Posts the **Deleted:** line names before the rest are counted.
DELETED_POSTS = 3
SUMMARY_UNAVAILABLE = "summary unavailable"
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
    #: When it was written (epoch seconds); None renders without a stamp.
    at: float | None = None
    details: dict[str, Any] | None = None


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
    at: float | None = None
    reason: str | None = None
    #: A review hold (``requested.provisional``), not a sanction.
    provisional: bool = False


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

#: Effects that did nothing: no line at all (#333).
_IDLE_STATES = (t.COVERED, t.ABSENT, t.SKIPPED)

#: Skips a moderator still needs to see (#342), by how the gateway's detail
#: begins, with the summary's words. Anything else skipped (a duplicate, a
#: weaker sanction, nothing to lift) did nothing worth a line.
_PROTECTED = "protected"
_SHOWN_SKIPS = (
    (_PROTECTED, "refused on a protected member"),
    ("a moderator reversed it", "stood down after a moderator reversed it"),
    ("a timeout someone else set", "left a timeout someone else set"),
    ("the timeout in place is someone else's", "left a timeout someone else set"),
    ("not a member", "not done: not a member"),
    ("already banned", "already banned"),
)


def _shown_skip(op: ReportOperation) -> tuple[str, str] | None:
    """``(prefix, summary words)`` for a skip the report shows, else None."""
    if op.state != t.SKIPPED:
        return None
    detail = op.detail or ""
    return next((item for item in _SHOWN_SKIPS if detail.startswith(item[0])), None)


def _idle(op: ReportOperation) -> bool:
    return op.state in _IDLE_STATES and _shown_skip(op) is None


#: How an effect ranks when the report must be trimmed.
_DONE = 1  # confirmed, or skipped for a reason worth showing
_PINNED = 2  # failed, unknown, in progress, or refused on a protected member

#: What a dropped line is counted as.
_TAG_DONE = "confirmed"
_TAG_SKIPPED = "not done"
_TAG_PINNED = "failed or unresolved"
_TAG_REFUSED = "refused (protected)"
_SANCTIONS = (t.BAN, t.KICK, t.TIMEOUT, t.WARN)


def _who(op: ReportOperation) -> str:
    who = f"<@{op.target_user_id}>"
    if op.target_username and op.target_username != op.target_user_id:
        who += f" {sanitize(op.target_username, 40)}"
    return who


def _effect_words(guild_id: str, op: ReportOperation) -> str:
    """What was asked, for an effect that did not (yet) land."""
    if op.kind == t.TIMEOUT:
        return ("hold " if op.provisional else "timeout ") + format_duration(
            op.duration_seconds
        )
    if op.kind == t.UNTIMEOUT:
        return "timeout release"
    if op.kind in t.CLEANUP_KINDS:
        if len(op.message_ids) == 1:
            link = jump_link(guild_id, op.channel_id, op.message_ids[0])
            return f"delete message {link}" if link else "delete 1 message"
        return f"delete {len(op.message_ids)} messages in <#{op.channel_id}>"
    return op.kind


def _done_words(guild_id: str, op: ReportOperation, held: set[str]) -> str:
    """What happened, for a confirmed effect. ``held``: targets whose review
    hold is still on, so a longer timeout upgrades it and a lift ends it."""
    who = _who(op)
    until = f" (until <t:{int(op.expires_at)}:f>)" if op.expires_at else ""
    duration = _span(op.duration_seconds)
    if op.kind == t.TIMEOUT:
        if op.provisional:
            held.add(op.target_user_id)
            return f"Held {who} for {duration}"
        if op.target_user_id in held:
            held.discard(op.target_user_id)
            adjective = _span_adjective(op.duration_seconds)
            return f"Hold on {who} upgraded to a {adjective} timeout{until}"
        return f"Timed out {who} for {duration}{until}"
    if op.kind == t.UNTIMEOUT:
        if op.target_user_id in held:
            held.discard(op.target_user_id)
            return f"Hold on {who} lifted"
        return f"Timeout on {who} lifted"
    if op.kind in t.CLEANUP_KINDS:
        asked = len(op.message_ids)
        if asked == 1:
            link = jump_link(guild_id, op.channel_id, op.message_ids[0])
            return f"Deleted a message from {who}" + (f": {link}" if link else "")
        done = len(op.deleted)
        count = f"{done} of {asked}" if done != asked else str(asked)
        return f"Deleted {count} messages from {who} in <#{op.channel_id}>"
    if op.kind == t.BAN:
        return f"Banned {who}"
    if op.kind == t.KICK:
        return f"Kicked {who}"
    if op.kind == t.WARN:
        return f"Warned {who}"
    return f"{who}: {op.kind}"


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def _span(seconds: int | None) -> str:
    """A duration as people say it: up to two days in hours ("24 hours")."""
    if seconds and seconds % 3600 == 0 and seconds <= 2 * 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return format_duration(seconds)


def _span_adjective(seconds: int | None) -> str:
    """``_span`` before a noun: "a 24-hour timeout", "a 7-day timeout"."""
    count, unit = _span(seconds).split(" ", 1)
    return f"{count}-{unit.removesuffix('s')}"


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


@dataclass(slots=True)
class _Line:
    at: float | None
    order: int
    text: str
    source: str
    rank: int = _DONE
    tag: str = _TAG_DONE


@dataclass(slots=True)
class _Deleted:
    """One post's deleted messages and their one description."""

    text: str | None
    message_ids: list[str]
    channel_ids: list[str]
    shown: bool = False

    def words(self) -> str:
        text = sanitize(self.text, DELETED_LIMIT) if self.text else SUMMARY_UNAVAILABLE
        count = len(self.message_ids)
        if count > 1:
            where = ", ".join(f"<#{channel}>" for channel in self.channel_ids)
            text += f" (\u00d7{count}" + (f" in {where}" if where else "") + ")"
        return text


def deleted_posts(facts: ReportFacts) -> list[_Deleted]:
    """The deletion summaries, one per post: the entries of its copies (the
    same ``details.key``) merged, the first description that was written
    standing for all of them."""
    posts: dict[str, _Deleted] = {}
    for index, entry in enumerate(facts.entries):
        if entry.kind != t.ENTRY_DELETED_SUMMARY:
            continue
        details = entry.details or {}
        key = str(details.get("key") or f"entry-{index}")
        text = entry.content if details.get("status") == "ok" else None
        post = posts.setdefault(key, _Deleted(None, [], []))
        if post.text is None and text:
            post.text = text
        post.message_ids = list(
            dict.fromkeys(
                [*post.message_ids, *map(str, details.get("message_ids") or ())]
            )
        )
        post.channel_ids = list(
            dict.fromkeys(
                [*post.channel_ids, *map(str, details.get("channel_ids") or ())]
            )
        )
    return list(posts.values())


def _deleted_line(posts: list[_Deleted]) -> str | None:
    """The summary's **Deleted:** line: what each post was."""
    if not posts:
        return None
    named = [post.words() for post in posts[:DELETED_POSTS]]
    more = len(posts) - len(named)
    text = "; ".join(named) + (f"; and {more} more" if more > 0 else "")
    return shorten(f"**Deleted:** {text}", DELETED_LINE_LIMIT)


def _lead(facts: ReportFacts, reasons: list[str]) -> str:
    """The summary's opening: the strongest ``summary`` entry (newest among
    equals), so a later unknown verdict never overwrites a confirmed one;
    without one, the first reason."""
    leads = [
        (int((entry.details or {}).get("priority") or 0), index, entry.rule)
        for index, entry in enumerate(facts.entries)
        if entry.kind == t.ENTRY_SUMMARY and entry.rule
    ]
    if leads:
        return max(leads)[2]
    return reasons[0] if reasons else "Recorded"


def _summary(
    facts: ReportFacts, reasons: list[str], pinned: int, held: set[str]
) -> str:
    """One line: what it was, what was done, whether the member was told,
    and their history."""
    lead = _lead(facts, reasons)
    confirmed = [op for op in facts.operations if op.state == t.CONFIRMED]
    parts: list[str] = []
    removed = {
        message_id
        for op in confirmed
        if op.kind in t.CLEANUP_KINDS
        for message_id in (op.deleted or op.message_ids)
    }
    if removed:
        parts.append(f"{_plural(len(removed), 'message')} removed")
    for kind, verb in (
        (t.BAN, "banned"),
        (t.KICK, "kicked"),
        (t.TIMEOUT, "timed out"),
        (t.WARN, "warned"),
    ):
        ops = [op for op in confirmed if op.kind == kind and not op.provisional]
        targets = {op.target_user_id for op in ops}
        if not targets:
            continue
        if len(targets) > 1:
            parts.append(f"{len(targets)} members {verb}")
        elif kind == t.TIMEOUT:
            longest = max(op.duration_seconds or 0 for op in ops)
            parts.append(f"{verb} for {_span(longest)}")
        else:
            parts.append(verb)
    holds = [op for op in confirmed if op.kind == t.TIMEOUT and op.provisional]
    replaced = any(
        op.kind in (t.BAN, t.KICK) or (op.kind == t.TIMEOUT and not op.provisional)
        for op in confirmed
    )
    if holds and not replaced:
        if held:
            parts.append(f"held for {_span(holds[0].duration_seconds)}")
        else:
            parts.append("hold lifted")
    skipped: dict[str, set[str]] = {}
    for op in facts.operations:
        shown = _shown_skip(op)
        if shown is not None:
            skipped.setdefault(shown[1], set()).add(op.target_user_id)
    for prefix, words in dict.fromkeys((item[0], item[1]) for item in _SHOWN_SKIPS):
        targets = skipped.pop(words, None)
        if not targets:
            continue
        if prefix == _PROTECTED:
            words = (
                f"refused on {len(targets)} protected members"
                if len(targets) > 1
                else words
            )
            parts.append(f"**{words}**")
        else:
            parts.append(words)
    # Every DM, not the last per member (#342): "told" only if all arrived.
    delivered = [
        bool((entry.details or {}).get("delivered"))
        for entry in facts.entries
        if entry.kind == t.ENTRY_DELIVERY
    ]
    if delivered:
        missed = delivered.count(False)
        if not missed:
            parts.append("told by DM")
        elif missed == len(delivered) == 1:
            parts.append("**DM not delivered**")
        else:
            parts.append(f"**{missed} of {len(delivered)} DMs not delivered**")
    if pinned:
        parts.append(f"**{pinned} failed or unresolved**")
    history = next(
        (entry for entry in reversed(facts.entries) if entry.kind == t.ENTRY_HISTORY),
        None,
    )
    text = f"**Summary:** {shorten(_capital(lead), 200)}"
    text += f": {', '.join(parts)}." if parts else "."
    if history is not None:
        count = int((history.details or {}).get("count") or 0)
        text += (
            " No earlier actions."
            if count == 0
            else f" {_plural(count, 'earlier action')} on record."
        )
    return text


_MARKDOWN = re.compile(r"([\\*_~|`])")
NAME_LIMIT = 40
UNKNOWN_NAME = "username unknown"


def _plain(text: str) -> str:
    """A name shown as typed: markdown escaped, links and mass mentions made
    inert, one line."""
    return _MARKDOWN.sub(r"\\\1", sanitize(" ".join(text.split()), NAME_LIMIT))


def member_name(facts: ReportFacts) -> str:
    """The subject's username (and display name) in plain text, as recorded
    with the incident (#376): a mention alone reads "unknown-user" on a
    client that has not cached the member, which leaves nothing to search
    for. Falls back to the username an operation recorded, and says so when
    none is known."""
    subject = facts.subject_user_id
    named = next(
        (
            entry.details or {}
            for entry in facts.entries
            if entry.kind == t.ENTRY_MEMBER and entry.target_user_id == subject
        ),
        {},
    )
    username = named.get("username") or next(
        (
            op.target_username
            for op in facts.operations
            if op.target_user_id == subject
            and op.target_username
            and op.target_username != subject
        ),
        None,
    )
    if not username:
        return UNKNOWN_NAME
    display = named.get("display_name")
    shown = _plain(username)
    if display and display != username:
        shown += f" ({_plain(display)})"
    return shown


def render_report(facts: ReportFacts, *, limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    """The report text, at most ``limit`` characters."""
    head: list[str] = []
    if facts.ping_role_id:
        head.append(f"<@&{facts.ping_role_id}>")
    where = f" in <#{facts.channel_id}>" if facts.channel_id else ""
    head.append(f"🛡️ **Moderation incident**{where} · <t:{int(facts.opened_at)}:f>")
    if facts.subject_user_id:
        head.append(
            f"**Member:** <@{facts.subject_user_id}> · "
            f"{member_name(facts)} · {facts.subject_user_id}"
        )
    link = jump_link(facts.guild_id, facts.channel_id, facts.trigger_message_id)
    if link:
        head.append(f"**Message:** {link}")

    sources = {actor_label(op.actor) for op in facts.operations} | {
        actor_label(entry.source)
        for entry in facts.entries
        if entry.kind != t.ENTRY_EVIDENCE
    }
    # Who did it, only when more than one system (or a moderator) took part.
    by = len(sources) > 1

    def stamp(at: float | None, text: str, source: str, *, dated: bool) -> str:
        # The date is given on the first line and wherever it changes, so a
        # time is never ambiguous across midnight (#342).
        when = f"<t:{int(at)}:{'f' if dated else 'T'}> " if at is not None else ""
        suffix = f" · {actor_label(source)}" if by else ""
        return f"- {when}{text}{suffix}"

    order = 0
    effects: list[_Line] = []
    given: list[str] = []
    held: set[str] = set()
    posts = deleted_posts(facts)
    for op in facts.operations:
        if _idle(op):
            continue
        order += 1
        if op.state == t.SKIPPED:
            text = f"{_who(op)}: {_effect_words(facts.guild_id, op)}: not done"
            text += f" ({sanitize(op.detail, 160)})"
            # A staff refusal ranks with failures: trimming keeps it (#342).
            refused = (op.detail or "").startswith(_PROTECTED)
            rank = _PINNED if refused else _DONE
            tag = _TAG_REFUSED if refused else _TAG_SKIPPED
        elif op.state == t.CONFIRMED:
            text = _done_words(facts.guild_id, op, held)
            if op.kind in _SANCTIONS and op.reason:
                text += f": {sanitize(op.reason, 200)}"
                given.append(_norm(op.reason))
            if op.kind in t.CLEANUP_KINDS:
                # What it deleted, on the first delete of each post (#378).
                removed = set(op.deleted or op.message_ids)
                for post in posts:
                    if not post.shown and removed & set(post.message_ids):
                        post.shown = True
                        text += f" \u2014 {post.words()}"
            rank, tag = _DONE, _TAG_DONE
        else:
            text = f"{_who(op)}: {_effect_words(facts.guild_id, op)}: "
            text += _STATE_WORDS.get(op.state, op.state)
            if op.detail:
                text += f" ({sanitize(op.detail, 160)})"
            rank, tag = _PINNED, _TAG_PINNED
        effects.append(_Line(op.at, order, text, op.actor, rank, tag))

    reasons: list[_Line] = []
    reason_texts: list[str] = []
    seen: set[tuple[str, str]] = set()
    for entry in facts.entries:
        if entry.kind != t.ENTRY_REASON or not entry.rule:
            continue
        key = (entry.source, _norm(entry.rule))
        if key in seen:
            continue
        seen.add(key)
        reason_texts.append(entry.rule)
        if _norm(entry.rule) in given:
            continue
        given.append(_norm(entry.rule))
        order += 1
        reasons.append(
            _Line(entry.at, order, _capital(sanitize(entry.rule, 200)), entry.source)
        )

    quotes = [
        f"> {sanitize(entry.content)}"
        for entry in facts.entries
        if entry.kind == t.ENTRY_EVIDENCE and entry.content
    ][:2]

    alerts: list[_Line] = []
    notes: list[_Line] = []
    deliveries: list[_Line] = []
    noted: set[str] = set()
    for entry in facts.entries:
        if entry.kind == t.ENTRY_ALERT and entry.rule:
            text = "⚠️ " + shorten(entry.rule.removeprefix("- "), NOTE_LIMIT)
            bucket = alerts
        elif entry.kind == t.ENTRY_NOTE and entry.rule:
            text = shorten(entry.rule.removeprefix("- "), NOTE_LIMIT)
            bucket = notes
        elif entry.kind == t.ENTRY_ASSESSMENT and entry.content:
            text = sanitize(entry.content, ASSESSMENT_LIMIT)
            bucket = notes
        elif entry.kind == t.ENTRY_DELIVERY and entry.rule:
            text = shorten(entry.rule, NOTE_LIMIT)
            bucket = deliveries
        else:
            continue
        if text in noted:
            continue
        noted.add(text)
        order += 1
        bucket.append(_Line(entry.at, order, text, entry.source))

    history_entry = next(
        (
            entry
            for entry in reversed(facts.entries)
            if entry.kind == t.ENTRY_HISTORY and entry.rule
        ),
        None,
    )
    history = (
        [shorten(history_entry.rule, NOTE_LIMIT)]
        if history_entry is not None
        and int((history_entry.details or {}).get("count") or 0) > 0
        else []
    )
    summary = _summary(
        facts,
        reason_texts,
        sum(1 for line in effects if line.tag == _TAG_PINNED),
        held,
    )
    deleted_line = _deleted_line(posts)
    if deleted_line is not None:
        summary += "\n" + deleted_line

    tail: list[str] = []
    if facts.has_transcript:
        tail.append("The transcript is attached.")
    if any(
        op.kind == t.TIMEOUT and op.state == t.CONFIRMED and not op.provisional
        for op in facts.operations
    ):
        tail.append("To undo a timeout: open the member and choose Remove Timeout.")
    if any(op.kind == t.BAN and op.state == t.CONFIRMED for op in facts.operations):
        tail.append("To undo a ban: Server Settings → Bans.")
    tail.append(f"-# {marker(facts.incident_id)} · edited as it develops")

    kept_quotes = list(quotes)
    kept_history = list(history)
    kept_notes = list(notes)
    kept_deliveries = list(deliveries)
    kept_effects = list(range(len(effects)))
    kept_reasons = len(reasons)
    kept_alerts = len(alerts)

    def counters() -> list[str]:
        lines = []
        dropped = [effects[i].tag for i in range(len(effects)) if i not in kept_effects]
        if dropped:
            counts = []
            for tag in (_TAG_REFUSED, _TAG_PINNED, _TAG_DONE, _TAG_SKIPPED):
                count = dropped.count(tag)
                if not count:
                    continue
                pinned = tag in (_TAG_REFUSED, _TAG_PINNED)
                counts.append(f"**{count} {tag}**" if pinned else f"{count} {tag}")
            lines.append(f"- …and {len(dropped)} more ({', '.join(counts)})")
        if len(reasons) > kept_reasons:
            lines.append(f"- …and {len(reasons) - kept_reasons} more reason(s)")
        if len(alerts) > kept_alerts:
            lines.append(f"- ⚠️ …and {len(alerts) - kept_alerts} earlier alert(s)")
        return lines

    def current() -> str:
        timeline = [
            *(effects[i] for i in kept_effects),
            *reasons[:kept_reasons],
            *(alerts[len(alerts) - kept_alerts :] if kept_alerts else []),
            *kept_notes,
            *kept_deliveries,
        ]
        timeline.sort(
            key=lambda line: (
                line.at if line.at is not None else float("-inf"),
                line.order,
            )
        )
        rendered: list[str] = []
        day: str | None = None
        for line in timeline:
            today = (
                time.strftime("%Y-%m-%d", time.gmtime(line.at))
                if line.at is not None
                else None
            )
            dated = today is not None and today != day
            if today is not None:
                day = today
            rendered.append(stamp(line.at, line.text, line.source, dated=dated))
        parts = [
            *head,
            *kept_quotes,
            *counters(),
            *rendered,
            *kept_history,
            summary,
            *tail,
        ]
        return "\n".join(parts)

    def drop_oldest(rank: int, *, keep_newest_of_rank: bool = False) -> bool:
        """Drop the oldest kept effect of ``rank``, never the newest effect."""
        candidates = [i for i in kept_effects[:-1] if effects[i].rank == rank]
        if keep_newest_of_rank:
            ranked = [i for i in kept_effects if effects[i].rank == rank]
            candidates = [i for i in candidates if i != ranked[-1]]
        if not candidates:
            return False
        kept_effects.remove(candidates[0])
        return True

    # Too long: the least important goes first. Quotes, then the history
    # line, notes and assessments (oldest first), DM lines, then confirmed
    # effects (oldest first), then reasons. Failed, unknown and in-progress
    # effects, refusals on protected members (#342) and alerts go only when
    # they alone overflow, oldest first and
    # counted: failures down to the newest, alerts down to the newest, then
    # that last failure. The newest effect, the summary and the marker line
    # always stay.
    content = current()
    while len(content) > limit and kept_quotes:
        kept_quotes.pop()
        content = current()
    while len(content) > limit and kept_history:
        kept_history.pop()
        content = current()
    while len(content) > limit and kept_notes:
        kept_notes.pop(0)
        content = current()
    while len(content) > limit and kept_deliveries:
        kept_deliveries.pop(0)
        content = current()
    while len(content) > limit and drop_oldest(_DONE):
        content = current()
    while len(content) > limit and kept_reasons > 0:
        kept_reasons -= 1
        content = current()
    while len(content) > limit and drop_oldest(_PINNED, keep_newest_of_rank=True):
        content = current()
    while len(content) > limit and kept_alerts > 1:
        kept_alerts -= 1
        content = current()
    while len(content) > limit and drop_oldest(_PINNED):
        content = current()
    if len(content) > limit:
        # Whatever is left over comes out of the middle, never the marker.
        ending = "\n".join(tail)
        content = content[: limit - len(ending) - 2] + "…\n" + ending
    return content
