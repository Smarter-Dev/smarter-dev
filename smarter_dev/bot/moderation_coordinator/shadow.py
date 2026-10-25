"""The coordinator on live traffic: it judges, records, and (once a guild is
switched to it) acts.

For every human message (and every edit that changed the text) in a guild
listed in ``MODERATION_SHADOW_GUILD_IDS``, the coordinator makes its one
decision and :class:`ShadowRecorder` writes it to ``moderation_shadow_decisions``.
A minute-by-minute sweep (:class:`LegacyComparer`) then reads what the legacy
actors actually did to the same message, from the moderation gateway's own
records (the incident linked to ``msg:<id>``, its operations), and stores it
next to the decision with an agreement class.

**In shadow it cannot touch Discord.** The coordinator itself holds no
gateway, no port and no handler runtime; :class:`BotReads` is the only thing
it is given, and every method on it reads (the cache, the staff rule's REST
lookups, one channel-history read for a possible ring). Its classifier
requests are real and metered as ``moderation_shadow_classifier``, inside the
budget (:mod:`.classifier`).

**Acting (#240).** For a guild whose ownership row names the coordinator
(:mod:`.ownership`, written by ``scripts/moderation_cutover.py``), the
decision is recorded first and then handed to :class:`~.act.Executor`, which
carries it out through the moderation gateway. Recording first makes a
replayed event (a handover replays what the old pod may have seen) a no-op:
a revision already recorded is not acted on twice. Without that row nothing
changes: shadow, as before.

Nothing here blocks the bot: each message is judged in its own task, at most
:data:`MAX_CONCURRENT` label checks at a time (a decision waiting on a busy
ring channel holds no slot; the deterministic rules hold none and, when
acting, are carried out before any wait). Past :data:`MAX_PENDING` waiting, a
guild in shadow drops and counts the message; a guild the coordinator acts
for still has it judged on the deterministic rules, up to :data:`MAX_BACKLOG`.
Every task is tracked with
:func:`smarter_dev.bot.leadership.track`, so a process that stops acting
finishes the decisions it accepted before it shuts down.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import hikari
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import not_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from smarter_dev.bot import leadership
from smarter_dev.bot.moderation_coordinator.act import Executor
from smarter_dev.bot.moderation_coordinator.classifier import ClassifierBudget
from smarter_dev.bot.moderation_coordinator.classifier import ClassifierResult
from smarter_dev.bot.moderation_coordinator.classifier import ModerationClassifier
from smarter_dev.bot.moderation_coordinator.coordinator import Coordinator
from smarter_dev.bot.moderation_coordinator.coordinator import Evaluation
from smarter_dev.bot.moderation_coordinator.coordinator import budget_limits
from smarter_dev.bot.moderation_coordinator.coordinator import facts_hash
from smarter_dev.bot.moderation_coordinator.decide import PROVISIONAL_SECONDS
from smarter_dev.bot.moderation_coordinator.model import ACTION_RANK
from smarter_dev.bot.moderation_coordinator.model import Attachment
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import MessageFacts
from smarter_dev.bot.moderation_coordinator.model import PastAction
from smarter_dev.bot.moderation_coordinator.ownership import COORDINATOR
from smarter_dev.bot.moderation_coordinator.ownership import load_ownership
from smarter_dev.bot.moderation_coordinator.policy import PolicyCache
from smarter_dev.bot.moderation_coordinator.rules import SANCTION_ACTIONS
from smarter_dev.bot.purge_core import snowflake_to_datetime
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.web.models import MemberActivity
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationIncidentLink
from smarter_dev.web.models import ModerationOperation
from smarter_dev.web.models import ModerationShadowDecision
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.gateway import default_session_factory
from smarter_dev.web.moderation_policy_store import record_loads

logger = logging.getLogger(__name__)

GUILDS_ENV_VAR = "MODERATION_SHADOW_GUILD_IDS"
MAX_CONCURRENT = 4
MAX_PENDING = 200
#: Past MAX_PENDING, a guild the coordinator acts for is still judged on the
#: deterministic rules (cheap: no classifier, no ring), up to this many.
MAX_BACKLOG = 2000
BUDGET_EXHAUSTED_TEXT = (
    "⚠️ The moderation classifier's daily budget ({cap} requests) is used up. "
    "Until 00:00 UTC, scam and ring checks come back unknown: held posts are "
    "left for a moderator, and the deterministic rules still act."
)
#: The legacy side is read this long after the message: past the comparison
#: window below, so nothing that window counts can still be on its way.
LEGACY_SETTLE_SECONDS = 240.0
SWEEP_SECONDS = 60.0
#: The loads heartbeat's bound (it shares the comparison loop).
REPORT_LOADS_SECONDS = 10.0
#: Bounds on the ring's history read (see BotReads).
WINDOW_READ_SECONDS = 5.0
AUTHOR_FACTS_SECONDS = 600.0
MAX_CACHED_AUTHORS = 2000
SWEEP_BATCH = 500
#: Operations on the message's incident count when made from just before the
#: revision to this long after it.
LEGACY_WINDOW_SECONDS = 180.0
#: How far before a revision an operation may be stamped and still answer it
#: (listener order, pod clocks). Kept short: an author's revisions in a burst
#: come seconds apart, and a longer lead hands one message's action to the next.
LEGACY_LEAD_SECONDS = 1.0
#: How close ``member_activity`` may be to the message before it is taken to
#: already include it (the dispatch records the message as it fires).
ACTIVITY_SAME_SECONDS = 2.0
#: The automated actors whose effects the shadow is compared with.
LEGACY_ACTORS_PREFIX = (
    "spam_engine",
    "spam_ring",
    "attachment_filter",
    "content_filter",
    "handler:",
)


def shadow_guild_ids(raw: str | None = None) -> frozenset[str]:
    raw = os.getenv(GUILDS_ENV_VAR, "") if raw is None else raw
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def revision_of(edited_at: datetime | None, *, is_edit: bool, now: float) -> str:
    """``create``, or ``edit:<when Discord says it was edited, in ms>``.

    Taken from the edit's time, not its text, so a row holds nothing derived
    from what was written (a hash of a short edit could be matched against a
    guess); a replayed edit event carries the same time and is not judged
    twice.
    """
    if not is_edit:
        return "create"
    moment = edited_at.timestamp() if edited_at is not None else now
    return f"edit:{int(moment * 1000)}"


# ---------------------------------------------------------------------------
# Facts from hikari events
# ---------------------------------------------------------------------------


def _attachments(message: Any) -> tuple[Attachment, ...]:
    return tuple(
        Attachment(
            filename=str(getattr(item, "filename", "") or ""),
            content_type=getattr(item, "media_type", None),
            size=getattr(item, "size", None),
            url=getattr(item, "url", None),
            width=getattr(item, "width", None),
            height=getattr(item, "height", None),
        )
        for item in (getattr(message, "attachments", None) or ())
    )


def facts_from_message(
    *,
    guild_id: Any,
    channel_id: Any,
    message: Any,
    member: Any,
    is_edit: bool,
    now: float,
) -> MessageFacts:
    author = message.author
    content = message.content or ""
    return MessageFacts(
        guild_id=str(guild_id),
        channel_id=str(channel_id),
        message_id=str(message.id),
        author_id=str(author.id),
        author_name=getattr(author, "username", None) or str(author.id),
        author_display_name=(getattr(member, "nickname", None) if member else None)
        or getattr(author, "global_name", None)
        or None,
        content=content,
        posted_at=now,
        account_created_at=snowflake_to_datetime(int(author.id)),
        joined_at=getattr(member, "joined_at", None) if member else None,
        attachments=_attachments(message),
        revision=revision_of(
            getattr(message, "edited_timestamp", None), is_edit=is_edit, now=now
        ),
        is_edit=is_edit,
        author_is_bot=bool(getattr(author, "is_bot", False)),
        is_webhook=getattr(message, "webhook_id", None) is not None,
    )


# ---------------------------------------------------------------------------
# Reads (and nothing else)
# ---------------------------------------------------------------------------


class BotReads:
    """The coordinator's reads, from the bot's cache, REST and the database.

    The ring's history read shares hikari's rate limits with the legacy
    actors' deletes and timeouts, so it is bounded by
    :data:`WINDOW_READ_SECONDS` and each author's facts are kept for
    :data:`AUTHOR_FACTS_SECONDS`: a raid does not look the same accounts up
    once per message.
    """

    def __init__(self, bot: Any, *, clock=time.monotonic) -> None:
        self.bot = bot
        self._clock = clock
        self._authors: dict[tuple[str, str], tuple[float, Any]] = {}

    async def _author(self, guild_id: str, user, extra_role_ids) -> Any:
        from smarter_dev.bot.spam_ring import _resolve_author

        key = (guild_id, str(user.id))
        now = self._clock()
        cached = self._authors.get(key)
        if cached is not None and now - cached[0] < AUTHOR_FACTS_SECONDS:
            return cached[1]
        facts = await _resolve_author(self.bot, int(guild_id), user, extra_role_ids)
        if len(self._authors) >= MAX_CACHED_AUTHORS:
            for stale in [
                k
                for k, (at, _) in self._authors.items()
                if now - at >= AUTHOR_FACTS_SECONDS
            ]:
                del self._authors[stale]
            if len(self._authors) >= MAX_CACHED_AUTHORS:
                self._authors.clear()
        self._authors[key] = (now, facts)
        return facts

    async def staff_refusal(
        self,
        guild_id: str,
        user_id: str,
        *,
        is_bot: bool | None,
        extra_role_ids: tuple[str, ...],
    ) -> str | None:
        from smarter_dev.bot.staff_protection import staff_refusal

        return await staff_refusal(
            self.bot,
            int(guild_id),
            int(user_id),
            user_is_bot=is_bot,
            extra_role_ids=extra_role_ids,
        )

    async def author_history(
        self, guild_id: str, user_id: str, before: datetime
    ) -> AuthorHistory:
        async with default_session_factory()() as session:
            row = (
                await session.execute(
                    select(
                        MemberActivity.first_message_at, MemberActivity.last_message_at
                    ).where(
                        MemberActivity.guild_id == guild_id,
                        MemberActivity.user_id == user_id,
                    )
                )
            ).first()
            # A review hold (the gateway's provisional timeout) and its
            # release are no action on the member: told apart by their
            # operation, never by reason text, and left out in the query so
            # any number of them never hides a real action from the ten.
            # Only actions before this message: its own are not "earlier".
            actions = (
                await session.execute(
                    select(
                        ModerationAction.action_type,
                        ModerationAction.created_at,
                        ModerationAction.reason,
                    )
                    .outerjoin(
                        ModerationOperation,
                        ModerationOperation.id == ModerationAction.operation_id,
                    )
                    .where(
                        ModerationAction.guild_id == guild_id,
                        ModerationAction.target_user_id == user_id,
                        ModerationAction.action_type.in_(SANCTION_ACTIONS),
                        ModerationAction.created_at < before,
                        not_review_hold(),
                    )
                    .order_by(ModerationAction.created_at.desc())
                    .limit(HISTORY_ACTIONS)
                )
            ).all()
        return author_history_from(row, actions, before)

    async def channel_window(
        self,
        guild_id: str,
        channel_id: str,
        since: datetime,
        limit: int,
        extra_role_ids: tuple[str, ...] = (),
    ):
        from smarter_dev.bot.spam_ring import _to_transcript_message

        try:
            async with asyncio.timeout(WINDOW_READ_SECONDS):
                raw = []
                async for message in self.bot.rest.fetch_messages(
                    int(channel_id)
                ).limit(limit):
                    if message.created_at < since:
                        break
                    raw.append(message)
                raw.reverse()
                authors = {}
                for message in raw:
                    author_id = str(message.author.id)
                    if author_id in authors or getattr(message, "webhook_id", None):
                        continue
                    authors[author_id] = await self._author(
                        guild_id, message.author, extra_role_ids
                    )
            return [_to_transcript_message(message) for message in raw], authors
        except Exception:  # noqa: BLE001 — judged on the ad alone
            log_exception(
                logger,
                "Moderation shadow could not read channel %s",
                channel_id,
                level=logging.WARNING,
            )
            return None


#: The actions an author's history keeps.
HISTORY_ACTIONS = 10


def not_review_hold():
    """The action rows that are no review hold or release, for a query that
    outer-joins each row's operation. A hold is the gateway's provisional
    timeout (the coordinator's and the handlers' ``hold:`` claims); a release
    is an untimeout (one shortened to another claim is recorded as a
    timeout). A row with no operation was recorded outside the gateway: a
    moderator's own, always counted."""
    op = ModerationOperation
    provisional = func.coalesce(op.requested["provisional"].as_boolean(), False)
    return or_(
        op.id.is_(None),
        and_(
            op.kind != gw.UNTIMEOUT,
            not_(and_(op.kind == gw.TIMEOUT, provisional)),
        ),
    )


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def author_history_from(row, actions: Iterable, before: datetime) -> AuthorHistory:
    """The history as scam-banner saw it, before this message was recorded.

    The dispatch records each message in ``member_activity`` as it fires, and
    the bot's batched reports do too, so the row may already include the
    message being judged. A last message within :data:`ACTIVITY_SAME_SECONDS`
    of it is taken to be it: the first-message fact still holds when the
    first message is that close, and the time since the previous one cannot
    be told (None: not counted as long-silent).
    """
    past = tuple(
        PastAction(
            action_type=action[0],
            created_at=_utc(action[1]),
            reason=action[2] if len(action) > 2 else None,
        )
        for action in actions
    )
    if row is None:
        return AuthorHistory(
            is_first_message=True, days_since_last_message=None, actions=past
        )
    first, last = _utc(row[0]), _utc(row[1])
    margin = timedelta(seconds=ACTIVITY_SAME_SECONDS)
    if last >= before - margin:
        return AuthorHistory(
            is_first_message=first >= before - margin,
            days_since_last_message=None,
            actions=past,
        )
    return AuthorHistory(
        is_first_message=False,
        days_since_last_message=max(0, (before - last).days),
        actions=past,
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def row_from(evaluation: Evaluation) -> ModerationShadowDecision:
    facts = evaluation.facts
    decision = evaluation.decision
    provisional = decision.provisional
    return ModerationShadowDecision(
        guild_id=facts.guild_id,
        channel_id=facts.channel_id,
        message_id=facts.message_id,
        revision=facts.revision,
        is_edit=facts.is_edit,
        author_id=facts.author_id,
        message_at=datetime.fromtimestamp(facts.posted_at, UTC),
        policy=evaluation.policy.as_record(),
        rules=list(decision.rules_fired),
        labels={
            label: verdict.as_record()
            for label, verdict in sorted(evaluation.verdicts.items())
        },
        classifier={
            **evaluation.classifier.as_record(),
            "labels_wanted": evaluation.labels_wanted,
            "reused_labels": evaluation.reused_labels,
            "scam_outcome": evaluation.scam_outcome,
        },
        decision={
            "targets": {
                user_id: target.as_record()
                for user_id, target in sorted(decision.targets.items())
            },
            "review": decision.review,
        },
        strongest_action=decision.strongest,
        target_ids=decision.acted_on,
        staff_excluded=dict(decision.staff_excluded),
        provisional=None
        if provisional is None
        else {
            "user_id": provisional.user_id,
            "duration_seconds": provisional.duration_seconds,
            "reasons": list(provisional.reasons),
            "resolution": provisional.resolution,
            "after_ms": evaluation.provisional_ms,
        },
        latency_ms=evaluation.latency_ms,
    )


class ShadowRecorder:
    async def requests_today(
        self, guild_ids: Iterable[str], now: float
    ) -> dict[str, int]:
        """Classifier requests each guild's rows record since 00:00 UTC."""
        day_start = datetime.fromtimestamp(now - now % 86400, UTC)
        requests = ModerationShadowDecision.classifier["requests"].as_integer()
        async with default_session_factory()() as session:
            rows = await session.execute(
                select(
                    ModerationShadowDecision.guild_id,
                    func.coalesce(func.sum(requests), 0),
                )
                .where(
                    ModerationShadowDecision.guild_id.in_(list(guild_ids)),
                    ModerationShadowDecision.created_at >= day_start,
                )
                .group_by(ModerationShadowDecision.guild_id)
            )
            return {guild_id: int(total) for guild_id, total in rows}

    async def recorded(self, guild_id: str, message_id: str, revision: str) -> bool:
        """Whether this revision was already judged (a replayed event)."""
        async with default_session_factory()() as session:
            found = await session.execute(
                select(ModerationShadowDecision.id).where(
                    ModerationShadowDecision.guild_id == guild_id,
                    ModerationShadowDecision.message_id == message_id,
                    ModerationShadowDecision.revision == revision,
                )
            )
            return found.first() is not None

    async def record(self, row: ModerationShadowDecision) -> bool:
        """Insert one row; a revision already recorded (a replayed event) is kept."""
        async with default_session_factory()() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
        return True


# ---------------------------------------------------------------------------
# The legacy side
# ---------------------------------------------------------------------------

APPLIED = (gw.CONFIRMED, gw.COVERED)
#: The agreement of a revision the coordinator decided as the guild's owner.
ACTING = "acting"


def _is_provisional_hold(op: ModerationOperation) -> bool:
    """A hold while something was judged, not a sanction.

    scam-banner's 5-minute review hold is one; its 24-hour repeat hold
    (``hold_member(..., "repeat:" + key, DAY)``) is the sanction it decided,
    and counts as a timeout.
    """
    requested = op.requested or {}
    return (
        bool(requested.get("provisional"))
        and int(requested.get("duration_seconds") or 0) <= PROVISIONAL_SECONDS
    )


def summarize_legacy(
    operations: Iterable[ModerationOperation], message_id: str
) -> dict:
    """What the legacy actors did, from the gateway's operations.

    Per target: the strongest sanction that took effect (a provisional hold is
    reported apart, not as a sanction), whether any of the target's messages
    were removed, and who refused on staff grounds.
    """
    targets: dict[str, dict] = {}
    actors: set[str] = set()
    staff: dict[str, str] = {}
    deleted: set[str] = set()
    unknown = 0
    for op in operations:
        actors.add(op.actor or "")
        target = targets.setdefault(
            op.target_user_id,
            {"sanction": None, "deleted": False, "provisional": False, "unknown": []},
        )
        detail = op.detail or ""
        if op.state == gw.SKIPPED and detail.startswith("protected"):
            staff[op.target_user_id] = detail.removeprefix("protected: ")
            continue
        if op.state in (gw.UNKNOWN, gw.CLAIMED):
            unknown += 1
            target["unknown"].append(op.kind)
        if op.kind in gw.SANCTION_RANK and op.state in APPLIED:
            if _is_provisional_hold(op):
                target["provisional"] = True
                continue
            current = target["sanction"]
            if current is None or gw.SANCTION_RANK[op.kind] > gw.SANCTION_RANK[current]:
                target["sanction"] = op.kind
        if op.kind in gw.CLEANUP_KINDS and op.state in (*APPLIED, gw.ABSENT):
            result = op.result or {}
            removed = set(result.get("deleted") or []) | set(
                result.get("missing") or []
            )
            if not removed and op.state == gw.ABSENT:
                removed = set(op.message_ids or [])
            if removed:
                target["deleted"] = True
                deleted |= removed
    strongest = None
    acted = []
    for user_id, target in targets.items():
        kind = target["sanction"] or (gw.DELETE if target["deleted"] else None)
        if kind is not None:
            acted.append(user_id)
        if ACTION_RANK[kind] > ACTION_RANK[strongest]:
            strongest = kind
    return {
        "actors": sorted(actor for actor in actors if actor),
        "targets": targets,
        "acted_on": sorted(acted),
        "strongest": strongest,
        "message_deleted": message_id in deleted,
        "staff_excluded": staff,
        "unknown_operations": unknown,
    }


def agreement(
    shadow_strongest: str | None,
    shadow_targets: Iterable[str],
    legacy_strongest: str | None,
    legacy_targets: Iterable[str],
) -> str:
    ours, theirs = ACTION_RANK[shadow_strongest], ACTION_RANK[legacy_strongest]
    if ours == 0 and theirs == 0:
        return "both_none"
    if ours > theirs:
        return "shadow_stronger"
    if ours < theirs:
        return "shadow_weaker"
    if set(shadow_targets) != set(legacy_targets):
        return "different_targets"
    return "agree"


def _is_legacy_actor(actor: str | None) -> bool:
    return bool(actor) and any(
        actor == prefix or (prefix.endswith(":") and actor.startswith(prefix))
        for prefix in LEGACY_ACTORS_PREFIX
    )


def answered_revision(op: ModerationOperation, revisions) -> Any:
    """Which of the author's judged revisions ``op`` answers, or ``None``.

    ``revisions`` are ``(id, message_id, message_at)`` in posting order. An
    operation that names messages (a delete, a purge) answers the newest of
    them that was judged, whenever it was made. One that names none (a warn,
    a timeout) answers the latest revision posted before it, allowing
    :data:`LEGACY_LEAD_SECONDS` for order and clocks: the windows don't
    overlap, so the warn the previous copy earned is never this one's.
    """
    made = _utc(op.created_at)
    named = set(op.message_ids or ()) & {message_id for _, message_id, _ in revisions}
    if named:
        newest = max(named, key=lambda message_id: (len(message_id), message_id))
        candidates = [item for item in revisions if item[1] == newest]
    else:
        candidates = revisions
    answered = None
    for revision_id, _, posted in candidates:
        if posted - timedelta(seconds=LEGACY_LEAD_SECONDS) <= made:
            answered = revision_id
    if answered is None and named:
        answered = candidates[0][0]
    return answered


class LegacyComparer:
    """Fills in the legacy side of rows old enough for it to have settled."""

    def __init__(self, *, clock=time.time) -> None:
        self._clock = clock

    async def sweep(self) -> int:
        now = datetime.fromtimestamp(self._clock(), UTC)
        settled_before = now - timedelta(seconds=LEGACY_SETTLE_SECONDS)
        async with default_session_factory()() as session:
            rows = list(
                (
                    await session.execute(
                        select(ModerationShadowDecision)
                        .where(
                            ModerationShadowDecision.legacy_checked_at.is_(None),
                            ModerationShadowDecision.created_at <= settled_before,
                        )
                        .order_by(ModerationShadowDecision.created_at)
                        .limit(SWEEP_BATCH)
                    )
                ).scalars()
            )
            for row in rows:
                operations = await self._operations(session, row)
                legacy = summarize_legacy(operations, row.message_id)
                row.legacy = legacy
                row.legacy_strongest_action = legacy["strongest"]
                row.legacy_checked_at = now
                row.agreement = (
                    "not_judged"
                    if row.error
                    # The coordinator acted itself: there is no legacy side.
                    else ACTING
                    if (row.policy or {}).get("owner") == COORDINATOR
                    else agreement(
                        row.strongest_action,
                        row.target_ids or [],
                        legacy["strongest"],
                        legacy["acted_on"],
                    )
                )
            await session.commit()
        return len(rows)

    async def _operations(self, session, row: ModerationShadowDecision):
        """The automated actors' operations that answer this revision.

        The gateway folds an author's copies (and a message's edits) into one
        incident, so the incident alone over-attributes: the mute a fifth
        duplicate earned would show up on the third. Each operation on an
        incident linked to the message answers one of the author's judged
        revisions in it (:func:`answered_revision`), and counts here when that
        is this revision and it came within :data:`LEGACY_WINDOW_SECONDS`.
        """
        incident_ids = list(
            (
                await session.execute(
                    select(ModerationIncidentLink.incident_id).where(
                        ModerationIncidentLink.guild_id == row.guild_id,
                        ModerationIncidentLink.link_key == f"msg:{row.message_id}",
                    )
                )
            ).scalars()
        )
        if not incident_ids:
            return []
        linked = [
            key.removeprefix("msg:")
            for key in (
                await session.execute(
                    select(ModerationIncidentLink.link_key).where(
                        ModerationIncidentLink.incident_id.in_(incident_ids),
                        ModerationIncidentLink.link_key.like("msg:%"),
                    )
                )
            ).scalars()
        ]
        revisions = [
            (revision_id, message_id, _utc(message_at))
            for revision_id, message_id, message_at in (
                await session.execute(
                    select(
                        ModerationShadowDecision.id,
                        ModerationShadowDecision.message_id,
                        ModerationShadowDecision.message_at,
                    )
                    .where(
                        ModerationShadowDecision.guild_id == row.guild_id,
                        ModerationShadowDecision.author_id == row.author_id,
                        ModerationShadowDecision.message_id.in_(linked),
                    )
                    .order_by(ModerationShadowDecision.message_at)
                )
            ).all()
        ]
        at = _utc(row.message_at)
        end = at + timedelta(seconds=LEGACY_WINDOW_SECONDS)
        operations = (
            await session.execute(
                select(ModerationOperation).where(
                    ModerationOperation.incident_id.in_(incident_ids),
                    ModerationOperation.created_at >= at - timedelta(seconds=5),
                    ModerationOperation.created_at < end,
                )
            )
        ).scalars()
        return [
            op
            for op in operations
            if _is_legacy_actor(op.actor) and answered_revision(op, revisions) == row.id
        ]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class ModerationShadow:
    """The listener side: judge, record, never act."""

    def __init__(
        self,
        coordinator: Coordinator,
        *,
        guild_ids: frozenset[str],
        policies: PolicyCache | None = None,
        recorder: ShadowRecorder | None = None,
        executor: Executor | None = None,
        clock=time.time,
    ) -> None:
        self.coordinator = coordinator
        self.guild_ids = guild_ids
        self.policies = policies or PolicyCache(ownership=load_ownership)
        self.recorder = recorder or ShadowRecorder()
        #: Carries out decisions for guilds the coordinator owns; None never acts.
        self.executor = executor
        self._clock = clock
        self._slots = asyncio.Semaphore(MAX_CONCURRENT)
        self._tasks: set[asyncio.Task] = set()
        self.dropped = 0
        #: Messages past the queue judged on the deterministic rules only.
        self.backlogged = 0
        #: (guild, UTC day) whose classifier budget ran out, already reported.
        self._exhausted: set[tuple[str, int]] = set()
        #: The comparison loop, once started.
        self.background: asyncio.Task | None = None

    def wants(self, guild_id: Any) -> bool:
        return guild_id is not None and str(guild_id) in self.guild_ids

    def submit(self, facts: MessageFacts) -> asyncio.Task | None:
        """Judge ``facts`` in the background.

        Past :data:`MAX_PENDING` a guild the coordinator may own (acting, or
        not seen yet) is not dropped: nothing else moderates it, so the
        message is still judged on the deterministic rules (no classifier,
        no ring), up to :data:`MAX_BACKLOG`. A guild in shadow is dropped
        and counted, as before."""
        judge_labels = True
        if len(self._tasks) >= MAX_PENDING:
            known = self.policies.peek(facts.guild_id)
            may_act = self.executor is not None and (known is None or known.acting)
            if not may_act or len(self._tasks) >= MAX_BACKLOG:
                self.dropped += 1
                if self.dropped == 1 or self.dropped % 100 == 0:
                    logger.warning(
                        "Moderation shadow is behind; %d messages not judged so far",
                        self.dropped,
                    )
                return None
            judge_labels = False
            self.backlogged += 1
            if self.backlogged == 1 or self.backlogged % 100 == 0:
                logger.warning(
                    "Moderation coordinator is behind; %d messages judged on the "
                    "deterministic rules only so far",
                    self.backlogged,
                )
        task = asyncio.get_running_loop().create_task(
            self.judge(facts, judge_labels=judge_labels)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # A process that stops acting drains these before it shuts down
        # (client.drain_accepted_work), so an accepted decision is finished,
        # not dropped, at a handover.
        leadership.track(task)
        return task

    async def judge(
        self, facts: MessageFacts, *, judge_labels: bool = True
    ) -> ModerationShadowDecision | None:
        policy = None
        evaluation = None
        try:
            policy = await self.policies.get(facts.guild_id, facts.posted_at)
            if policy.revision is not None and not policy.document.controls.evaluate:
                # The policy switched evaluation off: nothing judged or kept.
                return None
            acting = self.executor is not None and policy.acting
            if acting and await self._recorded(facts):
                # A replayed event (a handover replays what the old pod may
                # have handled): judged and acted on already. Not judged
                # again, so no second hold either.
                logger.info(
                    "Coordinator skipped message %s revision %s: already decided",
                    facts.message_id,
                    facts.revision,
                )
                return None
            on_provisional = None
            on_deterministic = None
            if acting:

                async def on_provisional(facts_, provisional, key):
                    await self.executor.contain(facts_, provisional, key, policy=policy)

                async def on_deterministic(facts_, decision, key):
                    await self._act_early(facts_, policy, decision, key)

            evaluation = await self.coordinator.evaluate(
                facts,
                policy,
                slot=self._slots,
                on_provisional=on_provisional,
                on_deterministic=on_deterministic,
                judge_labels=judge_labels,
            )
            row = row_from(evaluation)
        except Exception as error:  # noqa: BLE001 — record that it was not judged
            log_exception(
                logger,
                "Moderation shadow could not judge message %s",
                facts.message_id,
                level=logging.WARNING,
            )
            if (
                self.executor is not None
                and policy is not None
                and policy.acting
                and evaluation is None
            ):
                await self.executor.evaluation_failed(
                    facts, facts_hash(facts), policy, error
                )
            row = ModerationShadowDecision(
                guild_id=facts.guild_id,
                channel_id=facts.channel_id,
                message_id=facts.message_id,
                revision=facts.revision,
                is_edit=facts.is_edit,
                author_id=facts.author_id,
                message_at=datetime.fromtimestamp(facts.posted_at, UTC),
                error=type(error).__name__,
            )
        fresh = True
        try:
            fresh = await self.recorder.record(row)
        except Exception:  # noqa: BLE001 — a lost row, never a lost message
            log_exception(
                logger,
                "Moderation shadow could not record message %s",
                facts.message_id,
                level=logging.WARNING,
            )
            row = None
        if (
            evaluation is not None
            and self.executor is not None
            and evaluation.policy.acting
        ):
            if not fresh:
                # Already recorded: a replayed event, already acted on, or
                # another pod judging the same message during a handover. A
                # hold notice this pod posted still gets its closing line.
                logger.info(
                    "Coordinator skipped message %s revision %s: already decided",
                    facts.message_id,
                    facts.revision,
                )
                await self.executor.close_hold(evaluation)
            else:
                await self._budget_exhausted(evaluation)
                result = None
                try:
                    result = await self.executor.execute(evaluation)
                except Exception:  # noqa: BLE001 — logged; the gateway reconciles
                    log_exception(
                        logger,
                        "Coordinator could not act on message %s",
                        facts.message_id,
                    )
                self._disarm_missed_mutes(evaluation, result)
        return row

    async def _recorded(self, facts: MessageFacts) -> bool:
        """Whether this revision was decided already. A failed read (the
        database is down) counts as not decided: the message is judged, so
        the deterministic rules still act, and the record's unique revision
        and the gateway's dedup absorb a replay (#310 re-review item 7)."""
        try:
            return await self.recorder.recorded(
                facts.guild_id, facts.message_id, facts.revision
            )
        except Exception as error:  # noqa: BLE001 — judged anyway
            logger.warning(
                "Coordinator could not check whether message %s revision %s was "
                "decided (%s); judging it",
                facts.message_id,
                facts.revision,
                type(error).__name__,
            )
            return False

    async def _budget_exhausted(self, evaluation: Evaluation) -> None:
        """The guild's daily classifier ceiling ran out: one WARNING and one
        line in the moderators' channel per guild and day (#310 item 5)."""
        if evaluation.classifier.refused_by != "guild_day":
            return
        facts = evaluation.facts
        day = (facts.guild_id, int(self._clock() // 86400))
        if day in self._exhausted:
            return
        self._exhausted.add(day)
        for stale in [d for d in self._exhausted if d[1] < day[1]]:
            self._exhausted.discard(stale)
        logger.warning(
            "Moderation classifier budget for guild %s is used up for today; "
            "scam checks come back unknown until 00:00 UTC",
            facts.guild_id,
        )
        await self.executor.notify(
            evaluation.policy,
            BUDGET_EXHAUSTED_TEXT.format(
                cap=self.coordinator.classifier.budget.day_cap(
                    True, budget_limits(evaluation.policy)
                )
            ),
        )

    async def _act_early(self, facts, policy, decision, key) -> None:
        """The deterministic rules' decision, carried out before any
        classifier wait. Never raises: the label checks still follow."""
        early = Evaluation(
            facts=facts,
            policy=policy,
            decision=decision,
            classifier=ClassifierResult(),
            key=key,
        )
        result = None
        try:
            result = await self.executor.execute(early)
        except Exception:  # noqa: BLE001 — logged; the gateway reconciles
            log_exception(
                logger,
                "Coordinator could not act on message %s",
                facts.message_id,
            )
        self._disarm_missed_mutes(early, result)

    def _disarm_missed_mutes(self, evaluation: Evaluation, result) -> None:
        """A spam mute Discord did not apply leaves the author counted again."""
        applied = {
            outcome.target_user_id
            for outcome in (result.outcomes if result is not None else ())
            if outcome.kind == gw.TIMEOUT and outcome.applied
        }
        for user_id, target in evaluation.to_carry_out.targets.items():
            if (
                "spam_engine" in target.rules
                and target.sanction is not None
                and target.sanction.kind == gw.TIMEOUT
                and user_id not in applied
            ):
                self.coordinator.spam.disarm(evaluation.facts.guild_id, user_id)

    async def on_create(self, event: hikari.GuildMessageCreateEvent) -> None:
        if not self.wants(event.guild_id) or not event.is_human:
            return
        self.submit(
            facts_from_message(
                guild_id=event.guild_id,
                channel_id=event.channel_id,
                message=event.message,
                member=event.member,
                is_edit=False,
                now=self._clock(),
            )
        )

    async def on_update(self, event: hikari.GuildMessageUpdateEvent) -> None:
        if not self.wants(event.guild_id) or not event.is_human:
            return
        message = event.message
        content = message.content
        # Unfurls and pins re-send the message with the same text: not a new
        # revision (the same rule the message_edit handler trigger applies).
        if not content:
            return
        old = event.old_message
        if old is not None and old.content == content:
            return
        self.submit(
            facts_from_message(
                guild_id=event.guild_id,
                channel_id=event.channel_id,
                message=message,
                member=event.member or None,
                is_edit=True,
                now=self._clock(),
            )
        )

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


async def heartbeat(policies: PolicyCache) -> None:
    """:func:`report_loads`, bounded by :data:`REPORT_LOADS_SECONDS`: a slow
    database must not hold up the comparison loop it shares. Never raises."""
    try:
        await asyncio.wait_for(report_loads(policies), REPORT_LOADS_SECONDS)
    except TimeoutError:
        logger.warning(
            "Moderation shadow gave up recording its loaded policy revisions "
            "after %.0f s",
            REPORT_LOADS_SECONDS,
        )


def process_name() -> str:
    """This process as ``moderation_policy_loads`` names it: the bot's pod."""
    return f"bot/{os.getenv('HOSTNAME') or socket.gethostname()}"[:200]


async def report_loads(
    policies: PolicyCache, process: str | None = None, now: float | None = None
) -> None:
    """This process's loaded policy revisions, as its heartbeat (#241). Never
    raises: a missed heartbeat only ages the row on the admin page."""
    now = time.time() if now is None else now
    loaded = {
        guild_id: (revision, datetime.fromtimestamp(since, UTC))
        for guild_id, (revision, since) in policies.loaded().items()
    }
    try:
        async with default_session_factory()() as session:
            await record_loads(
                session,
                process or process_name(),
                loaded,
                datetime.fromtimestamp(now, UTC),
            )
    except Exception:  # noqa: BLE001 — the next beat tries again
        log_exception(
            logger,
            "Moderation shadow could not record its loaded policy revisions",
            level=logging.WARNING,
        )


async def restore_budget(
    budget: ClassifierBudget, recorder: ShadowRecorder, guild_ids: Iterable[str]
) -> None:
    """Read today's spend back into ``budget``.

    On failure the day opens on a fresh count (fail open, #310 item 5: shut,
    every scam check in an acting guild would be unknown) and the read is
    tried again on the next pass and merged in.

    App ``StartedEvent`` listeners run once this process acts, so the count
    includes what the previous process recorded before it handed over.
    """
    now = time.time()
    try:
        spent = await recorder.requests_today(guild_ids, now)
    except Exception:  # noqa: BLE001 — retried on the next pass
        log_exception(
            logger,
            "Moderation shadow could not read today's classifier spend; "
            "counting from zero until it can",
            level=logging.WARNING,
        )
        budget.open_fresh()
        return
    budget.restore(spent, now)
    logger.info(
        "Moderation shadow classifier budget restored: %s",
        ", ".join(f"{guild}={count}" for guild, count in sorted(spent.items()))
        or "nothing spent today",
    )


def register_moderation_shadow(bot: Any) -> ModerationShadow | None:
    """Listen for the shadow guilds' messages; None when no guild is listed."""
    guild_ids = shadow_guild_ids()
    if not guild_ids:
        return None
    budget = ClassifierBudget(restored=False)
    shadow = ModerationShadow(
        Coordinator(BotReads(bot), ModerationClassifier(budget=budget)),
        guild_ids=guild_ids,
        # Acts only in a guild whose ownership row names the coordinator.
        executor=Executor.for_bot(bot),
    )

    @bot.listen()
    async def on_moderation_shadow_create(
        event: hikari.GuildMessageCreateEvent,
    ) -> None:
        await shadow.on_create(event)

    @bot.listen()
    async def on_moderation_shadow_update(
        event: hikari.GuildMessageUpdateEvent,
    ) -> None:
        await shadow.on_update(event)

    comparer = LegacyComparer()

    async def compare_forever() -> None:
        while True:
            if not budget.restored or budget.restore_pending:
                await restore_budget(budget, shadow.recorder, guild_ids)
            await asyncio.sleep(SWEEP_SECONDS)
            # Every process, acting or not: what it runs on is on the page.
            await heartbeat(shadow.policies)
            if not leadership.is_acting():
                continue
            try:
                await comparer.sweep()
            except Exception:  # noqa: BLE001 — the next pass tries again
                log_exception(logger, "Moderation shadow comparison failed")

    @bot.listen()
    async def on_moderation_shadow_started(event: hikari.StartedEvent) -> None:
        # Kept: the loop holds the only reference otherwise.
        shadow.background = asyncio.get_running_loop().create_task(compare_forever())

    logger.info(
        "Moderation coordinator on for guild(s) %s (shadow unless a guild is "
        "switched to it)",
        ", ".join(sorted(guild_ids)),
    )
    return shadow
