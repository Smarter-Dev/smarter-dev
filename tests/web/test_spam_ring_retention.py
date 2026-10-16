"""``spam_ring_incidents`` is deleted by the hourly sweep after 90 days."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy import select

from smarter_dev.shared.retention_policy import MODERATION_TRANSCRIPT_MAX
from smarter_dev.web.models import SpamRingIncident
from smarter_dev.web.retention import SCRUBBERS
from smarter_dev.web.retention import run_retention_sweep

NOW = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)


def _incident(age: timedelta) -> SpamRingIncident:
    return SpamRingIncident(
        guild_id="1",
        channel_id="2",
        trigger_message_id=str(int(age.total_seconds())),
        participant_user_ids=["3"],
        transcript="the ad text",
        created_at=NOW - age,
    )


def test_the_horizon_is_90_days():
    assert MODERATION_TRANSCRIPT_MAX == timedelta(days=90)
    assert "spam_ring_incidents" in SCRUBBERS


async def test_the_sweep_deletes_transcripts_past_90_days_and_keeps_the_rest(
    db_session,
):
    db_session.add_all(
        [
            _incident(timedelta(days=90, minutes=1)),
            _incident(timedelta(days=89, hours=23)),
            _incident(timedelta(hours=7)),
        ]
    )
    await db_session.commit()

    result = await run_retention_sweep(db_session, now=NOW)

    assert result.counts["spam_ring_incidents"] == 1
    kept = (await db_session.execute(select(SpamRingIncident))).scalars().all()
    assert sorted(row.trigger_message_id for row in kept) == sorted(
        [str(int(timedelta(days=89, hours=23).total_seconds())), str(7 * 3600)]
    )
