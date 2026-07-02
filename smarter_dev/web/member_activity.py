"""Member message-activity tracking and the handler-context facts built on it.

The bot batches "user X messaged in guild Y at T" events for every human guild
message; handler dispatch also records the triggering message synchronously so
facts are fresh even before the next batch lands. Handler scripts receive the
derived facts (first message ever, days since last message) in their trigger
context instead of tracking per-user history in their size-capped memory.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import MemberActivity


def _as_utc(value: datetime) -> datetime:
    """Normalize DB datetimes: SQLite drops tzinfo, Postgres keeps it."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def get_activity(
    session: AsyncSession, guild_id: str, user_id: str
) -> MemberActivity | None:
    return (
        await session.execute(
            select(MemberActivity).where(
                MemberActivity.guild_id == guild_id,
                MemberActivity.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def record_activity(
    session: AsyncSession, guild_id: str, user_id: str, message_at: datetime
) -> None:
    """Upsert one activity observation: keep the earliest first, latest last.

    Does not commit — the caller owns the transaction.
    """
    row = await get_activity(session, guild_id, user_id)
    if row is None:
        session.add(
            MemberActivity(
                guild_id=guild_id,
                user_id=user_id,
                first_message_at=message_at,
                last_message_at=message_at,
            )
        )
        return
    if message_at > _as_utc(row.last_message_at):
        row.last_message_at = message_at
    if message_at < _as_utc(row.first_message_at):
        row.first_message_at = message_at


def activity_facts(row: MemberActivity | None, now: datetime) -> dict:
    """The trigger-context facts for one author, derived from their activity row.

    ``None`` row = no message ever observed from this member in the guild.
    """
    if row is None:
        return {
            "author_is_first_message": True,
            "author_days_since_last_message": None,
            "author_last_message_at": None,
        }
    last = _as_utc(row.last_message_at)
    return {
        "author_is_first_message": False,
        "author_days_since_last_message": max(0, (now - last).days),
        "author_last_message_at": last.isoformat(),
    }
