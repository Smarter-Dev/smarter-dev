"""What happens to a jam after it starts: missed posts, closing and results.

The bot asks :func:`list_work` what there is to do and does it in Discord:

- while a jam is open, and once more after submissions close, it looks in the
  showcase forum for posts it never recorded (it was restarting, or the web
  app was);
- after submissions close it tells each team so in its thread and locks the
  thread, then reports with :func:`mark_teams_closed`;
- after voting ends it counts the reactions on every recorded post and hands
  them to :func:`save_votes`.

An entry's score is the number of reactions on the first message of its post
when voting ended, exactly as Discord shows them. Ties go to the entry more
different people reacted to. :func:`list_results` ranks the entries that way.

Each step is safe to repeat: the bot may do one twice after a lost reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.jams import store
from smarter_dev.web.jams import teams
from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamSubmission

# Teams are not told and locked this long after submissions closed: a jam
# that ended before the bot could do this is left as it is.
CLOSE_WITHIN = timedelta(days=7)

# Why a report from the bot was refused.
NO_JAM = "no_jam"
TOO_EARLY = "too_early"


@dataclass(frozen=True)
class TeamToClose:
    """A team to tell that submissions closed, and the entry it posted."""

    name: str
    thread_id: str
    entry_thread_id: str | None


@dataclass(frozen=True)
class JamWork:
    """What the bot should do for one jam now."""

    jam_id: UUID
    guild_id: str
    name: str
    showcase_channel_id: str | None
    tag_ids: tuple[str, ...]
    starts_at: datetime
    submissions_close_at: datetime
    voting_ends_at: datetime | None
    # Every post already recorded, removed ones too, so the bot can tell
    # which it missed. These are also the posts whose reactions it counts.
    known_posts: tuple[str, ...]
    find_missed_posts: bool
    # ``None`` when it is not time to close the teams, or it is done.
    teams_to_close: tuple[TeamToClose, ...] | None
    count_votes: bool


@dataclass(frozen=True)
class VoteCount:
    """The reactions on one post's first message."""

    thread_id: str
    reactions: int
    reactors: int


@dataclass(frozen=True)
class ResultRow:
    """One entry in the results, best first."""

    rank: int
    # Another entry has the same reactions and the same number of reactors.
    tied: bool
    thread_id: str
    author_id: str
    team_name: str | None
    reactions: int
    reactors: int


def _is_closed(jam: Jam, now: datetime) -> bool:
    return now > store.as_utc(jam.submissions_close_at)


def _voting_ended(jam: Jam, now: datetime) -> bool:
    return jam.voting_ends_at is not None and now > store.as_utc(jam.voting_ends_at)


def is_closing(jam: Jam, now: datetime) -> bool:
    """Whether the teams are still to be told and locked."""
    return (
        _is_closed(jam, now)
        and jam.teams_closed_at is None
        and now - store.as_utc(jam.submissions_close_at) <= CLOSE_WITHIN
    )


async def list_work(
    session: AsyncSession, now: datetime | None = None
) -> list[JamWork]:
    """Every jam with something for the bot to do, oldest start first."""
    now = now or datetime.now(UTC)
    result = await session.execute(
        select(Jam)
        .where(
            Jam.starts_at <= now,
            or_(
                Jam.teams_closed_at.is_(None),
                Jam.voting_ends_at.is_not(None) & Jam.votes_counted_at.is_(None),
            ),
        )
        .order_by(Jam.starts_at, Jam.id)
    )
    work: list[JamWork] = []
    for jam in list(result.scalars()):
        closed = _is_closed(jam, now)
        close_at = store.as_utc(jam.submissions_close_at)
        closing = is_closing(jam, now)
        # Counted only after the teams are closed: the last look for missed
        # posts happens then, so every entry is known.
        counting = (
            _voting_ended(jam, now) and jam.votes_counted_at is None and not closing
        )
        if closed and not closing and not counting:
            continue
        setup = await store.get_setup(session, jam.guild_id)
        entries = await teams.list_entries(session, jam.id)
        entry_of = {
            row.team.id: row.submission.thread_id
            for row in entries
            if row.team is not None and row.extra_to is None
        }
        to_close = None
        if closing:
            to_close = tuple(
                TeamToClose(
                    name=team.name,
                    thread_id=team.thread_id,
                    entry_thread_id=entry_of.get(team.id),
                )
                for team in await teams.list_teams(session, jam.id)
            )
        work.append(
            JamWork(
                jam_id=jam.id,
                guild_id=jam.guild_id,
                name=jam.name,
                showcase_channel_id=setup.channel_id_for("showcase") if setup else None,
                tag_ids=tuple(
                    tag for tag in (jam.showcase_tag_id, jam.year_tag_id) if tag
                ),
                starts_at=store.as_utc(jam.starts_at),
                submissions_close_at=close_at,
                voting_ends_at=(
                    store.as_utc(jam.voting_ends_at) if jam.voting_ends_at else None
                ),
                known_posts=tuple(
                    await session.scalars(
                        select(JamSubmission.thread_id)
                        .where(JamSubmission.jam_id == jam.id)
                        .order_by(JamSubmission.thread_id)
                    )
                ),
                find_missed_posts=not closed or closing,
                teams_to_close=to_close,
                count_votes=counting,
            )
        )
    return work


