"""What callers hand the gateway, and what it hands back.

A detector describes what fired (:class:`Trigger`) and what it wants done
(:class:`Proposal`); the gateway answers with one :class:`Outcome` per
proposal. Nothing here touches Discord or the database.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

# Effects. A sanction punishes a member; a cleanup removes messages.
BAN = "ban"
KICK = "kick"
TIMEOUT = "timeout"
UNTIMEOUT = "untimeout"
WARN = "warn"
DELETE = "delete"
PURGE = "purge"

#: Strongest wins per target within an incident. A delete is cleanup, not a
#: rung: a ban does not remove the offending message, so a stronger sanction
#: never blocks one (each message is deleted at most once instead).
SANCTION_RANK = {WARN: 1, TIMEOUT: 2, KICK: 3, BAN: 4}
CLEANUP_KINDS = frozenset({DELETE, PURGE})

# Operation states.
CLAIMED = "claimed"  # written and claimed; the REST call is about to go out
CONFIRMED = "confirmed"  # Discord confirmed it; a moderation_actions row says so
COVERED = "covered"  # a timeout claim already met by a longer gateway timeout
FAILED = "failed"  # Discord refused it, or reconciliation found it never landed
ABSENT = "absent"  # a delete found every message already gone
SKIPPED = "skipped"  # never sent: protected, weaker, duplicate, overridden
UNKNOWN = "unknown"  # may have landed; reconciled before anything is resent

SETTLED_STATES = frozenset({CONFIRMED, COVERED, FAILED, ABSENT, SKIPPED})
#: States that mean the effect is (or may be) in place.
LIVE_STATES = frozenset({CLAIMED, CONFIRMED, COVERED, UNKNOWN})

# ``moderation_actions.source`` values the gateway writes.
SOURCE_AI = "ai"
SOURCE_MANUAL = "manual"
SOURCE_HANDLER = "handler"

# Incident entry kinds (see ModerationIncidentEntry for how long each is kept).
ENTRY_REASON = "reason"
ENTRY_NOTE = "note"
#: A note a moderator must act on (something failed or was left undone): kept
#: like a note, and never trimmed from the report.
ENTRY_ALERT = "alert"
NOTE_KINDS = frozenset({ENTRY_NOTE, ENTRY_ALERT})

# The member-facing messages of a warn that ModerationGateway.claim_member_message
# dedupes per member per incident (outbox rows of these kinds).
MESSAGE_NOTICE = "notice"
MESSAGE_DM = "dm"
ENTRY_EVIDENCE = "evidence"
ENTRY_TRANSCRIPT = "transcript"
ENTRY_ASSESSMENT = "assessment"

_WHITESPACE = re.compile(r"\s+")


def fingerprint(author_id: str | int | None, content: str | None) -> str | None:
    """How a post looks, for joining copies of it into one incident.

    The same author posting the same text (whitespace and case aside) in
    several channels yields one fingerprint. Empty text yields none: two
    image-only posts are not the same post.
    """
    text = _WHITESPACE.sub(" ", (content or "").strip().casefold())
    if not author_id or not text:
        return None
    digest = hashlib.sha256(f"{author_id}\x00{text}".encode()).hexdigest()
    return digest[:32]


def epoch(moment: datetime) -> float:
    return moment.timestamp()


def from_epoch(seconds: float | None) -> datetime | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC)


@dataclass(frozen=True, slots=True)
class Evidence:
    """Something kept with the incident: a message, a transcript, a note.

    ``kind`` is one of the ``ENTRY_*`` kinds; ``evidence`` and ``transcript``
    are member content and age out after 90 days, ``assessment`` (a model's
    words) after 5 hours, ``note`` (the system's own words) stays.
    """

    kind: str
    content: str | None = None
    message_id: str | None = None
    channel_id: str | None = None
    author_id: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Trigger:
    """What fired, as the incident and its report should record it.

    ``actor`` names the system (``spam_engine``, ``spam_ring``,
    ``attachment_filter``, ``content_filter``, ``triage``,
    ``handler:<name>``, ``manual``); ``source`` is the
    ``moderation_actions.source`` its rows carry. ``report_channel_id`` asks
    for an incident report there; without one the incident is recorded but not
    reported (a spam warning alone never was). ``related_message_ids`` and
    ``aliases`` are further keys that find this incident later.
    """

    guild_id: str
    actor: str
    reason: str
    source: str = SOURCE_HANDLER
    channel_id: str | None = None
    message_id: str | None = None
    author_id: str | None = None
    content: str | None = None
    aliases: tuple[str, ...] = ()
    related_message_ids: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    report_channel_id: str | None = None
    ping_role_id: str | None = None
    moderator_user_id: str | None = None
    moderator_username: str | None = None

    @property
    def fingerprint(self) -> str | None:
        return fingerprint(self.author_id, self.content)

    @property
    def is_manual(self) -> bool:
        return self.source == SOURCE_MANUAL


@dataclass(frozen=True, slots=True)
class Proposal:
    """One effect a detector wants on one target.

    * ``ban``: ``delete_message_seconds`` purges their recent messages too.
    * ``timeout``: ``duration_seconds`` from now. ``claim`` names the claim
      (a hold's key); a later ``untimeout`` with the same claim ends it.
    * ``untimeout``: ends ``claim``, or every automated claim when None. The
      timeout falls back to the longest claim left, and a timeout somebody
      else placed is never lifted.
    * ``delete`` / ``purge``: ``message_ids`` in ``channel_id``, all by
      ``target_user_id``. Each message is deleted at most once guild-wide.
    * ``warn``: the caller delivers the notice (see
      :meth:`~smarter_dev.web.moderation_gateway.gateway.ModerationGateway.reserve`).
    """

    kind: str
    target_user_id: str
    target_username: str | None = None
    reason: str | None = None
    duration_seconds: int | None = None
    claim: str | None = None
    channel_id: str | None = None
    message_ids: tuple[str, ...] = ()
    delete_message_seconds: int = 0
    #: A timeout that contains while something is under review (a hold),
    #: not a sanction: it never makes a later warn or timeout "weaker".
    provisional: bool = False
    target_is_bot: bool | None = None
    #: Skip the staff check: the target is a webhook, whose message has no
    #: member behind it, or a moderator chose this target by hand.
    unprotected_target: bool = False
    ai_context_summary: str | None = None


@dataclass(frozen=True, slots=True)
class Outcome:
    """What became of one proposal."""

    kind: str
    target_user_id: str
    state: str
    detail: str
    operation_id: UUID | None = None
    action_id: UUID | None = None
    incident_id: UUID | None = None
    expires_at: datetime | None = None
    deleted: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    #: Skipped only because the incident already holds a sanction at least as
    #: strong. A warn skipped so still owes the member its messages (deduped
    #: through :meth:`ModerationGateway.claim_member_message`).
    superseded: bool = False

    @property
    def applied(self) -> bool:
        """Discord confirmed the effect (or a longer timeout already covers it)."""
        return self.state in (CONFIRMED, COVERED)


@dataclass(slots=True)
class ActResult:
    """Every outcome of one :meth:`ModerationGateway.act` call."""

    incident_id: UUID | None
    outcomes: list[Outcome] = field(default_factory=list)

    def first(self, kind: str | None = None) -> Outcome | None:
        for outcome in self.outcomes:
            if kind is None or outcome.kind == kind:
                return outcome
        return None


class EffectRejected(Exception):
    """Discord refused the call outright: it did not happen.

    ``status`` is the HTTP status (403 missing permission or hierarchy, 400
    rejected arguments). Ports raise this, or :class:`EffectNotFound`, only
    when the response proves nothing changed; everything else (5xx, a
    timeout, a dropped connection, a cancellation) is an unknown outcome.
    """

    def __init__(self, status: int | None, detail: str = "") -> None:
        super().__init__(detail or f"Discord refused it ({status})")
        self.status = status
        self.detail = detail or f"Discord refused it ({status})"


class EffectNotFound(EffectRejected):
    """Discord answered 404: the member, user or message is not there."""

    def __init__(self, detail: str = "not found") -> None:
        super().__init__(404, detail)


@dataclass(frozen=True, slots=True)
class TimeoutState:
    """A member's timeout as Discord holds it now."""

    is_member: bool
    until: float | None


@dataclass(slots=True)
class DeleteResult:
    """What a delete pass did with each message id."""

    deleted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: Discord refused these (403): definitively not deleted.
    refused: list[str] = field(default_factory=list)
    #: The call for these may or may not have landed.
    unknown: list[str] = field(default_factory=list)
