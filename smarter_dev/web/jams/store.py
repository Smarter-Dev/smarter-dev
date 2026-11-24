"""Database operations for jams and their scheduled messages.

Sending is at-most-once. The bot does not read due messages and report back
afterwards, because a lost report would post the message again; it claims
them first (``pending`` -> ``sending``) and then reports ``sent`` or
``failed``. A claim that is never reported on stays ``sending``, which the
admin page shows as stuck and lets an administrator retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamDiscordSetup
from smarter_dev.web.models import JamMessage

# A message this far past its send time is not posted: a "one day left"
# reminder that arrives a day late is worse than none. It is marked failed and
# the administrator decides.
MAX_SEND_DELAY = timedelta(hours=6)

# A claim older than this never got a report back from the bot.
STUCK_AFTER = timedelta(minutes=10)

_ERROR_MAX = 500


def _utc(moment: datetime) -> datetime:
    """Read a stored datetime as UTC (SQLite hands back naive values)."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def is_stuck(message: JamMessage, now: datetime | None = None) -> bool:
    """Whether a claimed message has waited too long for the bot's report."""
    if message.status != "sending" or message.claimed_at is None:
        return False
    now = now or datetime.now(UTC)
    return now - _utc(message.claimed_at) > STUCK_AFTER


# -- Jams ---------------------------------------------------------------------


async def list_jams(session: AsyncSession, guild_id: str) -> list[Jam]:
    """A guild's jams, newest start first."""
    result = await session.execute(
        select(Jam).where(Jam.guild_id == guild_id).order_by(Jam.starts_at.desc())
    )
    return list(result.scalars())


async def get_jam(session: AsyncSession, guild_id: str, jam_id: UUID) -> Jam | None:
    """One jam, or ``None`` if it does not exist in this guild."""
    result = await session.execute(
        select(Jam).where(Jam.id == jam_id, Jam.guild_id == guild_id)
    )
    return result.scalar_one_or_none()


async def overlapping_jam(
    session: AsyncSession,
    guild_id: str,
    starts_at: datetime,
    ends_at: datetime,
    *,
    except_jam_id: UUID | None = None,
) -> Jam | None:
    """A jam in the guild whose dates overlap ``starts_at``..``ends_at``.

    A guild runs one jam at a time: its jams share their channels, and the bot
    tags a new showcase post with whichever jam is on. A jam runs from its
    start to its last scheduled date, both ends included.
    """
    for jam in await list_jams(session, guild_id):
        if jam.id == except_jam_id:
            continue
        if _utc(jam.starts_at) <= ends_at and starts_at <= _utc(jam.ends_at):
            return jam
    return None


async def get_setup(session: AsyncSession, guild_id: str) -> JamDiscordSetup | None:
    """The guild's shared jam channels and role, if setup has ever run."""
    return await session.get(JamDiscordSetup, guild_id)


async def set_showcase_forum(
    session: AsyncSession, guild_id: str, channel_id: str | None
) -> None:
    """Point the guild's showcase at ``channel_id`` (None: not chosen yet).

    Tags belong to one forum, so every jam in the guild forgets its tag ids
    and picks them up again from the new forum at its next setup run.
    """
    setup = await get_or_create_setup(session, guild_id)
    if setup.showcase_channel_id == channel_id:
        return
    setup.showcase_channel_id = channel_id
    await session.execute(
        update(Jam)
        .where(Jam.guild_id == guild_id)
        .values(showcase_tag_id=None, year_tag_id=None)
    )


async def get_or_create_setup(session: AsyncSession, guild_id: str) -> JamDiscordSetup:
    """The guild's shared setup row, added to the session if it is new."""
    setup = await get_setup(session, guild_id)
    if setup is None:
        setup = JamDiscordSetup(guild_id=guild_id)
        session.add(setup)
    return setup


# -- Messages -----------------------------------------------------------------


async def list_messages(session: AsyncSession, jam_id: UUID) -> list[JamMessage]:
    """A jam's scheduled messages in send order."""
    result = await session.execute(
        select(JamMessage)
        .where(JamMessage.jam_id == jam_id)
        .order_by(JamMessage.send_at, JamMessage.created_at)
    )
    return list(result.scalars())


