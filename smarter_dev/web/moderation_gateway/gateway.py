"""The action gateway: one way for every actor to moderate a member.

Detectors (the spam engine, the spam ring, the attachment and content
filters, the staff-ping triage, admin handler scripts) and the manual
commands no longer call Discord to punish anyone. They describe what fired and
what they want (:class:`~.types.Trigger`, :class:`~.types.Proposal`), and the
gateway decides, acts, records and reports:

1. **Incident.** The trigger joins an open incident when it shares a key with
   it: the same message, an alias (a spam ring's), a copy of the same post by
   the same author inside :data:`GROUP_WINDOW_SECONDS`. Otherwise it opens
   one. Its reason and evidence are added.
2. **Per member, under that member's lock** (:mod:`.locks`, shared by the bot
   and the handler workers):

   * anything still unknown about this member is reconciled with Discord;
   * the staff-protection rule is asked again, and a moderator's reversal in
     this incident (an untimeout, an unban) stands automation down;
   * strongest wins: within an incident a weaker or duplicate sanction is
     never applied (ban > kick > timeout > warn); a delete is cleanup, and
     each message is deleted at most once guild-wide;
   * a timeout only ever extends, never shortens, and a timeout somebody else
     placed is left alone;
   * the operation is written and claimed, the one REST call is made, and
     the outcome is settled with the ``moderation_actions`` row and its
     announcement in one commit. A call whose outcome is unknown stays
     ``unknown`` until :meth:`ModerationGateway.reconcile_pending` (or the
     next action on the member) reads Discord; nothing is resent blindly.
3. **One report** per incident in the moderators' channel, edited as the
   incident grows, rendered from these records (:mod:`.report`).

Manual commands go through the same path with ``source="manual"``: they skip
the automated checks (a moderator decides), but are serialised with the
automation and leave the same records.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationIncident
from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.models import ModerationIncidentLink
from smarter_dev.web.models import ModerationOperation
from smarter_dev.web.models import ModerationOutbox
from smarter_dev.web.moderation_gateway import report as report_mod
from smarter_dev.web.moderation_gateway import types as t
from smarter_dev.web.moderation_gateway.locks import MemberLocks
from smarter_dev.web.moderation_gateway.port import ModerationPort
from smarter_dev.web.webhook_urls import redact_webhook_tokens

logger = logging.getLogger(__name__)

#: How long an incident keeps taking related triggers after its last one.
GROUP_WINDOW_SECONDS = 600.0
#: A claimed call older than this is presumed interrupted and reconciled.
OPERATION_LEASE_SECONDS = 90.0
#: An unknown outcome waits this long before it is checked, so Discord has
#: settled whatever it did.
RECONCILE_GRACE_SECONDS = 5.0
#: Discord stores a timeout to the millisecond; two expiries this close are one.
SAME_EXPIRY_SECONDS = 2.0
#: A timeout must end at least this much later than the one in place to be
#: worth a call; two detectors asking for "24 hours" a second apart are one.
EXTEND_MIN_SECONDS = 60.0
REPORT_LEASE_SECONDS = 30.0
#: Discord's own ceiling for a timeout.
MAX_TIMEOUT_SECONDS = 28 * 86400
#: Human reversals that stand automation down for the rest of an incident.
REVERSAL_ACTIONS = ("untimeout", "unban")
UNRESOLVED_DETAIL = "may have been delivered; never sent again"


#: Where gateways get their database sessions when not handed a factory.
#: Tests point it at a throwaway database; production uses the main one.
SESSION_FACTORY_OVERRIDE: Callable[[], Any] | None = None


def default_session_factory() -> Callable[[], Any]:
    return SESSION_FACTORY_OVERRIDE or get_db_session_context


def _now_dt(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _epoch_of(moment: datetime | None) -> float | None:
    """A stored time as epoch seconds; SQLite hands it back without a zone."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def _timeout_key_prefix(incident_id: UUID, target: str, claim: str | None) -> str:
    # A claim can be long (a handler id and a script's key); the key names it
    # by digest so it fits the column.
    digest = hashlib.sha256((claim or "").encode()).hexdigest()[:16]
    return f"timeout:{incident_id}:{target}:{digest}:"


@dataclass(slots=True)
class _Decision:
    """What to do with a proposal once the checks ran."""

    state: str | None = None  # SKIPPED / COVERED / FAILED: settle without REST
    detail: str = ""
    until: float | None = None  # timeout / untimeout target (None lifts)
    message_ids: tuple[str, ...] = ()
    released_claims: tuple[UUID, ...] = ()
    key: str | None = None  # the idempotency key, when the decision fixed it
    superseded: bool = False  # skipped for a sanction already in the incident
    in_place_until: float | None = None  # covered: the timeout already in place


class Reservation:
    """A caller-performed effect, inside :meth:`ModerationGateway.reserve`.

    ``go`` says whether to perform it. Call :meth:`confirm` once it landed or
    :meth:`fail` when it did not; leaving the block without either records it
    as not done, and an exception records it as ``unknown`` (a notice may have
    been posted), which is never retried.
    """

    def __init__(self, go: bool, outcome: t.Outcome | None = None) -> None:
        self.go = go
        self.outcome = outcome
        self.action: ModerationAction | None = None
        self.incident_id: UUID | None = None
        self._settle: tuple[str, str, dict | None] | None = None

    def confirm(self, detail: str = "", result: dict | None = None) -> None:
        self._settle = (t.CONFIRMED, detail, result)

    def fail(self, detail: str) -> None:
        self._settle = (t.FAILED, detail, None)


