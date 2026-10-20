"""How long the gateway's records keep what members wrote, and a deletion request."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from smarter_dev.shared.retention_policy import MODERATION_TRANSCRIPT_MAX
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationIncident
from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.models import ModerationMemberLock
from smarter_dev.web.models import ModerationOperation
from smarter_dev.web.models import ModerationOutbox
from smarter_dev.web.moderation_gateway.privacy import delete_member_moderation_content
from smarter_dev.web.retention import SCRUBBERS
from smarter_dev.web.retention import run_retention_sweep

NOW = datetime(2026, 10, 19, 21, 0, tzinfo=UTC)
GUILD = "1"
PERSON = "123456789012345678"
OTHER = "223456789012345678"


def _incident() -> ModerationIncident:
    return ModerationIncident(
        id=uuid4(),
        guild_id=GUILD,
        subject_user_id=PERSON,
        opened_at=NOW - timedelta(days=100),
        active_until=0.0,
    )


def _entry(incident, kind: str, age: timedelta, **fields) -> ModerationIncidentEntry:
    values = {
        "incident_id": incident.id,
        "guild_id": GUILD,
        "kind": kind,
        "source": "spam_engine",
        "created_at": NOW - age,
    }
    values.update(fields)
    return ModerationIncidentEntry(**values)


async def _kinds(session) -> list[tuple[str, str | None]]:
    rows = (await session.execute(select(ModerationIncidentEntry))).scalars().all()
    return sorted((row.kind, row.content or row.rule) for row in rows)


def test_the_sweep_covers_the_new_tables():
    assert {
        "moderation_incident_entries",
        "moderation_outbox",
        "moderation_member_locks",
    } <= set(SCRUBBERS)
    assert MODERATION_TRANSCRIPT_MAX == timedelta(days=90)


async def test_evidence_goes_after_90_days_assessments_after_5_hours(db_session):
    incident = _incident()
    db_session.add(incident)
    db_session.add_all(
        [
            _entry(
                incident, "evidence", timedelta(days=90, minutes=1), content="old scam"
            ),
            _entry(incident, "evidence", timedelta(days=89), content="recent scam"),
            _entry(incident, "transcript", timedelta(days=91), content="old ring"),
            _entry(incident, "assessment", timedelta(hours=6), content="model said"),
            _entry(
                incident, "assessment", timedelta(hours=1), content="model just said"
            ),
            _entry(incident, "reason", timedelta(days=400), rule="mass mention"),
            _entry(incident, "note", timedelta(days=400), rule="flagged for review"),
        ]
    )
    await db_session.commit()

    result = await run_retention_sweep(db_session, now=NOW)

    assert result.counts["moderation_incident_entries"] == 3
    assert await _kinds(db_session) == [
        ("assessment", "model just said"),
        ("evidence", "recent scam"),
        ("note", "flagged for review"),
        ("reason", "mass mention"),
    ]
    # The incident itself is a moderation record and stays.
    assert (await db_session.execute(select(ModerationIncident))).scalars().all()


async def test_outbox_payloads_and_old_rows_and_dead_leases_go(db_session):
    incident = _incident()
    db_session.add(incident)
    db_session.add_all(
        [
            ModerationOutbox(
                incident_id=incident.id,
                guild_id=GUILD,
                kind="announce",
                dedupe_key="a",
                payload={"action_id": "x"},
                state="pending",
                created_at=NOW - timedelta(hours=6),
            ),
            ModerationOutbox(
                incident_id=incident.id,
                guild_id=GUILD,
                kind="announce",
                dedupe_key="settled",
                payload=None,
                state="sent",
                created_at=NOW - timedelta(hours=6),
            ),
            ModerationOutbox(
                incident_id=incident.id,
                guild_id=GUILD,
                kind="announce",
                dedupe_key="b",
                payload=None,
                state="sent",
                created_at=NOW - timedelta(days=31),
            ),
            ModerationMemberLock(
                guild_id=GUILD,
                user_id=PERSON,
                token="t",
                expires_at=NOW.timestamp() - 1,
            ),
            ModerationMemberLock(
                guild_id=GUILD,
                user_id=OTHER,
                token="t",
                expires_at=NOW.timestamp() + 60,
            ),
        ]
    )
    await db_session.commit()

    await run_retention_sweep(db_session, now=NOW)

    outbox = (await db_session.execute(select(ModerationOutbox))).scalars().all()
    # A stale undelivered row loses its payload; a delivered one is left as
    # it is; a month-old one goes.
    assert sorted((row.dedupe_key, row.payload, row.state) for row in outbox) == [
        ("a", None, "unknown"),
        ("settled", None, "sent"),
    ]
    locks = (await db_session.execute(select(ModerationMemberLock))).scalars().all()
    assert [lock.user_id for lock in locks] == [OTHER]


async def test_a_deletion_request_removes_their_content_and_keeps_the_history(
    db_session,
):
    incident = _incident()
    db_session.add(incident)
    await db_session.flush()
    operation = ModerationOperation(
        incident_id=incident.id,
        guild_id=GUILD,
        target_user_id=PERSON,
        kind="ban",
        idempotency_key="ban:x",
        state="confirmed",
        actor="spam_engine",
    )
    db_session.add(operation)
    db_session.add_all(
        [
            _entry(
                incident,
                "evidence",
                timedelta(hours=1),
                target_user_id=PERSON,
                content="their scam",
            ),
            _entry(
                incident,
                "evidence",
                timedelta(hours=1),
                target_user_id=OTHER,
                content="someone else",
            ),
            _entry(
                incident,
                "transcript",
                timedelta(hours=1),
                content=f"[{PERSON}] hi\n[{OTHER}] yo",
            ),
            _entry(
                incident,
                "transcript",
                timedelta(hours=1),
                content=f"[{OTHER}] only them, not {PERSON}9",
            ),
            _entry(
                incident,
                "assessment",
                timedelta(hours=1),
                content=f"<@{PERSON}> looks compromised",
            ),
            _entry(
                incident,
                "reason",
                timedelta(hours=1),
                target_user_id=PERSON,
                rule="mass mention",
            ),
            _entry(
                incident,
                "note",
                timedelta(hours=1),
                rule=f"Flagged for review: <@{PERSON}>",
            ),
            ModerationOutbox(
                incident_id=incident.id,
                guild_id=GUILD,
                kind="announce",
                dedupe_key="c",
                target_user_id=PERSON,
                state="pending",
            ),
            ModerationOutbox(
                incident_id=incident.id,
                guild_id=GUILD,
                kind="announce",
                dedupe_key="d",
                target_user_id=OTHER,
                state="pending",
            ),
            ModerationAction(
                guild_id=GUILD,
                target_user_id=PERSON,
                target_username="them",
                action_type="ban",
                source="handler",
                incident_id=incident.id,
            ),
        ]
    )
    await db_session.commit()

    deleted = await delete_member_moderation_content(db_session, PERSON)
    await db_session.commit()

    assert (deleted.entries, deleted.outbox) == (3, 1)
    assert await _kinds(db_session) == [
        ("evidence", "someone else"),
        ("note", f"Flagged for review: <@{PERSON}>"),
        ("reason", "mass mention"),
        ("transcript", f"[{OTHER}] only them, not {PERSON}9"),
    ]
    outbox = (await db_session.execute(select(ModerationOutbox))).scalars().all()
    assert [row.target_user_id for row in outbox] == [OTHER]
    # Moderation history stays, as the privacy notice says.
    assert (await db_session.execute(select(ModerationIncident))).scalars().all()
    assert (await db_session.execute(select(ModerationOperation))).scalars().all()
    assert (await db_session.execute(select(ModerationAction))).scalars().all()