async def get_message(
    session: AsyncSession, jam_id: UUID, message_id: UUID
) -> JamMessage | None:
    """One of a jam's messages, or ``None`` if it is not this jam's."""
    result = await session.execute(
        select(JamMessage).where(
            JamMessage.id == message_id, JamMessage.jam_id == jam_id
        )
    )
    return result.scalar_one_or_none()


async def retry_message(
    session: AsyncSession, message: JamMessage, now: datetime | None = None
) -> bool:
    """Queue a failed or stuck message to send now. False if it cannot be.

    A message still inside its claim window is left alone: the bot may be
    about to post it, and queueing it again would post it twice.
    """
    now = now or datetime.now(UTC)
    if message.status != "failed" and not is_stuck(message, now):
        return False
    message.status = "pending"
    message.send_at = now
    message.claimed_at = None
    message.error = None
    await session.commit()
    return True


@dataclass(frozen=True)
class OutgoingJamMessage:
    """A claimed message, resolved to where the bot should post it."""

    id: UUID
    guild_id: str
    channel_id: str
    content: str
    role_id: str | None


async def claim_due_messages(
    session: AsyncSession, now: datetime | None = None
) -> list[OutgoingJamMessage]:
    """Claim every due message and return the ones the bot should post.

    The claim is one ``UPDATE``, so two callers can never both get the same
    message. A claimed message that cannot be posted (its channel was never
    created, or it is too late) is marked failed here and not returned.
    """
    now = now or datetime.now(UTC)
    claimed = await session.execute(
        update(JamMessage)
        .where(JamMessage.status == "pending", JamMessage.send_at <= now)
        .values(status="sending", claimed_at=now)
        .returning(JamMessage.id)
        .execution_options(synchronize_session=False)
    )
    claimed_ids = list(claimed.scalars())
    if not claimed_ids:
        await session.commit()
        return []

    result = await session.execute(
        select(JamMessage)
        .where(JamMessage.id.in_(claimed_ids))
        .options(selectinload(JamMessage.jam))
        .order_by(JamMessage.send_at, JamMessage.created_at)
        .execution_options(populate_existing=True)
    )
    messages = list(result.scalars())
    setups: dict[str, JamDiscordSetup | None] = {}
    for guild_id in {message.jam.guild_id for message in messages}:
        setups[guild_id] = await get_setup(session, guild_id)

    outgoing: list[OutgoingJamMessage] = []
    for message in messages:
        setup = setups[message.jam.guild_id]
        channel_id = setup.channel_id_for(message.channel_key) if setup else None
        role_id = setup.role_id if setup else None
        problem = _cannot_send(message, channel_id, role_id, now)
        if problem is not None:
            message.status = "failed"
            message.error = problem
            continue
        outgoing.append(
            OutgoingJamMessage(
                id=message.id,
                guild_id=message.jam.guild_id,
                channel_id=channel_id,
                content=message.content,
                role_id=role_id if message.mention_role else None,
            )
        )
    await session.commit()
    return outgoing


def _cannot_send(
    message: JamMessage, channel_id: str | None, role_id: str | None, now: datetime
) -> str | None:
    if now - _utc(message.send_at) > MAX_SEND_DELAY:
        return "Not sent: it was more than 6 hours past its send time."
    if not channel_id:
        return (
            f"Not sent: the jam {message.channel_key} channel has not been "
            "created. Finish the Discord setup, then send it again."
        )
    if message.mention_role and not role_id:
        return (
            "Not sent: the jam role has not been created. Finish the Discord "
            "setup, then send it again."
        )
    return None


async def mark_message_sent(
    session: AsyncSession, message_id: UUID, now: datetime | None = None
) -> bool:
    """Record a claimed message as posted. False if it was not claimed."""
    return await _finish_claim(
        session,
        message_id,
        status="sent",
        sent_at=now or datetime.now(UTC),
        error=None,
    )


async def mark_message_failed(
    session: AsyncSession, message_id: UUID, error: str
) -> bool:
    """Record a claimed message as failed. False if it was not claimed."""
    return await _finish_claim(
        session, message_id, status="failed", error=error[:_ERROR_MAX]
    )


async def _finish_claim(session: AsyncSession, message_id: UUID, **values) -> bool:
    result = await session.execute(
        update(JamMessage)
        .where(JamMessage.id == message_id, JamMessage.status == "sending")
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1
