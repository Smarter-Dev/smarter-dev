"""The coordinator in shadow: it judges live traffic and records, nothing else.

For every human message (and every edit that changed the text) in a guild
listed in ``MODERATION_SHADOW_GUILD_IDS``, the coordinator makes its one
decision and :class:`ShadowRecorder` writes it to ``moderation_shadow_decisions``.
A minute-by-minute sweep (:class:`LegacyComparer`) then reads what the legacy
actors actually did to the same message, from the moderation gateway's own
records (the incident linked to ``msg:<id>``, its operations), and stores it
next to the decision with an agreement class.

**It cannot touch Discord.** It holds no gateway, no port and no handler
runtime; :class:`BotReads` is the only thing it is given, and every method on
it reads (the cache, the staff rule's REST lookups, one channel-history read
for a possible ring). Its classifier requests are real and metered as
``moderation_shadow_classifier``, inside the budget (:mod:`.classifier`).

Nothing here blocks the bot: each message is judged in its own task, at most
:data:`MAX_CONCURRENT` at a time, and one more than :data:`MAX_PENDING`
waiting is dropped and counted rather than queued.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import hikari
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from smarter_dev.bot.moderation_coordinator.classifier import ModerationClassifier
from smarter_dev.bot.moderation_coordinator.coordinator import Coordinator
from smarter_dev.bot.moderation_coordinator.coordinator import Evaluation
from smarter_dev.bot.moderation_coordinator.model import ACTION_RANK
from smarter_dev.bot.moderation_coordinator.model import Attachment
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import MessageFacts
from smarter_dev.bot.moderation_coordinator.model import PastAction
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

logger = logging.getLogger(__name__)

GUILDS_ENV_VAR = "MODERATION_SHADOW_GUILD_IDS"
MAX_CONCURRENT = 4
MAX_PENDING = 200
#: The legacy side is read this long after the message: the slowest legacy
#: actor (the scam-banner handler, a model call and a fire queue) settles
#: well inside it.
LEGACY_SETTLE_SECONDS = 150.0
SWEEP_SECONDS = 60.0
SWEEP_BATCH = 500
#: Operations on the message's incident count when made from just before the
#: revision to this long after it.
LEGACY_WINDOW_SECONDS = 180.0
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


def revision_of(content: str, *, is_edit: bool) -> str:
    if not is_edit:
        return "create"
    return "edit:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]


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
        content=content,
        posted_at=now,
        account_created_at=snowflake_to_datetime(int(author.id)),
        joined_at=getattr(member, "joined_at", None) if member else None,
        attachments=_attachments(message),
        revision=revision_of(content, is_edit=is_edit),
        is_edit=is_edit,
        author_is_bot=bool(getattr(author, "is_bot", False)),
        is_webhook=getattr(message, "webhook_id", None) is not None,
    )


# ---------------------------------------------------------------------------
# Reads (and nothing else)
# ---------------------------------------------------------------------------


class BotReads:
    """The coordinator's reads, from the bot's cache, REST and the database."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

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
            actions = (
                await session.execute(
                    select(ModerationAction.action_type, ModerationAction.created_at)
                    .where(
                        ModerationAction.guild_id == guild_id,
                        ModerationAction.target_user_id == user_id,
                        ModerationAction.action_type.in_(SANCTION_ACTIONS),
                    )
                    .order_by(ModerationAction.created_at.desc())
                    .limit(10)
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
        from smarter_dev.bot.spam_ring import _resolve_author
        from smarter_dev.bot.spam_ring import _to_transcript_message

        try:
            raw = []
            async for message in self.bot.rest.fetch_messages(int(channel_id)).limit(
                limit
            ):
                if message.created_at < since:
                    break
                raw.append(message)
            raw.reverse()
            authors = {}
            for message in raw:
                author_id = str(message.author.id)
                if author_id in authors or getattr(message, "webhook_id", None):
                    continue
                authors[author_id] = await _resolve_author(
                    self.bot, int(guild_id), message.author, extra_role_ids
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
        PastAction(action_type=action_type, created_at=_utc(created_at))
        for action_type, created_at in actions
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
            if (op.requested or {}).get("provisional"):
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
        start = _utc(row.message_at) - timedelta(seconds=5)
        end = _utc(row.message_at) + timedelta(seconds=LEGACY_WINDOW_SECONDS)
        operations = (
            await session.execute(
                select(ModerationOperation).where(
                    ModerationOperation.incident_id.in_(incident_ids),
                    ModerationOperation.created_at >= start,
                    ModerationOperation.created_at <= end,
                )
            )
        ).scalars()
        return [op for op in operations if _is_legacy_actor(op.actor)]


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
        clock=time.time,
    ) -> None:
        self.coordinator = coordinator
        self.guild_ids = guild_ids
        self.policies = policies or PolicyCache()
        self.recorder = recorder or ShadowRecorder()
        self._clock = clock
        self._slots = asyncio.Semaphore(MAX_CONCURRENT)
        self._tasks: set[asyncio.Task] = set()
        self.dropped = 0

    def wants(self, guild_id: Any) -> bool:
        return guild_id is not None and str(guild_id) in self.guild_ids

    def submit(self, facts: MessageFacts) -> asyncio.Task | None:
        """Judge ``facts`` in the background; dropped (and counted) when full."""
        if len(self._tasks) >= MAX_PENDING:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.warning(
                    "Moderation shadow is behind; %d messages not judged so far",
                    self.dropped,
                )
            return None
        task = asyncio.get_running_loop().create_task(self.judge(facts))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def judge(self, facts: MessageFacts) -> ModerationShadowDecision | None:
        async with self._slots:
            try:
                policy = await self.policies.get(facts.guild_id, facts.posted_at)
                evaluation = await self.coordinator.evaluate(facts, policy)
                row = row_from(evaluation)
            except Exception as error:  # noqa: BLE001 — record that it was not judged
                log_exception(
                    logger,
                    "Moderation shadow could not judge message %s",
                    facts.message_id,
                    level=logging.WARNING,
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
            try:
                await self.recorder.record(row)
            except Exception:  # noqa: BLE001 — a lost row, never a lost message
                log_exception(
                    logger,
                    "Moderation shadow could not record message %s",
                    facts.message_id,
                    level=logging.WARNING,
                )
                return None
            return row

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


def register_moderation_shadow(bot: Any) -> ModerationShadow | None:
    """Listen for the shadow guilds' messages; None when no guild is listed."""
    guild_ids = shadow_guild_ids()
    if not guild_ids:
        return None
    from smarter_dev.bot import leadership

    shadow = ModerationShadow(
        Coordinator(BotReads(bot), ModerationClassifier()), guild_ids=guild_ids
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
            await asyncio.sleep(SWEEP_SECONDS)
            if not leadership.is_acting():
                continue
            try:
                await comparer.sweep()
            except Exception:  # noqa: BLE001 — the next pass tries again
                log_exception(logger, "Moderation shadow comparison failed")

    @bot.listen()
    async def on_moderation_shadow_started(event: hikari.StartedEvent) -> None:
        asyncio.get_running_loop().create_task(compare_forever())

    logger.info(
        "Moderation coordinator shadow on for guild(s) %s", ", ".join(sorted(guild_ids))
    )
    return shadow
