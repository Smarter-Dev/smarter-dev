"""Teams and showcase entries of a jam.

A member is on at most one team per jam. That is what lets a showcase post be
tied to a team: the post's author has one team, or none and the entry is
their own.

The rules, all decided here so the bot only reports them:

- teams are made and changed only while the jam is open, from its start
  until submissions close;
- a team has at most ``Jam.max_team_size`` members;
- once a team has an entry its roster is fixed: nobody is added and nobody
  leaves;
- a member who posted an entry on their own cannot join or make a team.

Every change runs under :func:`store.guild_lock`, one at a time per guild, so
a check and the write that follows it see the same rows. The unique
constraint on (jam, member) holds even if that lock is ever bypassed.

Each change may be asked for twice: the bot's API client sends a request again
after a timeout. Asking again for something already done answers as done.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smarter_dev.web.jams import store
from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamSubmission
from smarter_dev.web.models import JamTeam
from smarter_dev.web.models import JamTeamMember

# Why a change was refused. The bot turns each into a sentence.
NOT_OPEN = "not_open"
SOLO_JAM = "solo_jam"
ON_A_TEAM = "on_a_team"
HAS_ENTRY = "has_entry"
NOT_A_TEAM = "not_a_team"
NOT_YOUR_TEAM = "not_your_team"
TEAM_HAS_ENTRY = "team_has_entry"
FULL = "full"
NOT_FOUND = "not_found"


@dataclass(frozen=True)
class TeamInfo:
    """A team as it was when read: plain values that outlive the session."""

    name: str
    thread_id: str
    member_ids: tuple[str, ...]


def team_info(team: JamTeam | None) -> TeamInfo | None:
    """Copy a loaded team's values out of the session."""
    if team is None:
        return None
    return TeamInfo(
        name=team.name,
        thread_id=team.thread_id,
        member_ids=tuple(sorted(member.user_id for member in team.members)),
    )


@dataclass(frozen=True)
class TeamOutcome:
    """What a team change did.

    ``refusal`` is ``None`` when it was done. ``team`` is the team concerned,
    as it was before the change: for :data:`ON_A_TEAM` that is the team the
    member is already on. ``already`` says the change had been made before
    this call.
    """

    refusal: str | None = None
    team: TeamInfo | None = None
    already: bool = False
    members_left: int = 0


@dataclass(frozen=True)
class SubmissionOutcome:
    """A recorded showcase post and the entry it belongs with.

    ``entry_thread_id`` is the earliest post of the same team or solo member.
    When it is not this post's own id, this post is an extra one.
    """

    thread_id: str
    team: TeamInfo | None
    entry_thread_id: str
    new: bool


@dataclass(frozen=True)
class EntryRow:
    """One showcase post as the admin page lists it."""

    submission: JamSubmission
    team: JamTeam | None
    # The post that counts for this team or solo member; None if this one does.
    extra_to: str | None


def is_open(jam: Jam, now: datetime) -> bool:
    """Whether the jam is running: started, and still taking submissions."""
    return store.as_utc(jam.starts_at) <= now <= store.as_utc(jam.submissions_close_at)


# -- Reading ------------------------------------------------------------------