async def _locked_jam(session: AsyncSession, jam_id: UUID) -> Jam | None:
    """Read a jam again, fresh; call inside its guild's lock."""
    result = await session.execute(
        select(Jam).where(Jam.id == jam_id).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _guild_of(session: AsyncSession, jam_id: UUID) -> str | None:
    return await session.scalar(select(Jam.guild_id).where(Jam.id == jam_id))


async def mark_teams_closed(
    session: AsyncSession, jam_id: UUID, now: datetime | None = None
) -> str | None:
    """Record that the teams were told and locked; the refusal, or ``None``."""
    now = now or datetime.now(UTC)
    guild_id = await _guild_of(session, jam_id)
    if guild_id is None:
        return NO_JAM
    async with store.guild_lock(session, guild_id):
        jam = await _locked_jam(session, jam_id)
        refusal = None
        if jam is None:
            refusal = NO_JAM
        elif not _is_closed(jam, now):
            refusal = TOO_EARLY
        elif jam.teams_closed_at is None:
            jam.teams_closed_at = now
        await session.commit()
    return refusal


async def save_votes(
    session: AsyncSession,
    jam_id: UUID,
    counts: list[VoteCount],
    gone: list[str],
    now: datetime | None = None,
) -> str | None:
    """Keep the bot's reaction counts; the refusal, or ``None``.

    ``gone`` are posts deleted in Discord: they are forgotten, as when the bot
    sees a post deleted. A count for a post that is not recorded is ignored.
    """
    now = now or datetime.now(UTC)
    guild_id = await _guild_of(session, jam_id)
    if guild_id is None:
        return NO_JAM
    async with store.guild_lock(session, guild_id):
        jam = await _locked_jam(session, jam_id)
        refusal = None
        if jam is None:
            refusal = NO_JAM
        elif not _voting_ended(jam, now):
            refusal = TOO_EARLY
        else:
            if gone:
                await session.execute(
                    delete(JamSubmission)
                    .where(
                        JamSubmission.jam_id == jam.id,
                        JamSubmission.thread_id.in_(gone),
                    )
                    .execution_options(synchronize_session=False)
                )
            by_thread = {count.thread_id: count for count in counts}
            result = await session.execute(
                select(JamSubmission)
                .where(JamSubmission.jam_id == jam.id)
                .execution_options(populate_existing=True)
            )
            for submission in result.scalars():
                count = by_thread.get(submission.thread_id)
                if count is not None:
                    submission.reaction_count = count.reactions
                    submission.reactor_count = count.reactors
            jam.votes_counted_at = now
        await session.commit()
    return refusal


async def clear_votes(session: AsyncSession, jam: Jam) -> None:
    """Drop the counts so the bot counts again on its next round."""
    async with store.guild_lock(session, jam.guild_id):
        fresh = await _locked_jam(session, jam.id)
        if fresh is not None:
            result = await session.execute(
                select(JamSubmission)
                .where(JamSubmission.jam_id == jam.id)
                .execution_options(populate_existing=True)
            )
            for submission in result.scalars():
                submission.reaction_count = None
                submission.reactor_count = None
            fresh.votes_counted_at = None
        await session.commit()


async def list_results(session: AsyncSession, jam_id: UUID) -> list[ResultRow]:
    """The jam's entries, most reactions first.

    Only the post that counts for each team or solo member is ranked; extra
    posts and posts an administrator removed are not. Entries
    with the same reactions are ordered by how many people reacted; entries
    level on both share a rank and are marked as tied. An entry not counted
    yet ranks as zero.
    """
    scored = [
        (
            row.submission.reaction_count or 0,
            row.submission.reactor_count or 0,
            row,
        )
        for row in await teams.list_entries(session, jam_id)
        if row.extra_to is None
    ]
    # Stable: entries level on both keep the order they were posted in.
    scored.sort(key=lambda item: (-item[0], -item[1]))
    scores = [(reactions, reactors) for reactions, reactors, _ in scored]
    rows: list[ResultRow] = []
    for reactions, reactors, row in scored:
        score = (reactions, reactors)
        rows.append(
            ResultRow(
                rank=scores.index(score) + 1,
                tied=scores.count(score) > 1,
                thread_id=row.submission.thread_id,
                author_id=row.submission.author_id,
                team_name=row.team.name if row.team is not None else None,
                reactions=reactions,
                reactors=reactors,
            )
        )
    return rows
