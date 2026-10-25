"""Incidents as moderators read them in the admin panel (#241).

One incident is everything about one situation: what each detector asked
for (``moderation_operations``), what Discord confirmed (the
``moderation_actions`` rows those operations wrote), what is still unknown,
what the member was told (``moderation_outbox``), and the one report in the
moderators' channel the incident owns. This module reads them; it never
changes anything.

Outcomes are counted per operation:

* **requested**: every operation the gateway recorded;
* **confirmed**: Discord confirmed it (``confirmed``), or a longer timeout
  already held the member (``covered``);
* **not applied**: Discord refused it (``failed``), the message was already
  gone (``absent``), or it was never sent (``skipped``: protected, weaker,
  overridden);
* **unknown**: it may have landed (``unknown``), or a claim is still out
  (``claimed``); reconciliation reads Discord before anything is resent.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from dataclasses import field
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy import select

from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationIncident
from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.models import ModerationOperation
from smarter_dev.web.models import ModerationOutbox
from smarter_dev.web.moderation_gateway import types as gw

CONFIRMED = "confirmed"
NOT_APPLIED = "not_applied"
UNKNOWN = "unknown"
OUTCOME_OF = {
    gw.CONFIRMED: CONFIRMED,
    gw.COVERED: CONFIRMED,
    gw.FAILED: NOT_APPLIED,
    gw.ABSENT: NOT_APPLIED,
    gw.SKIPPED: NOT_APPLIED,
    gw.UNKNOWN: UNKNOWN,
    gw.CLAIMED: UNKNOWN,
}
#: Entries whose text is the system's own words, safe to show in a list.
#: A deletion summary is a model's, but written to be shown (defanged, no
#: slurs quoted) and kept with the incident (#378).
SYSTEM_ENTRY_KINDS = (
    gw.ENTRY_REASON,
    gw.ENTRY_NOTE,
    gw.ENTRY_ALERT,
    gw.ENTRY_DELIVERY,
    gw.ENTRY_HISTORY,
    gw.ENTRY_SUMMARY,
    gw.ENTRY_DELETED_SUMMARY,
)
LIST_LIMIT = 50


def outcome(state: str) -> str:
    return OUTCOME_OF.get(state, UNKNOWN)


def report_url(incident: ModerationIncident) -> str | None:
    """The incident's one report in the moderators' channel, when posted."""
    if not incident.report_channel_id or not incident.report_message_id:
        return None
    return (
        f"https://discord.com/channels/{incident.guild_id}/"
        f"{incident.report_channel_id}/{incident.report_message_id}"
    )


@dataclass(slots=True)
class IncidentSummary:
    incident: ModerationIncident
    requested: int = 0
    counts: Counter = field(default_factory=Counter)
    #: The effects asked for, e.g. ``timeout``, ``delete``; each once.
    kinds: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    summary: str | None = None

    @property
    def report_url(self) -> str | None:
        return report_url(self.incident)

    @property
    def short_id(self) -> str:
        return str(self.incident.id)[:8]


@dataclass(slots=True)
class IncidentDetail:
    summary: IncidentSummary
    operations: list[ModerationOperation]
    entries: list[ModerationIncidentEntry]
    outbox: list[ModerationOutbox]
    actions: list[ModerationAction]


async def list_incidents(
    session, guild_id: str, *, user_id: str | None = None, limit: int = LIST_LIMIT
) -> list[IncidentSummary]:
    """The guild's latest incidents, newest first; ``user_id``: those about
    that member (its subject, or a target of one of its effects)."""
    query = select(ModerationIncident).where(ModerationIncident.guild_id == guild_id)
    if user_id is not None:
        targeted = select(ModerationOperation.incident_id).where(
            ModerationOperation.guild_id == guild_id,
            ModerationOperation.target_user_id == user_id,
        )
        query = query.where(
            or_(
                ModerationIncident.subject_user_id == user_id,
                ModerationIncident.id.in_(targeted),
            )
        )
    incidents = list(
        (
            await session.execute(
                query.order_by(ModerationIncident.opened_at.desc()).limit(limit)
            )
        ).scalars()
    )
    return await _summaries(session, incidents)


async def _summaries(session, incidents) -> list[IncidentSummary]:
    if not incidents:
        return []
    ids = [incident.id for incident in incidents]
    summaries = {incident.id: IncidentSummary(incident) for incident in incidents}
    operations = (
        await session.execute(
            select(ModerationOperation)
            .where(ModerationOperation.incident_id.in_(ids))
            .order_by(ModerationOperation.created_at)
        )
    ).scalars()
    for op in operations:
        item = summaries[op.incident_id]
        item.requested += 1
        item.counts[outcome(op.state)] += 1
        if op.kind not in item.kinds:
            item.kinds.append(op.kind)
        if op.target_user_id not in item.targets:
            item.targets.append(op.target_user_id)
    leads = (
        await session.execute(
            select(ModerationIncidentEntry)
            .where(
                ModerationIncidentEntry.incident_id.in_(ids),
                ModerationIncidentEntry.kind == gw.ENTRY_SUMMARY,
            )
            .order_by(ModerationIncidentEntry.created_at)
        )
    ).scalars()
    for entry in leads:
        summaries[entry.incident_id].summary = entry.rule or entry.content
    return [summaries[incident.id] for incident in incidents]


async def incident_detail(
    session, guild_id: str, incident_id: UUID
) -> IncidentDetail | None:
    incident = await session.get(ModerationIncident, incident_id)
    if incident is None or incident.guild_id != guild_id:
        return None
    [summary] = await _summaries(session, [incident])

    async def rows(model, order):
        return list(
            (
                await session.execute(
                    select(model)
                    .where(model.incident_id == incident_id)
                    .order_by(order)
                )
            ).scalars()
        )

    return IncidentDetail(
        summary=summary,
        operations=await rows(ModerationOperation, ModerationOperation.created_at),
        entries=await rows(ModerationIncidentEntry, ModerationIncidentEntry.created_at),
        outbox=await rows(ModerationOutbox, ModerationOutbox.created_at),
        actions=await rows(ModerationAction, ModerationAction.created_at),
    )