async def team_of(session: AsyncSession, jam_id: UUID, user_id: str) -> JamTeam | None:
    """The member's team in the jam, if they are on one."""
    result = await session.execute(
        select(JamTeam)
        .join(JamTeamMember, JamTeamMember.team_id == JamTeam.id)
        .where(JamTeamMember.jam_id == jam_id, JamTeamMember.user_id == user_id)
        .options(selectinload(JamTeam.members))
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def team_for_thread(
    session: AsyncSession, jam_id: UUID, thread_id: str
) -> JamTeam | None:
    """The jam's team whose thread this is, if it is a team thread."""
    result = await session.execute(
        select(JamTeam)
        .where(JamTeam.jam_id == jam_id, JamTeam.thread_id == thread_id)
        .options(selectinload(JamTeam.members))
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_teams(session: AsyncSession, jam_id: UUID) -> list[JamTeam]:
    """A jam's teams with their members, oldest first."""
    result = await session.execute(
        select(JamTeam)
        .where(JamTeam.jam_id == jam_id)
        .options(selectinload(JamTeam.members))
        .order_by(JamTeam.created_at, JamTeam.id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars())


def _post_order(submission: JamSubmission) -> tuple[int, str]:
    # A Discord id grows with time, so it orders posts by when they were made.
    thread_id = submission.thread_id
    return (int(thread_id) if thread_id.isdigit() else 0, thread_id)


def _entrant(submission: JamSubmission) -> tuple[str, str]:
    """Who a post is from: its team, or its author when they have no team."""
    if submission.team_id is not None:
        return ("team", str(submission.team_id))
    return ("member", submission.author_id)


async def list_entries(session: AsyncSession, jam_id: UUID) -> list[EntryRow]:
    """A jam's showcase posts in the order they were made.

    The first post from a team or a solo member is their entry. A later one
    from the same team or member is marked as extra to it. Posts an
    administrator removed are left out; see :func:`list_removed`.
    """
    result = await session.execute(
        select(JamSubmission)
        .where(JamSubmission.jam_id == jam_id, JamSubmission.removed_at.is_(None))
        .options(selectinload(JamSubmission.team))
        .execution_options(populate_existing=True)
    )
    first: dict[tuple[str, str], str] = {}
    rows: list[EntryRow] = []
    for submission in sorted(result.scalars(), key=_post_order):
        entry = first.setdefault(_entrant(submission), submission.thread_id)
        rows.append(
            EntryRow(
                submission=submission,
                team=submission.team,
                extra_to=None if entry == submission.thread_id else entry,
            )
        )
    return rows


async def list_removed(session: AsyncSession, jam_id: UUID) -> list[JamSubmission]:
    """The jam's posts an administrator stopped counting, oldest first."""
    result = await session.execute(
        select(JamSubmission)
        .where(JamSubmission.jam_id == jam_id, JamSubmission.removed_at.is_not(None))
        .options(selectinload(JamSubmission.team))
        .execution_options(populate_existing=True)
    )
    return sorted(result.scalars(), key=_post_order)


async def _team_has_entry(session: AsyncSession, team_id: UUID) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(JamSubmission)
        .where(JamSubmission.team_id == team_id, JamSubmission.removed_at.is_(None))
    )
    return bool(count)


async def _has_solo_entry(session: AsyncSession, jam_id: UUID, user_id: str) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(JamSubmission)
        .where(
            JamSubmission.jam_id == jam_id,
            JamSubmission.author_id == user_id,
            JamSubmission.team_id.is_(None),
            JamSubmission.removed_at.is_(None),
        )
    )
    return bool(count)


async def cannot_join(
    session: AsyncSession, jam: Jam, user_id: str, now: datetime | None = None
) -> TeamOutcome | None:
    """Why the member cannot make or join a team now; ``None`` if they can.

    Says nothing about the team they would join: its size and its entry are
    checked by :func:`add_member`.
    """
    now = now or datetime.now(UTC)
    if not is_open(jam, now):
        return TeamOutcome(refusal=NOT_OPEN)
    if jam.max_team_size < 2:
        return TeamOutcome(refusal=SOLO_JAM)
    current = await team_of(session, jam.id, user_id)
    if current is not None:
        return TeamOutcome(refusal=ON_A_TEAM, team=team_info(current))
    if await _has_solo_entry(session, jam.id, user_id):
        return TeamOutcome(refusal=HAS_ENTRY)
    return None


# -- Changing teams -----------------------------------------------------------


async def _add_row(session: AsyncSession, row: object) -> bool:
    """Insert a row; False if a unique constraint turned it away.

    Tried inside a savepoint, so a refusal undoes only the insert and the
    transaction keeps the guild's lock.
    """
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return False
    return True


async def create_team(
    session: AsyncSession,
    jam: Jam,
    *,
    name: str,
    thread_id: str,
    user_id: str,
    now: datetime | None = None,
) -> TeamOutcome:
    """Record a new team with the member who made it as its first member."""
    async with store.guild_lock(session, jam.guild_id):
        existing = await team_for_thread(session, jam.id, thread_id)
        if existing is not None:
            # The same request, sent again.
            outcome = TeamOutcome(team=team_info(existing), already=True)
        elif (refused := await cannot_join(session, jam, user_id, now)) is not None:
            outcome = refused
        else:
            team = JamTeam(
                jam_id=jam.id, name=name, thread_id=thread_id, created_by=user_id
            )
            team.members.append(JamTeamMember(jam_id=jam.id, user_id=user_id))
            info = team_info(team)
            if await _add_row(session, team):
                outcome = TeamOutcome(team=info)
            else:
                outcome = await _on_a_team(session, jam, user_id)
        await session.commit()
    return outcome


async def add_member(
    session: AsyncSession,
    jam: Jam,
    *,
    thread_id: str,
    user_id: str,
    invited_by: str,
    now: datetime | None = None,
) -> TeamOutcome:
    """Add a member to the team whose thread ``thread_id`` is.

    Only a member of the team can add someone.
    """
    async with store.guild_lock(session, jam.guild_id):
        outcome = await _add_member(session, jam, thread_id, user_id, invited_by, now)
        await session.commit()
    return outcome


async def _add_member(
    session: AsyncSession,
    jam: Jam,
    thread_id: str,
    user_id: str,
    invited_by: str,
    now: datetime | None,
) -> TeamOutcome:
    team = await team_for_thread(session, jam.id, thread_id)
    if team is None:
        return TeamOutcome(refusal=NOT_A_TEAM)
    info = team_info(team)
    roster = set(info.member_ids)
    if invited_by not in roster:
        return TeamOutcome(refusal=NOT_YOUR_TEAM, team=info)
    if user_id in roster:
        return TeamOutcome(team=info, already=True)
    if await _team_has_entry(session, team.id):
        return TeamOutcome(refusal=TEAM_HAS_ENTRY, team=info)
    if (refused := await cannot_join(session, jam, user_id, now)) is not None:
        return refused
    if len(roster) >= jam.max_team_size:
        return TeamOutcome(refusal=FULL, team=info)
    member = JamTeamMember(team_id=team.id, jam_id=jam.id, user_id=user_id)
    if not await _add_row(session, member):
        return await _on_a_team(session, jam, user_id)
    return TeamOutcome(team=info)


async def _on_a_team(session: AsyncSession, jam: Jam, user_id: str) -> TeamOutcome:
    """The refusal for a member the unique constraint found on a team."""
    current = await team_of(session, jam.id, user_id)
    return TeamOutcome(refusal=ON_A_TEAM, team=team_info(current))


async def leave_team(
    session: AsyncSession, jam: Jam, *, thread_id: str, user_id: str
) -> TeamOutcome:
    """Take a member off the team whose thread ``thread_id`` is.

    A team its last member leaves is deleted. Leaving a team the member is
    not on answers as already done, with no team.
    """
    async with store.guild_lock(session, jam.guild_id):
        outcome = await _leave_team(session, jam, thread_id, user_id)
        await session.commit()
    return outcome


async def _leave_team(
    session: AsyncSession, jam: Jam, thread_id: str, user_id: str
) -> TeamOutcome:
    team = await team_for_thread(session, jam.id, thread_id)
    if team is None:
        return TeamOutcome(already=True)
    info = team_info(team)
    size = len(info.member_ids)
    member = next((m for m in team.members if m.user_id == user_id), None)
    if member is None:
        return TeamOutcome(team=info, already=True, members_left=size)
    if await _team_has_entry(session, team.id):
        return TeamOutcome(refusal=TEAM_HAS_ENTRY, team=info, members_left=size)
    team.members.remove(member)
    if size == 1:
        # Deleting the team deletes its posts' records. A removed post's
        # record is kept, with no team, so the bot does not record the post
        # again when it next looks through the forum.
        await session.execute(
            update(JamSubmission)
            .where(JamSubmission.team_id == team.id)
            .values(team_id=None)
            .execution_options(synchronize_session=False)
        )
        await session.delete(team)
    await session.flush()
    return TeamOutcome(team=info, members_left=size - 1)


# -- Entries ------------------------------------------------------------------


async def record_submission(
    session: AsyncSession,
    jam: Jam,
    *,
    thread_id: str,
    author_id: str,
    posted_at: datetime | None = None,
) -> SubmissionOutcome | None:
    """Record a showcase post against its author's team, or as a solo entry.

    ``posted_at`` is when the post was made, which can be well before it is
    recorded: the bot picks up posts it missed. ``None`` if the jam was not
    open then, or an administrator removed the post. The post is kept even
    when its team or author already has an entry; the outcome says which post
    is the entry.
    """
    posted_at = posted_at or datetime.now(UTC)
    async with store.guild_lock(session, jam.guild_id):
        result = await session.execute(
            select(JamSubmission)
            .where(JamSubmission.thread_id == thread_id)
            .execution_options(populate_existing=True)
        )
        submission = result.scalar_one_or_none()
        new = submission is None
        if submission is not None and (
            submission.jam_id != jam.id or submission.removed_at is not None
        ):
            await session.commit()
            return None
        if submission is None:
            if not is_open(jam, posted_at):
                await session.commit()
                return None
            team = await team_of(session, jam.id, author_id)
            submission = JamSubmission(
                jam_id=jam.id,
                thread_id=thread_id,
                author_id=author_id,
                team_id=team.id if team else None,
            )
            session.add(submission)
            await session.flush()
        elif submission.team_id is not None:
            team = await session.get(
                JamTeam,
                submission.team_id,
                options=[selectinload(JamTeam.members)],
                populate_existing=True,
            )
        else:
            team = None
        same = select(JamSubmission).where(
            JamSubmission.jam_id == jam.id, JamSubmission.removed_at.is_(None)
        )
        if submission.team_id is not None:
            same = same.where(JamSubmission.team_id == submission.team_id)
        else:
            same = same.where(
                JamSubmission.team_id.is_(None),
                JamSubmission.author_id == submission.author_id,
            )
        posts = (await session.execute(same)).scalars()
        entry = min(posts, key=_post_order)
        outcome = SubmissionOutcome(
            thread_id=submission.thread_id,
            team=team_info(team),
            entry_thread_id=entry.thread_id,
            new=new,
        )
        await session.commit()
    return outcome


async def remove_submission(
    session: AsyncSession, guild_id: str, thread_id: str
) -> bool:
    """Forget a showcase post, in whichever of the guild's jams it was.

    Used when the post is deleted in Discord. If it was a team's entry, the
    team's next post becomes the entry; with none left, the team can change
    again.
    """
    in_guild = select(Jam.id).where(Jam.guild_id == guild_id)
    return await _delete_submission(session, guild_id, thread_id, in_guild)


async def remove_entry(session: AsyncSession, jam: Jam, thread_id: str) -> bool:
    """Stop counting one of this jam's showcase posts, for the admin page.

    The record is kept and marked: the post is still in Discord, and the bot
    would record it again if it were forgotten.
    """
    async with store.guild_lock(session, jam.guild_id):
        submission = await _submission(session, jam, thread_id)
        changed = submission is not None and submission.removed_at is None
        if changed:
            submission.removed_at = datetime.now(UTC)
        await session.commit()
    return changed


async def restore_entry(session: AsyncSession, jam: Jam, thread_id: str) -> str | None:
    """Count a post again that an administrator had removed.

    Returns why not, or ``None`` when it counts again. A post its author made
    alone is not restored once the author is on a team: they would be
    entered twice.
    """
    async with store.guild_lock(session, jam.guild_id):
        submission = await _submission(session, jam, thread_id)
        refusal = None
        if submission is None or submission.removed_at is None:
            refusal = NOT_FOUND
        elif (
            submission.team_id is None
            and await team_of(session, jam.id, submission.author_id) is not None
        ):
            refusal = ON_A_TEAM
        else:
            submission.removed_at = None
        await session.commit()
    return refusal


async def _submission(
    session: AsyncSession, jam: Jam, thread_id: str
) -> JamSubmission | None:
    result = await session.execute(
        select(JamSubmission)
        .where(JamSubmission.jam_id == jam.id, JamSubmission.thread_id == thread_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _delete_submission(
    session: AsyncSession, guild_id: str, thread_id: str, jam_ids: Select
) -> bool:
    async with store.guild_lock(session, guild_id):
        result = await session.execute(
            delete(JamSubmission)
            .where(
                JamSubmission.thread_id == thread_id,
                JamSubmission.jam_id.in_(jam_ids),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return result.rowcount > 0