class ModerationGateway:
    """Acts for one guild's detectors through one :class:`ModerationPort`."""

    def __init__(
        self,
        port: ModerationPort,
        *,
        session_factory: Callable[[], Any] | None = None,
        locks: MemberLocks | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.port = port
        self._session_factory = session_factory or default_session_factory()
        self._clock = clock
        self.locks = locks or MemberLocks(self._session, clock=clock)

    def _session(self):
        return self._session_factory()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def act(
        self, trigger: t.Trigger, proposals: Sequence[t.Proposal]
    ) -> t.ActResult:
        """Decide and perform ``proposals`` for ``trigger``; never raises for Discord."""
        subject = trigger.author_id or (
            proposals[0].target_user_id if proposals else None
        )
        incident_id = await self.ensure_incident(trigger, subject=subject)
        result = t.ActResult(incident_id=incident_id)
        try:
            for proposal in proposals:
                result.outcomes.append(
                    await self._execute(incident_id, trigger, proposal)
                )
        finally:
            await self._after(incident_id)
        return result

    @contextlib.asynccontextmanager
    async def reserve(
        self, trigger: t.Trigger, proposal: t.Proposal
    ) -> AsyncIterator[Reservation]:
        """An effect the caller performs itself (a warn's notice, a manual record).

        The member's lock is held for the whole block, so the effect is
        serialised with every other action on them.
        """
        subject = trigger.author_id or proposal.target_user_id
        incident_id = await self.ensure_incident(trigger, subject=subject)
        try:
            async with self.locks.hold(trigger.guild_id, proposal.target_user_id):
                await self._reconcile_target(trigger.guild_id, proposal.target_user_id)
                decision = await self._decide(incident_id, trigger, proposal)
                if decision.state is not None:
                    outcome = await self._record_without_call(
                        incident_id, trigger, proposal, decision
                    )
                    reservation = Reservation(go=False, outcome=outcome)
                    reservation.incident_id = incident_id
                    yield reservation
                    return
                op_id, existing = await self._claim(
                    incident_id, trigger, proposal, decision
                )
                if existing is not None:
                    reservation = Reservation(go=False, outcome=existing)
                    reservation.incident_id = incident_id
                    yield reservation
                    return
                reservation = Reservation(go=True)
                reservation.incident_id = incident_id
                try:
                    yield reservation
                except BaseException:
                    await asyncio.shield(
                        self._settle_unknown(op_id, "the caller failed partway")
                    )
                    raise
                state, detail, result = reservation._settle or (
                    t.SKIPPED,
                    "not performed",
                    None,
                )
                if state == t.CONFIRMED:
                    outcome, action = await self._settle_confirmed(
                        op_id, trigger, proposal, detail=detail, result=result or {}
                    )
                    reservation.action = action
                else:
                    outcome = await self._settle_simple(op_id, state, detail)
                reservation.outcome = outcome
        finally:
            await self._after(incident_id)

    async def ensure_incident(
        self, trigger: t.Trigger, *, subject: str | None = None
    ) -> UUID:
        """The incident ``trigger`` belongs to, opened if none; adds its reason."""
        subject = subject or trigger.author_id
        lock = (
            self.locks.hold(trigger.guild_id, subject)
            if subject
            else contextlib.nullcontext()
        )
        async with lock:
            for attempt in range(3):
                try:
                    return await self._resolve_incident(trigger, subject)
                except IntegrityError:
                    # Another process claimed one of our keys first; its
                    # incident is the one now, so look again.
                    if attempt == 2:
                        raise
        raise AssertionError("unreachable")

    async def claim_member_message(
        self, incident_id: UUID, *, kind: str, target_user_id: str
    ) -> bool:
        """Whether to send a warn's member-facing message; True claims it.

        ``kind`` is ``notice`` (the public one), ``dm`` or ``warning_dm``. One
        of each per member per incident: a warn the incident skipped still
        tells the member, unless the incident already did. A notice is also
        taken by a warn the incident confirmed, since a warn's notice is
        public (not a ``dm_only`` warn's: it had none).
        """
        target = str(target_user_id)
        async with self._session() as session:
            incident = await session.get(ModerationIncident, incident_id)
            if incident is None:
                return False
            if kind == t.MESSAGE_NOTICE:
                warned = (
                    await session.execute(
                        select(ModerationOperation.requested).where(
                            ModerationOperation.incident_id == incident_id,
                            ModerationOperation.target_user_id == target,
                            ModerationOperation.kind == t.WARN,
                            ModerationOperation.state == t.CONFIRMED,
                        )
                    )
                ).scalars()
                if any(not (requested or {}).get("dm_only") for requested in warned):
                    return False
            session.add(
                ModerationOutbox(
                    incident_id=incident_id,
                    guild_id=incident.guild_id,
                    kind=kind,
                    dedupe_key=f"{kind}:{incident_id}:{target}",
                    target_user_id=target,
                    state="sent",
                    settled_at=_now_dt(self._clock()),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                return False
        return True

    async def record_member_message(
        self,
        incident_id: UUID,
        *,
        kind: str,
        target_user_id: str,
        delivered: bool,
        source: str,
        report_channel_id: str | None = None,
    ) -> None:
        """Whether a claimed DM (``dm`` or ``warning_dm``) reached the member.

        Kept on its outbox row (``detail``) and as a ``delivery`` entry, so
        the report's timeline says "DM delivered" or "not delivered"."""
        target = str(target_user_id)
        async with self._session() as session:
            await session.execute(
                update(ModerationOutbox)
                .where(
                    ModerationOutbox.incident_id == incident_id,
                    ModerationOutbox.dedupe_key == f"{kind}:{incident_id}:{target}",
                )
                .values(detail="delivered" if delivered else "not delivered")
            )
            await session.commit()
        what = "Warning DM" if kind == t.MESSAGE_WARNING_DM else "DM"
        text = (
            f"{what} to <@{target}> delivered."
            if delivered
            else f"{what} to <@{target}> not delivered (DMs closed or Discord "
            "refused it)."
        )
        await self.add_notes(
            incident_id,
            source=source,
            notes=[(t.ENTRY_DELIVERY, text, {"delivered": delivered, "message": kind})],
            report_channel_id=report_channel_id,
            target_user_id=target,
        )

    async def release_member_message(
        self, incident_id: UUID, *, kind: str, target_user_id: str
    ) -> None:
        """Give back a claim whose message was never sent (a cap stopped it)."""
        async with self._session() as session:
            await session.execute(
                delete(ModerationOutbox).where(
                    ModerationOutbox.dedupe_key
                    == f"{kind}:{incident_id}:{target_user_id}",
                    ModerationOutbox.incident_id == incident_id,
                )
            )
            await session.commit()

    async def add_note(
        self,
        incident_id: UUID,
        *,
        source: str,
        text: str,
        kind: str = t.ENTRY_NOTE,
        report_channel_id: str | None = None,
        target_user_id: str | None = None,
    ) -> None:
        """Add a line to the incident (and its report, posting one if asked)."""
        await self.add_notes(
            incident_id,
            source=source,
            notes=[(kind, text)],
            report_channel_id=report_channel_id,
            target_user_id=target_user_id,
        )

    async def add_notes(
        self,
        incident_id: UUID,
        *,
        source: str,
        notes: Sequence[tuple[str, str] | tuple[str, str, dict[str, Any] | None]],
        report_channel_id: str | None = None,
        target_user_id: str | None = None,
    ) -> None:
        """Several ``(kind, text)`` or ``(kind, text, details)`` lines at once,
        then one report update."""
        async with self._session() as session:
            incident = await session.get(ModerationIncident, incident_id)
            if incident is None:
                return
            for note in notes:
                kind, text = note[0], note[1]
                session.add(
                    ModerationIncidentEntry(
                        incident_id=incident_id,
                        guild_id=incident.guild_id,
                        kind=kind,
                        source=source,
                        content=redact_webhook_tokens(text)
                        if kind not in t.NOTE_KINDS
                        else None,
                        rule=text if kind in t.NOTE_KINDS else None,
                        target_user_id=target_user_id,
                        details=note[2] if len(note) > 2 else None,
                    )
                )
            if report_channel_id and not incident.report_channel_id:
                incident.report_channel_id = report_channel_id
            await self._bump(session, incident_id)
            await session.commit()
        await self.deliver_report(incident_id)

    async def report_channel_of(self, incident_id: UUID) -> str | None:
        async with self._session() as session:
            incident = await session.get(ModerationIncident, incident_id)
            return incident.report_channel_id if incident else None

    async def report_message_of(self, incident_id: UUID) -> str | None:
        async with self._session() as session:
            incident = await session.get(ModerationIncident, incident_id)
            return incident.report_message_id if incident else None

    async def reconcile_pending(self, guild_id: str | None = None) -> int:
        """Settle every unknown or interrupted operation that is due; returns how many."""
        now = self._clock()
        async with self._session() as session:
            query = select(
                ModerationOperation.guild_id, ModerationOperation.target_user_id
            ).where(self._due_for_reconcile(now))
            if guild_id is not None:
                query = query.where(ModerationOperation.guild_id == guild_id)
            targets = sorted(set((await session.execute(query)).all()))
        settled = 0
        for target_guild, target_user in targets:
            try:
                async with self.locks.hold(target_guild, target_user):
                    settled += await self._reconcile_target(target_guild, target_user)
            except Exception:  # noqa: BLE001 — the next pass tries again
                log_exception(
                    logger,
                    "Reconciling moderation operations for %s failed",
                    target_user,
                    level=logging.WARNING,
                )
        return settled

    async def sweep(self, *, report_horizon_seconds: float = 86400.0) -> None:
        """The periodic pass: reconcile, deliver what a crash left, catch reports up."""
        await self.reconcile_pending()
        await self.deliver_outbox()
        horizon = _now_dt(self._clock() - report_horizon_seconds)
        async with self._session() as session:
            behind = list(
                (
                    await session.execute(
                        select(ModerationIncident.id).where(
                            ModerationIncident.report_channel_id.is_not(None),
                            ModerationIncident.report_state != "failed",
                            ModerationIncident.report_revision
                            < ModerationIncident.revision,
                            ModerationIncident.opened_at >= horizon,
                        )
                    )
                ).scalars()
            )
        for incident_id in behind:
            await self.deliver_report(incident_id)

    async def deliver_report(self, incident_id: UUID) -> None:
        """Post or edit the incident's report until it shows the latest revision."""
        for _ in range(5):
            if not await self._deliver_report_once(incident_id):
                return

    # ------------------------------------------------------------------
    # Incidents
    # ------------------------------------------------------------------

    def _link_keys(self, trigger: t.Trigger) -> list[str]:
        keys: list[str] = []
        if trigger.message_id:
            keys.append(f"msg:{trigger.message_id}")
        keys.extend(f"msg:{message_id}" for message_id in trigger.related_message_ids)
        keys.extend(f"alias:{alias}" for alias in trigger.aliases)
        return list(dict.fromkeys(keys))

    async def _resolve_incident(self, trigger: t.Trigger, subject: str | None) -> UUID:
        now = self._clock()
        keys = self._link_keys(trigger)
        async with self._session() as session:
            incident = await self._find_incident(session, trigger, keys, now)
            if incident is None:
                incident = ModerationIncident(
                    id=uuid.uuid4(),
                    guild_id=trigger.guild_id,
                    subject_user_id=subject,
                    channel_id=trigger.channel_id,
                    trigger_message_id=trigger.message_id,
                    opened_at=_now_dt(now),
                    active_until=now + GROUP_WINDOW_SECONDS,
                    revision=1,
                    report_channel_id=trigger.report_channel_id,
                    report_ping_role_id=trigger.ping_role_id
                    if trigger.report_channel_id
                    else None,
                )
                session.add(incident)
                await session.flush()
            else:
                incident.active_until = max(
                    incident.active_until, now + GROUP_WINDOW_SECONDS
                )
                if trigger.report_channel_id and not incident.report_channel_id:
                    incident.report_channel_id = trigger.report_channel_id
                if (
                    trigger.ping_role_id
                    and trigger.report_channel_id
                    and not incident.report_ping_role_id
                ):
                    # Joining a report already posted: the next delivery pings
                    # the role after its edit (edits never ping).
                    incident.report_ping_role_id = trigger.ping_role_id
            existing_keys = (
                set(
                    (
                        await session.execute(
                            select(ModerationIncidentLink.link_key).where(
                                ModerationIncidentLink.guild_id == trigger.guild_id,
                                ModerationIncidentLink.link_key.in_(keys),
                            )
                        )
                    ).scalars()
                )
                if keys
                else set()
            )
            for key in keys:
                if key not in existing_keys:
                    session.add(
                        ModerationIncidentLink(
                            incident_id=incident.id,
                            guild_id=trigger.guild_id,
                            link_key=key,
                        )
                    )
            await self._add_trigger_entries(session, incident.id, trigger)
            await self._bump(session, incident.id)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            return incident.id

    async def _find_incident(
        self, session, trigger: t.Trigger, keys: list[str], now: float
    ) -> ModerationIncident | None:
        if keys:
            linked = (
                (
                    await session.execute(
                        select(ModerationIncident)
                        .join(
                            ModerationIncidentLink,
                            ModerationIncidentLink.incident_id == ModerationIncident.id,
                        )
                        .where(
                            ModerationIncidentLink.guild_id == trigger.guild_id,
                            ModerationIncidentLink.link_key.in_(keys),
                        )
                        .order_by(ModerationIncident.opened_at.desc())
                    )
                )
                .scalars()
                .first()
            )
            if linked is not None:
                return linked
        print_ = trigger.fingerprint
        if print_:
            return (
                (
                    await session.execute(
                        select(ModerationIncident)
                        .join(
                            ModerationIncidentEntry,
                            ModerationIncidentEntry.incident_id
                            == ModerationIncident.id,
                        )
                        .where(
                            ModerationIncidentEntry.guild_id == trigger.guild_id,
                            ModerationIncidentEntry.target_user_id
                            == str(trigger.author_id),
                            ModerationIncidentEntry.fingerprint == print_,
                            ModerationIncident.active_until > now,
                        )
                        .order_by(ModerationIncident.opened_at.desc())
                    )
                )
                .scalars()
                .first()
            )
        return None

    async def _add_trigger_entries(
        self, session, incident_id: UUID, trigger: t.Trigger
    ) -> None:
        already = set(
            (
                await session.execute(
                    select(
                        ModerationIncidentEntry.kind,
                        ModerationIncidentEntry.source,
                        ModerationIncidentEntry.rule,
                        ModerationIncidentEntry.message_id,
                    ).where(ModerationIncidentEntry.incident_id == incident_id)
                )
            ).all()
        )
        if (
            trigger.reason
            and (t.ENTRY_REASON, trigger.actor, trigger.reason, None) not in already
        ):
            session.add(
                ModerationIncidentEntry(
                    incident_id=incident_id,
                    guild_id=trigger.guild_id,
                    kind=t.ENTRY_REASON,
                    source=trigger.actor,
                    rule=trigger.reason,
                    target_user_id=trigger.author_id,
                    channel_id=trigger.channel_id,
                    # A post with no text has no evidence entry: the reason
                    # carries its fingerprint, so its copies still join.
                    fingerprint=trigger.fingerprint,
                )
            )
        seen_messages = {row[3] for row in already if row[0] == t.ENTRY_EVIDENCE}
        if (
            trigger.message_id
            and trigger.content
            and trigger.message_id not in seen_messages
        ):
            session.add(
                ModerationIncidentEntry(
                    incident_id=incident_id,
                    guild_id=trigger.guild_id,
                    kind=t.ENTRY_EVIDENCE,
                    source=trigger.actor,
                    target_user_id=trigger.author_id,
                    channel_id=trigger.channel_id,
                    message_id=trigger.message_id,
                    fingerprint=trigger.fingerprint,
                    content=redact_webhook_tokens(trigger.content),
                )
            )
        for evidence in trigger.evidence:
            if (
                evidence.kind == t.ENTRY_EVIDENCE
                and evidence.message_id
                and evidence.message_id in seen_messages
            ):
                continue
            is_note = evidence.kind in t.NOTE_KINDS
            session.add(
                ModerationIncidentEntry(
                    incident_id=incident_id,
                    guild_id=trigger.guild_id,
                    kind=evidence.kind,
                    source=trigger.actor,
                    rule=evidence.content if is_note else None,
                    content=None
                    if is_note or evidence.content is None
                    else redact_webhook_tokens(evidence.content),
                    target_user_id=evidence.author_id,
                    channel_id=evidence.channel_id,
                    message_id=evidence.message_id,
                    details=evidence.details,
                )
            )

    async def _bump(self, session, incident_id: UUID) -> None:
        await session.execute(
            update(ModerationIncident)
            .where(ModerationIncident.id == incident_id)
            .values(revision=ModerationIncident.revision + 1)
        )

    # ------------------------------------------------------------------
    # One proposal
    # ------------------------------------------------------------------

    async def _execute(
        self, incident_id: UUID, trigger: t.Trigger, proposal: t.Proposal
    ) -> t.Outcome:
        try:
            async with self.locks.hold(trigger.guild_id, proposal.target_user_id):
                await self._reconcile_target(trigger.guild_id, proposal.target_user_id)
                decision = await self._decide(incident_id, trigger, proposal)
                if decision.state is not None:
                    return await self._record_without_call(
                        incident_id, trigger, proposal, decision
                    )
                op_id, existing = await self._claim(
                    incident_id, trigger, proposal, decision
                )
                if existing is not None:
                    return existing
                return await self._perform(op_id, trigger, proposal, decision)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — a lock or database failure
            log_exception(
                logger,
                "Moderation gateway could not handle %s for %s",
                proposal.kind,
                proposal.target_user_id,
                level=logging.WARNING,
            )
            return t.Outcome(
                kind=proposal.kind,
                target_user_id=proposal.target_user_id,
                state=t.FAILED,
                detail=f"not done: the gateway failed ({type(error).__name__})",
                incident_id=incident_id,
            )

    async def _decide(
        self, incident_id: UUID, trigger: t.Trigger, proposal: t.Proposal
    ) -> _Decision:
        guild_id = trigger.guild_id
        target = proposal.target_user_id
        kind = proposal.kind
        manual = trigger.is_manual
        now = self._clock()

        if kind == t.TIMEOUT:
            seconds = int(proposal.duration_seconds or 0)
            if seconds <= 0 or seconds > MAX_TIMEOUT_SECONDS:
                return _Decision(state=t.FAILED, detail=f"invalid duration {seconds}s")

        if not manual and not proposal.unprotected_target and kind != t.UNTIMEOUT:
            refusal = await self.port.staff_refusal(
                guild_id, target, is_bot=proposal.target_is_bot
            )
            if refusal:
                return _Decision(state=t.SKIPPED, detail=f"protected: {refusal}")

        async with self._session() as session:
            incident = await session.get(ModerationIncident, incident_id)
            incident_ops = list(
                (
                    await session.execute(
                        select(ModerationOperation).where(
                            ModerationOperation.incident_id == incident_id,
                            ModerationOperation.target_user_id == target,
                        )
                    )
                ).scalars()
            )
            reversal = None
            if not manual and kind in t.SANCTION_RANK and incident is not None:
                reversal = (
                    (
                        await session.execute(
                            select(ModerationAction.action_type).where(
                                ModerationAction.guild_id == guild_id,
                                ModerationAction.target_user_id == target,
                                ModerationAction.source.in_(("manual", "audit_log")),
                                ModerationAction.action_type.in_(REVERSAL_ACTIONS),
                                ModerationAction.created_at >= incident.opened_at,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
        if reversal:
            return _Decision(
                state=t.SKIPPED,
                detail=f"a moderator reversed it ({reversal}); automation stands down",
            )

        if not manual and kind in t.SANCTION_RANK:
            live = [
                op
                for op in incident_ops
                if op.state in t.LIVE_STATES
                and op.kind in t.SANCTION_RANK
                and not (op.kind == t.TIMEOUT and op.released_at is not None)
                and not (op.requested or {}).get("provisional")
            ]
            strongest = max(live, key=lambda op: t.SANCTION_RANK[op.kind], default=None)
            if strongest is not None:
                rank = t.SANCTION_RANK[kind]
                their = t.SANCTION_RANK[strongest.kind]
                if their > rank:
                    return _Decision(
                        state=t.SKIPPED,
                        detail=f"weaker than the {strongest.kind} already in this incident",
                        superseded=True,
                    )
                if their == rank and kind != t.TIMEOUT:
                    return _Decision(
                        state=t.SKIPPED,
                        detail=f"already a {kind} in this incident",
                        superseded=True,
                    )

        if kind == t.BAN:
            if not manual and await self.port.is_banned(guild_id, target):
                return _Decision(state=t.SKIPPED, detail="already banned")
            return _Decision()
        if kind == t.KICK:
            state = await self.port.timeout_state(guild_id, target)
            if not state.is_member:
                return _Decision(state=t.SKIPPED, detail="not a member")
            return _Decision()
        if kind == t.TIMEOUT:
            return await self._decide_timeout(
                incident_id, trigger, proposal, now, incident_ops
            )
        if kind == t.UNTIMEOUT:
            return await self._decide_untimeout(trigger, proposal, now)
        if kind in t.CLEANUP_KINDS:
            return await self._decide_delete(trigger, proposal)
        if kind == t.WARN:
            return _Decision()
        return _Decision(state=t.FAILED, detail=f"unknown action {kind}")

    async def _automation_set(self, guild_id: str, target: str, until: float) -> bool:
        """Whether the timeout ending at ``until`` is one the automation set.

        Matched by the expiry Discord stored, which every confirmed timeout
        (and every release that shortened one) records. Not by recency: other
        writes touch operation rows, and the last one written need not be the
        one in place.
        """
        async with self._session() as session:
            match = (
                (
                    await session.execute(
                        select(ModerationOperation.id).where(
                            ModerationOperation.guild_id == guild_id,
                            ModerationOperation.target_user_id == target,
                            ModerationOperation.kind.in_((t.TIMEOUT, t.UNTIMEOUT)),
                            ModerationOperation.state == t.CONFIRMED,
                            ModerationOperation.actor != "manual",
                            ModerationOperation.expires_at
                            >= until - SAME_EXPIRY_SECONDS,
                            ModerationOperation.expires_at
                            <= until + SAME_EXPIRY_SECONDS,
                        )
                    )
                )
                .scalars()
                .first()
            )
        return match is not None

    async def _active_claims(
        self, guild_id: str, target: str, now: float
    ) -> list[ModerationOperation]:
        async with self._session() as session:
            return list(
                (
                    await session.execute(
                        select(ModerationOperation).where(
                            ModerationOperation.guild_id == guild_id,
                            ModerationOperation.target_user_id == target,
                            ModerationOperation.kind == t.TIMEOUT,
                            ModerationOperation.state.in_((t.CONFIRMED, t.COVERED)),
                            ModerationOperation.released_at.is_(None),
                            ModerationOperation.expires_at > now,
                            ModerationOperation.actor != "manual",
                        )
                    )
                ).scalars()
            )

    async def _decide_timeout(
        self,
        incident_id: UUID,
        trigger: t.Trigger,
        proposal: t.Proposal,
        now: float,
        incident_ops: Sequence[ModerationOperation] = (),
    ) -> _Decision:
        desired = now + int(proposal.duration_seconds or 0)
        state = await self.port.timeout_state(trigger.guild_id, proposal.target_user_id)
        if not state.is_member:
            return _Decision(state=t.SKIPPED, detail="not a member")
        if trigger.is_manual:
            return _Decision(until=desired)
        # An automated timeout's key is its claim's n-th request in the
        # incident, never its expiry: a repeat a moment later must be the same
        # request. Keys are chosen under the member's lock, so the count is
        # stable.
        prefix = _timeout_key_prefix(
            incident_id, proposal.target_user_id, proposal.claim
        )
        earlier = [op for op in incident_ops if op.idempotency_key.startswith(prefix)]
        for op in earlier:
            if op.released_at is not None:
                continue
            if op.state in (t.CLAIMED, t.UNKNOWN):
                # In flight, or unknown until reconciled: the same request,
                # answered with that request's outcome and never sent again.
                return _Decision(until=op.expires_at, key=op.idempotency_key)
            if (
                op.state == t.FAILED
                and (op.result or {}).get("reconciled")
                and not (op.requested or {}).get("retried")
                and op.expires_at is not None
                and op.expires_at >= desired - EXTEND_MIN_SECONDS
            ):
                # Reconciled as never applied: the one retry it allows (a
                # later repeat is a new request). It runs from now; the first
                # request's expiry may have passed.
                return _Decision(
                    until=max(op.expires_at, desired), key=op.idempotency_key
                )
        key = f"{prefix}{len(earlier)}"
        current = state.until if state.until and state.until > now else None
        if current is not None:
            if not await self._automation_set(
                trigger.guild_id, proposal.target_user_id, current
            ):
                return _Decision(
                    state=t.SKIPPED,
                    detail=(
                        f"a timeout someone else set runs until <t:{int(current)}:f>; "
                        "left as it is"
                    ),
                )
            if current >= desired - EXTEND_MIN_SECONDS:
                return _Decision(
                    state=t.COVERED,
                    detail=f"already timed out until <t:{int(current)}:f>",
                    until=desired,
                    in_place_until=current,
                )
        return _Decision(until=desired, key=key)

    async def _decide_untimeout(
        self, trigger: t.Trigger, proposal: t.Proposal, now: float
    ) -> _Decision:
        guild_id = trigger.guild_id
        target = proposal.target_user_id
        if trigger.is_manual:
            return _Decision(until=None)
        claims = await self._active_claims(guild_id, target, now)
        ending = [
            claim
            for claim in claims
            if proposal.claim is None or claim.claim_key == proposal.claim
        ]
        remaining = [claim for claim in claims if claim not in ending]
        released = tuple(claim.id for claim in ending)
        state = await self.port.timeout_state(guild_id, target)
        if not state.is_member:
            return _Decision(
                state=t.SKIPPED, detail="not a member", released_claims=released
            )
        current = state.until if state.until and state.until > now else None
        if current is None:
            return _Decision(
                state=t.SKIPPED, detail="no timeout in place", released_claims=released
            )
        if not await self._automation_set(guild_id, target, current):
            return _Decision(
                state=t.SKIPPED,
                detail="the timeout in place is someone else's; left as it is",
                released_claims=released,
            )
        keep_until = max((claim.expires_at for claim in remaining), default=None)
        if keep_until is not None and keep_until >= current - SAME_EXPIRY_SECONDS:
            return _Decision(
                state=t.SKIPPED,
                detail=f"another claim keeps it until <t:{int(keep_until)}:f>",
                released_claims=released,
            )
        return _Decision(until=keep_until, released_claims=released)

    async def _decide_delete(
        self, trigger: t.Trigger, proposal: t.Proposal
    ) -> _Decision:
        wanted = tuple(dict.fromkeys(str(m) for m in proposal.message_ids))
        if not wanted:
            return _Decision(state=t.SKIPPED, detail="no messages")
        async with self._session() as session:
            earlier = (
                await session.execute(
                    select(ModerationOperation).where(
                        ModerationOperation.guild_id == trigger.guild_id,
                        ModerationOperation.target_user_id == proposal.target_user_id,
                        ModerationOperation.kind.in_(tuple(t.CLEANUP_KINDS)),
                        ModerationOperation.state.in_(
                            (t.CLAIMED, t.CONFIRMED, t.ABSENT, t.UNKNOWN)
                        ),
                    )
                )
            ).scalars()
            handled: set[str] = set()
            for op in earlier:
                if op.state in (t.CLAIMED, t.UNKNOWN, t.ABSENT):
                    handled.update(op.message_ids or [])
                else:
                    result = op.result or {}
                    handled.update(result.get("deleted") or [])
                    handled.update(result.get("missing") or [])
        remaining = tuple(m for m in wanted if m not in handled)
        if not remaining:
            return _Decision(state=t.SKIPPED, detail="already deleted")
        return _Decision(message_ids=remaining)

    def _idempotency_key(
        self,
        incident_id: UUID,
        trigger: t.Trigger,
        proposal: t.Proposal,
        decision: _Decision,
    ) -> str:
        if decision.key:
            return decision.key
        target = proposal.target_user_id
        if proposal.kind in t.CLEANUP_KINDS:
            if len(decision.message_ids) == 1:
                return f"delete:{proposal.channel_id}:{decision.message_ids[0]}"
            return f"purge:{proposal.channel_id}:{uuid.uuid5(uuid.NAMESPACE_OID, ','.join(sorted(decision.message_ids)))}"
        if proposal.kind in (t.BAN, t.KICK, t.WARN) and not trigger.is_manual:
            return f"{proposal.kind}:{incident_id}:{target}"
        return f"{proposal.kind}:{target}:{uuid.uuid4()}"

    def _new_operation(
        self,
        incident_id: UUID,
        trigger: t.Trigger,
        proposal: t.Proposal,
        decision: _Decision,
        *,
        state: str,
        key: str,
    ) -> ModerationOperation:
        kind = proposal.kind
        if kind in t.CLEANUP_KINDS and not trigger.is_manual:
            kind = (
                t.DELETE
                if len(decision.message_ids or proposal.message_ids) == 1
                else t.PURGE
            )
        requested: dict[str, Any] = {}
        if proposal.duration_seconds is not None:
            requested["duration_seconds"] = int(proposal.duration_seconds)
        if proposal.kind in (t.TIMEOUT, t.UNTIMEOUT):
            requested["until"] = decision.until
        if proposal.delete_message_seconds:
            requested["delete_message_seconds"] = int(proposal.delete_message_seconds)
        if proposal.provisional:
            requested["provisional"] = True
        if proposal.dm_only:
            requested["dm_only"] = True
        result = (
            {"in_place_until": decision.in_place_until}
            if decision.in_place_until is not None
            else None
        )
        return ModerationOperation(
            id=uuid.uuid4(),
            incident_id=incident_id,
            guild_id=trigger.guild_id,
            target_user_id=proposal.target_user_id,
            target_username=proposal.target_username,
            kind=kind,
            idempotency_key=key,
            state=state,
            actor="manual" if trigger.is_manual else trigger.actor,
            reason=proposal.reason or trigger.reason,
            channel_id=proposal.channel_id or trigger.channel_id,
            message_ids=list(decision.message_ids or proposal.message_ids) or None,
            requested=requested or None,
            result=result,
            detail=decision.detail or None,
            claim_key=proposal.claim,
            expires_at=decision.until if proposal.kind == t.TIMEOUT else None,
            lease_until=self._clock() + OPERATION_LEASE_SECONDS
            if state == t.CLAIMED
            else None,
        )

    async def _release_claims(self, session, claim_ids: Sequence[UUID]) -> None:
        if claim_ids:
            await session.execute(
                update(ModerationOperation)
                .where(ModerationOperation.id.in_(list(claim_ids)))
                .values(released_at=self._clock())
            )

    async def _record_without_call(
        self,
        incident_id: UUID,
        trigger: t.Trigger,
        proposal: t.Proposal,
        decision: _Decision,
    ) -> t.Outcome:
        op = self._new_operation(
            incident_id,
            trigger,
            proposal,
            decision,
            state=decision.state or t.SKIPPED,
            key=f"{decision.state}:{proposal.kind}:{proposal.target_user_id}:{uuid.uuid4()}",
        )
        async with self._session() as session:
            session.add(op)
            await self._release_claims(session, decision.released_claims)
            await self._bump(session, incident_id)
            await session.commit()
        return dataclasses.replace(self._outcome(op), superseded=decision.superseded)

    async def _claim(
        self,
        incident_id: UUID,
        trigger: t.Trigger,
        proposal: t.Proposal,
        decision: _Decision,
    ) -> tuple[UUID, t.Outcome | None]:
        """Write the operation as claimed; or the outcome of its earlier twin."""
        key = self._idempotency_key(incident_id, trigger, proposal, decision)
        op = self._new_operation(
            incident_id, trigger, proposal, decision, state=t.CLAIMED, key=key
        )
        async with self._session() as session:
            session.add(op)
            # A release ends its claims whatever Discord answers: the caller
            # no longer wants the member held for them.
            await self._release_claims(session, decision.released_claims)
            try:
                await session.commit()
                return op.id, None
            except IntegrityError:
                await session.rollback()
            await self._release_claims(session, decision.released_claims)
            twin = (
                await session.execute(
                    select(ModerationOperation).where(
                        ModerationOperation.guild_id == trigger.guild_id,
                        ModerationOperation.idempotency_key == key,
                    )
                )
            ).scalar_one()
            if twin.state == t.FAILED and (twin.result or {}).get("reconciled"):
                # Checked with Discord and found never applied: this is the
                # retry the reconciliation allows.
                twin.state = t.CLAIMED
                twin.lease_until = self._clock() + OPERATION_LEASE_SECONDS
                twin.result = None
                twin.incident_id = incident_id
                twin.actor = op.actor
                twin.reason = op.reason
                twin.detail = None
                # The retry's own request, marked so it is the only one.
                twin.requested = {**(op.requested or {}), "retried": True}
                twin.expires_at = op.expires_at
                await session.commit()
                return twin.id, None
            await session.commit()
            return twin.id, self._outcome(
                twin, detail="duplicate of an earlier request"
            )

    async def _perform(
        self,
        op_id: UUID,
        trigger: t.Trigger,
        proposal: t.Proposal,
        decision: _Decision,
    ) -> t.Outcome:
        guild_id = trigger.guild_id
        target = proposal.target_user_id
        reason = (proposal.reason or trigger.reason or "")[:400]
        kind = proposal.kind
        try:
            result: dict[str, Any] = {}
            if kind == t.BAN:
                await self.port.ban(
                    guild_id,
                    target,
                    reason=reason,
                    delete_message_seconds=int(proposal.delete_message_seconds),
                )
            elif kind == t.KICK:
                await self.port.kick(guild_id, target, reason=reason)
            elif kind in (t.TIMEOUT, t.UNTIMEOUT):
                stored = await self.port.set_timeout(
                    guild_id,
                    target,
                    _now_dt(decision.until) if decision.until else None,
                    reason=reason,
                )
                result["until"] = stored if stored is not None else decision.until
            elif kind in t.CLEANUP_KINDS:
                deletion = await self.port.delete_messages(
                    str(proposal.channel_id), list(decision.message_ids)
                )
                result = {
                    "deleted": deletion.deleted,
                    "missing": deletion.missing,
                    "refused": deletion.refused,
                    "unknown": deletion.unknown,
                }
                if deletion.unknown:
                    return await self._settle_unknown(
                        op_id, "some deletes may not have landed", result=result
                    )
                if not deletion.deleted:
                    if deletion.refused:
                        return await self._settle_simple(
                            op_id,
                            t.FAILED,
                            "Discord refused (missing permission)",
                            result,
                        )
                    return await self._settle_simple(
                        op_id, t.ABSENT, "already gone", result
                    )
        except t.EffectNotFound as error:
            detail = {
                t.BAN: "the user does not exist",
                t.KICK: "not a member",
                t.TIMEOUT: "not a member",
                t.UNTIMEOUT: "not a member",
            }.get(kind, error.detail)
            return await self._settle_simple(op_id, t.FAILED, detail)
        except t.EffectRejected as error:
            return await self._settle_simple(op_id, t.FAILED, error.detail)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._settle_unknown(op_id, "interrupted after the call was sent")
            )
            raise
        except Exception as error:  # noqa: BLE001 — sent, outcome unknown
            return await self._settle_unknown(
                op_id, f"Discord did not confirm ({type(error).__name__})"
            )
        outcome, _ = await self._settle_confirmed(
            op_id,
            trigger,
            proposal,
            detail=decision.detail,
            result=result,
        )
        return outcome

    # ------------------------------------------------------------------
    # Settling
    # ------------------------------------------------------------------

    def _outcome(
        self, op: ModerationOperation, *, detail: str | None = None, action_id=None
    ) -> t.Outcome:
        result = op.result or {}
        return t.Outcome(
            kind=op.kind,
            target_user_id=op.target_user_id,
            state=op.state,
            detail=detail or op.detail or op.state,
            operation_id=op.id,
            action_id=action_id,
            incident_id=op.incident_id,
            expires_at=t.from_epoch(op.expires_at),
            deleted=tuple(result.get("deleted") or ()),
            missing=tuple(result.get("missing") or ()),
            in_place_until=t.from_epoch(result.get("in_place_until")),
        )

    async def _settle_simple(
        self, op_id: UUID, state: str, detail: str, result: dict | None = None
    ) -> t.Outcome:
        async with self._session() as session:
            op = await session.get(ModerationOperation, op_id)
            op.state = state
            op.detail = detail
            # An unknown outcome is checked once Discord has settled.
            op.lease_until = (
                self._clock() + RECONCILE_GRACE_SECONDS if state == t.UNKNOWN else None
            )
            if result is not None:
                op.result = result
            await self._bump(session, op.incident_id)
            await session.commit()
            return self._outcome(op)

    async def _settle_unknown(
        self, op_id: UUID, detail: str, *, result: dict | None = None
    ) -> t.Outcome:
        try:
            return await self._settle_simple(op_id, t.UNKNOWN, detail, result)
        except Exception:  # noqa: BLE001 — the lease runs out and it is reconciled
            log_exception(
                logger, "Could not record an unknown outcome", level=logging.WARNING
            )
            return t.Outcome(
                kind="",
                target_user_id="",
                state=t.UNKNOWN,
                detail=detail,
                operation_id=op_id,
            )

    async def _settle_confirmed(
        self,
        op_id: UUID,
        trigger: t.Trigger | None,
        proposal: t.Proposal | None,
        *,
        detail: str = "",
        result: dict | None = None,
        released: Sequence[UUID] = (),
    ) -> tuple[t.Outcome, ModerationAction | None]:
        """Confirm, write the action row and its announcement, in one commit."""
        bot_id, bot_name = (None, None)
        if trigger is None or not trigger.moderator_username:
            try:
                bot_id, bot_name = await self.port.bot_identity()
            except Exception:  # noqa: BLE001 — the row is worth more than the name
                bot_id, bot_name = None, None
        now = self._clock()
        async with self._session() as session:
            op = await session.get(ModerationOperation, op_id)
            incident = await session.get(ModerationIncident, op.incident_id)
            result = dict(result or op.result or {})
            op.state = t.CONFIRMED
            op.lease_until = None
            op.result = result
            if detail:
                op.detail = detail
            if op.kind in (t.TIMEOUT, t.UNTIMEOUT):
                op.expires_at = result.get("until")
            await self._release_claims(session, released)
            action = self._action_row(
                op, incident, trigger, proposal, bot_id, bot_name, now
            )
            session.add(action)
            await session.flush()
            session.add(
                ModerationOutbox(
                    incident_id=op.incident_id,
                    guild_id=op.guild_id,
                    kind="announce",
                    dedupe_key=f"announce:{action.id}",
                    target_user_id=op.target_user_id,
                    payload={"action_id": str(action.id)},
                    state="pending",
                )
            )
            await self._bump(session, op.incident_id)
            await session.commit()
            return self._outcome(op, action_id=action.id), action

    def _action_row(
        self,
        op: ModerationOperation,
        incident: ModerationIncident | None,
        trigger: t.Trigger | None,
        proposal: t.Proposal | None,
        bot_id: str | None,
        bot_name: str | None,
        now: float,
    ) -> ModerationAction:
        result = op.result or {}
        requested = op.requested or {}
        action_type = op.kind
        if op.kind == t.UNTIMEOUT and result.get("until"):
            # Shortened to another claim rather than lifted.
            action_type = t.TIMEOUT
        applied = None
        if op.kind in (t.TIMEOUT, t.UNTIMEOUT) and result.get("until"):
            applied = max(0, int(result["until"] - now))
        evidence: dict[str, Any] = {}
        if result.get("deleted"):
            evidence["deleted_message_ids"] = list(result["deleted"])
        if result.get("until"):
            evidence["expires_at"] = _now_dt(result["until"]).isoformat()
        if incident is not None and incident.trigger_message_id:
            evidence["trigger_message_id"] = incident.trigger_message_id
        moderator_id = trigger.moderator_user_id if trigger else None
        moderator_name = trigger.moderator_username if trigger else None
        if not moderator_name:
            moderator_id, moderator_name = bot_id, bot_name
        source = (
            trigger.source
            if trigger
            else (t.SOURCE_MANUAL if op.actor == "manual" else t.SOURCE_HANDLER)
        )
        reason = op.reason
        if (
            op.kind in t.CLEANUP_KINDS
            and op.actor != "manual"
            and result.get("deleted")
            and len(op.message_ids or []) > 1
        ):
            reason = (
                f"{reason or ''} ({len(result['deleted'])} message(s) deleted)".strip()
            )
        return ModerationAction(
            guild_id=op.guild_id,
            target_user_id=op.target_user_id,
            target_username=op.target_username or op.target_user_id,
            moderator_user_id=moderator_id,
            moderator_username=moderator_name,
            action_type=action_type,
            reason=reason,
            duration_seconds=requested.get("duration_seconds")
            if op.kind == t.TIMEOUT
            else (applied if action_type == t.TIMEOUT else None),
            source=source,
            channel_id=op.channel_id,
            trigger_message_id=(trigger.message_id if trigger else None)
            or (incident.trigger_message_id if incident else None),
            ai_context_summary=proposal.ai_context_summary if proposal else None,
            incident_id=op.incident_id,
            operation_id=op.id,
            actor=op.actor,
            rule=trigger.reason if trigger else None,
            applied_duration_seconds=applied,
            evidence=evidence or None,
            created_at=_now_dt(now),
        )

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def _due_for_reconcile(self, now: float):
        return ModerationOperation.state.in_((t.UNKNOWN, t.CLAIMED)) & or_(
            ModerationOperation.lease_until.is_(None),
            ModerationOperation.lease_until < now,
        )

    async def _reconcile_target(self, guild_id: str, target: str) -> int:
        """Settle this member's unknown and interrupted operations; how many."""
        now = self._clock()
        async with self._session() as session:
            due = list(
                (
                    await session.execute(
                        select(ModerationOperation).where(
                            ModerationOperation.guild_id == guild_id,
                            ModerationOperation.target_user_id == target,
                            self._due_for_reconcile(now),
                        )
                    )
                ).scalars()
            )
        settled = 0
        for op in due:
            try:
                if await self._reconcile_one(op):
                    settled += 1
            except Exception:  # noqa: BLE001 — Discord unreadable: try again later
                log_exception(
                    logger,
                    "Could not reconcile moderation operation %s",
                    op.id,
                    level=logging.WARNING,
                )
        return settled

    async def _reconcile_one(self, op: ModerationOperation) -> bool:
        guild_id = op.guild_id
        target = op.target_user_id
        requested = op.requested or {}
        if op.kind == t.WARN:
            await self._settle_simple(op.id, "unresolved", UNRESOLVED_DETAIL)
            return True
        if op.kind == t.BAN:
            applied = await self.port.is_banned(guild_id, target)
            result: dict[str, Any] = {}
        elif op.kind == t.KICK:
            state = await self.port.timeout_state(guild_id, target)
            if not state.is_member:
                # The kick was sent and the member is gone: recorded as the
                # kick it most likely was, so the history and the audit echo
                # have it.
                applied, result = True, {}
            else:
                applied, result = False, {}
        elif op.kind in (t.TIMEOUT, t.UNTIMEOUT):
            state = await self.port.timeout_state(guild_id, target)
            wanted = requested.get("until")
            current = (
                state.until if state.until and state.until > self._clock() else None
            )
            if wanted is None:
                applied = state.is_member and current is None
            else:
                applied = (
                    current is not None and abs(current - wanted) <= SAME_EXPIRY_SECONDS
                )
            result = {"until": current}
        else:
            prior = op.result or {}
            done = set(prior.get("deleted") or ()) | set(prior.get("missing") or ())
            gone = list(prior.get("deleted") or ())
            for message_id in op.message_ids or ():
                if message_id in done:
                    continue
                if not await self.port.message_exists(str(op.channel_id), message_id):
                    gone.append(message_id)
            applied = bool(gone)
            result = {"deleted": gone, "missing": list(prior.get("missing") or ())}
        if applied:
            await self._settle_confirmed(
                op.id,
                None,
                None,
                detail="confirmed by checking Discord",
                result=result,
            )
        else:
            await self._settle_simple(
                op.id,
                t.FAILED,
                "not applied (checked with Discord)",
                {"reconciled": True, **result},
            )
        return True

    # ------------------------------------------------------------------
    # Delivery: announcements and the report
    # ------------------------------------------------------------------

    async def _after(self, incident_id: UUID | None) -> None:
        if incident_id is None:
            return
        try:
            await self.deliver_outbox(incident_id)
        except Exception:  # noqa: BLE001 — observers never break an action
            log_exception(
                logger, "Moderation announcements failed", level=logging.WARNING
            )
        try:
            await self.deliver_report(incident_id)
        except Exception:  # noqa: BLE001 — the records hold it; the next change retries
            log_exception(
                logger, "Moderation report delivery failed", level=logging.WARNING
            )

    async def deliver_outbox(self, incident_id: UUID | None = None) -> int:
        """Deliver pending announcements once each; returns how many went out."""
        async with self._session() as session:
            query = select(ModerationOutbox.id).where(
                ModerationOutbox.state == "pending"
            )
            if incident_id is not None:
                query = query.where(ModerationOutbox.incident_id == incident_id)
            pending = list((await session.execute(query)).scalars())
        sent = 0
        for row_id in pending:
            async with self._session() as session:
                taken = await session.execute(
                    update(ModerationOutbox)
                    .where(
                        ModerationOutbox.id == row_id,
                        ModerationOutbox.state == "pending",
                    )
                    .values(state="sending", lease_until=self._clock() + 60)
                )
                await session.commit()
                if taken.rowcount != 1:
                    continue
                row = await session.get(ModerationOutbox, row_id)
                action = None
                if row.kind == "announce":
                    action_id = (row.payload or {}).get("action_id")
                    action = (
                        await session.get(ModerationAction, UUID(action_id))
                        if action_id
                        else None
                    )
            state, detail = "sent", None
            try:
                if action is not None:
                    await self.port.announce(action)
                    sent += 1
                else:
                    state, detail = "failed", "nothing to announce"
            except Exception as error:  # noqa: BLE001 — may have gone out; not resent
                state, detail = "unknown", type(error).__name__
            async with self._session() as session:
                await session.execute(
                    update(ModerationOutbox)
                    .where(ModerationOutbox.id == row_id)
                    .values(
                        state=state,
                        detail=detail,
                        payload=None,
                        lease_until=None,
                        settled_at=_now_dt(self._clock()),
                    )
                )
                await session.commit()
        return sent

    async def _report_facts(
        self, session, incident: ModerationIncident
    ) -> tuple[report_mod.ReportFacts, tuple[str, bytes] | None]:
        entries = list(
            (
                await session.execute(
                    select(ModerationIncidentEntry)
                    .where(ModerationIncidentEntry.incident_id == incident.id)
                    .order_by(ModerationIncidentEntry.created_at)
                )
            ).scalars()
        )
        operations = list(
            (
                await session.execute(
                    select(ModerationOperation)
                    .where(ModerationOperation.incident_id == incident.id)
                    .order_by(ModerationOperation.created_at)
                )
            ).scalars()
        )
        transcript = next(
            (
                entry
                for entry in entries
                if entry.kind == t.ENTRY_TRANSCRIPT and entry.content
            ),
            None,
        )
        attachment = None
        if transcript is not None and not incident.report_attachment_sent:
            filename = (transcript.details or {}).get("filename") or "transcript.txt"
            attachment = (filename, transcript.content.encode("utf-8"))
        facts = report_mod.ReportFacts(
            incident_id=incident.id,
            guild_id=incident.guild_id,
            subject_user_id=incident.subject_user_id,
            channel_id=incident.channel_id,
            trigger_message_id=incident.trigger_message_id,
            opened_at=incident.opened_at.timestamp(),
            ping_role_id=incident.report_ping_role_id,
            entries=tuple(
                report_mod.ReportEntry(
                    kind=entry.kind,
                    source=entry.source,
                    rule=entry.rule,
                    content=entry.content,
                    target_user_id=entry.target_user_id,
                    channel_id=entry.channel_id,
                    message_id=entry.message_id,
                    at=_epoch_of(entry.created_at),
                    details=entry.details or None,
                )
                for entry in entries
                if entry.kind != t.ENTRY_TRANSCRIPT
            ),
            operations=tuple(
                report_mod.ReportOperation(
                    kind=op.kind,
                    state=op.state,
                    actor=op.actor,
                    target_user_id=op.target_user_id,
                    target_username=op.target_username,
                    detail=op.detail,
                    channel_id=op.channel_id,
                    message_ids=tuple(op.message_ids or ()),
                    deleted=tuple((op.result or {}).get("deleted") or ()),
                    duration_seconds=(op.requested or {}).get("duration_seconds"),
                    expires_at=op.expires_at,
                    at=_epoch_of(op.created_at),
                    reason=op.reason,
                    provisional=bool((op.requested or {}).get("provisional")),
                )
                for op in operations
                if not (op.state == t.SKIPPED and op.detail == "already deleted")
            ),
            has_transcript=transcript is not None,
        )
        return facts, attachment

    async def _deliver_report_once(self, incident_id: UUID) -> bool:
        """One post or edit; True when another pass is needed."""
        now = self._clock()
        async with self._session() as session:
            incident = await session.get(ModerationIncident, incident_id)
            if incident is None or not incident.report_channel_id:
                return False
            if incident.report_state == "failed":
                return False
            if (
                incident.report_state == "posted"
                and incident.report_revision >= incident.revision
                and not (incident.report_ping_role_id and not incident.report_pinged)
            ):
                return False
            taken = await session.execute(
                update(ModerationIncident)
                .where(
                    ModerationIncident.id == incident_id,
                    or_(
                        ModerationIncident.report_lease_until.is_(None),
                        ModerationIncident.report_lease_until < now,
                    ),
                )
                .values(report_lease_until=now + REPORT_LEASE_SECONDS)
            )
            await session.commit()
            if taken.rowcount != 1:
                # Another delivery holds it; it loops until it shows our revision.
                return False
            await session.refresh(incident)
            revision = incident.revision
            facts, attachment = await self._report_facts(session, incident)
            channel_id = incident.report_channel_id
            message_id = incident.report_message_id
            state = incident.report_state
            pinged = incident.report_pinged
        content = report_mod.render_report(facts)
        new_state = "posted"
        editing = False
        attachment_sent = attachment is not None
        try:
            if message_id is None and state in ("posting", "unknown"):
                message_id = await self.port.find_message(
                    channel_id, report_mod.marker(incident_id)
                )
            if message_id is None:
                # A post carries the ping itself; one whose answer is lost and
                # is found later by its marker must not be pinged again.
                await self._set_report(
                    incident_id,
                    report_state="posting",
                    report_pinged=bool(facts.ping_role_id) or pinged,
                )
                message_id = await self.port.send_message(
                    channel_id,
                    content,
                    role_mention_ids=[facts.ping_role_id]
                    if facts.ping_role_id
                    else None,
                    attachment=attachment,
                )
            else:
                editing = True
                await self.port.edit_message(
                    channel_id, message_id, content, attachment=attachment
                )
        except t.EffectNotFound:
            if not editing:
                # The channel itself is gone or unreadable.
                await self._set_report(
                    incident_id,
                    report_state="failed",
                    report_revision=revision,
                    report_lease_until=None,
                )
                return False
            # Somebody deleted the report: the incident gets a new one.
            logger.info(
                "Moderation report for incident %s was deleted; posting anew",
                incident_id,
            )
            await self._set_report(
                incident_id,
                report_message_id=None,
                report_state="none",
                report_attachment_sent=False,
                report_lease_until=None,
            )
            return True
        except t.EffectRejected as error:
            logger.warning(
                "Cannot post the moderation report for incident %s to channel %s: %s",
                incident_id,
                channel_id,
                error.detail,
            )
            await self._set_report(
                incident_id,
                report_message_id=message_id,
                report_state="failed" if message_id is None else "posted",
                report_revision=revision,
                report_lease_until=None,
            )
            return False
        except Exception:  # noqa: BLE001 — may have posted; reconciled by marker
            log_exception(
                logger,
                "Moderation report for incident %s may not have posted",
                incident_id,
                level=logging.WARNING,
            )
            # Keep an id already known (found by its marker): the next pass
            # edits it instead of posting a second report.
            await self._set_report(
                incident_id,
                report_message_id=message_id,
                report_state="unknown" if message_id is None else "posted",
                report_lease_until=None,
            )
            return False
        values: dict[str, Any] = {
            "report_message_id": message_id,
            "report_state": new_state,
            "report_revision": revision,
            "report_lease_until": None,
        }
        if attachment_sent:
            values["report_attachment_sent"] = True
        await self._set_report(incident_id, **values)
        if facts.ping_role_id and editing and not pinged:
            await self._ping_after_edit(incident_id, facts, channel_id, message_id)
        async with self._session() as session:
            incident = await session.get(ModerationIncident, incident_id)
            return incident is not None and incident.revision > revision

    async def _ping_after_edit(
        self,
        incident_id: UUID,
        facts: report_mod.ReportFacts,
        channel_id: str,
        message_id: str,
    ) -> None:
        """Ping the role a detector asked for once the report already existed.

        A short message pointing at the report. It does not carry the marker,
        so it is never mistaken for the report. Sent once: a refusal marks it
        done too, and a lost response may at worst ping twice.
        """
        link = report_mod.jump_link(facts.guild_id, channel_id, message_id)
        try:
            await self.port.send_message(
                channel_id,
                f"<@&{facts.ping_role_id}> A moderation incident needs you: {link}",
                role_mention_ids=[facts.ping_role_id],
            )
        except t.EffectRejected as error:
            logger.warning(
                "Cannot ping for the moderation report of incident %s: %s",
                incident_id,
                error.detail,
            )
        except Exception:  # noqa: BLE001 — the next delivery tries again
            log_exception(
                logger,
                "Ping for the moderation report of incident %s may not have gone out",
                incident_id,
                level=logging.WARNING,
            )
            return
        await self._set_report(incident_id, report_pinged=True)

    async def _set_report(self, incident_id: UUID, **values: Any) -> None:
        async with self._session() as session:
            await session.execute(
                update(ModerationIncident)
                .where(ModerationIncident.id == incident_id)
                .values(**values)
            )
            await session.commit()


#: How far back an audit-log entry of the bot's looks for the operation it echoes.
AUDIT_LINK_WINDOW_SECONDS = 600.0


async def link_audit_entry(
    *,
    guild_id: str,
    target_user_id: str,
    kinds: Sequence[str],
    audit_entry_id: str,
    session_factory: Callable[[], Any] | None = None,
    now: float | None = None,
) -> bool:
    """Tie a Discord audit-log entry of the bot's own to the operation it echoes.

    The audit logger sees every ban, kick and timeout, the bot's included. An
    entry by the bot is the echo of a gateway operation: it is linked to the
    newest confirmed one of that kind on that member (and to its
    ``moderation_actions`` row), never recorded a second time. False when no
    operation matches, for instance an action the bot took before the gateway
    existed.
    """
    factory = session_factory or default_session_factory()
    moment = time.time() if now is None else now
    async with factory() as session:
        already = (
            (
                await session.execute(
                    select(ModerationOperation.id).where(
                        ModerationOperation.guild_id == str(guild_id),
                        ModerationOperation.audit_entry_id == str(audit_entry_id),
                    )
                )
            )
            .scalars()
            .first()
        )
        if already is not None:
            return True
        candidates = (
            await session.execute(
                select(ModerationOperation)
                .where(
                    ModerationOperation.guild_id == str(guild_id),
                    ModerationOperation.target_user_id == str(target_user_id),
                    ModerationOperation.kind.in_(list(kinds)),
                    ModerationOperation.state == t.CONFIRMED,
                    ModerationOperation.audit_entry_id.is_(None),
                )
                .order_by(ModerationOperation.updated_at.desc())
            )
        ).scalars()
        operation = next(
            (
                op
                for op in candidates
                if op.updated_at is None
                or moment - op.updated_at.timestamp() <= AUDIT_LINK_WINDOW_SECONDS
            ),
            None,
        )
        if operation is None:
            return False
        operation.audit_entry_id = str(audit_entry_id)
        await session.execute(
            update(ModerationAction)
            .where(ModerationAction.operation_id == operation.id)
            .values(audit_entry_id=str(audit_entry_id))
        )
        await session.commit()
        return True
