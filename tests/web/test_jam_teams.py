"""Jam teams and showcase entries: who may join, leave and submit."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.jams import teams
from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamSubmission
from smarter_dev.web.models import JamTeam
from smarter_dev.web.models import JamTeamMember
from tests.web.jam_helpers import START
from tests.web.jam_helpers import make_jam

OPEN = START + timedelta(days=5)
THREAD = "500"


async def _jam(db_session, **overrides) -> Jam:
    jam = make_jam(**overrides)
    db_session.add(jam)
    await db_session.commit()
    return jam


async def _team(db_session, jam, thread_id=THREAD, *members, name="Elves"):
    """A team made by its first member, with the rest invited by them."""
    first, *rest = members or ("1",)
    made = await teams.create_team(
        db_session, jam, name=name, thread_id=thread_id, user_id=first, now=OPEN
    )
    assert made.refusal is None
    for member in rest:
        added = await teams.add_member(
            db_session,
            jam,
            thread_id=thread_id,
            user_id=member,
            invited_by=first,
            now=OPEN,
        )
        assert added.refusal is None


async def _add(db_session, jam, user_id, *, by="1", thread_id=THREAD, now=OPEN):
    return await teams.add_member(
        db_session, jam, thread_id=thread_id, user_id=user_id, invited_by=by, now=now
    )


async def _post(db_session, jam, thread_id, author_id, now=OPEN):
    return await teams.record_submission(
        db_session, jam, thread_id=thread_id, author_id=author_id, now=now
    )


async def _roster(db_session, jam, thread_id=THREAD) -> list[str] | None:
    team = await teams.team_for_thread(db_session, jam.id, thread_id)
    return None if team is None else list(teams.team_info(team).member_ids)


async def _count(db_session, model) -> int:
    return await db_session.scalar(select(func.count()).select_from(model))


# -- making a team ------------------------------------------------------------


async def test_a_team_starts_with_the_member_who_made_it(db_session):
    jam = await _jam(db_session)

    made = await teams.create_team(
        db_session, jam, name="Elves", thread_id=THREAD, user_id="1", now=OPEN
    )

    assert made.refusal is None and made.already is False
    assert made.team == teams.TeamInfo("Elves", THREAD, ("1",))
    [team] = await teams.list_teams(db_session, jam.id)
    assert (team.name, team.thread_id, team.created_by) == ("Elves", THREAD, "1")
    assert (await teams.team_of(db_session, jam.id, "1")).id == team.id


@pytest.mark.parametrize(
    "when",
    [START - timedelta(seconds=1), START + timedelta(days=33, seconds=1)],
    ids=["before the jam starts", "after submissions close"],
)
async def test_no_team_is_made_or_joined_outside_the_jam(db_session, when):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1")

    made = await teams.create_team(
        db_session, jam, name="Late", thread_id="501", user_id="2", now=when
    )
    added = await _add(db_session, jam, "2", now=when)

    assert made.refusal == teams.NOT_OPEN
    assert added.refusal == teams.NOT_OPEN
    assert await _count(db_session, JamTeam) == 1
    assert await _roster(db_session, jam) == ["1"]


async def test_teams_open_at_the_start_and_close_with_submissions(db_session):
    jam = await _jam(db_session)

    assert teams.is_open(jam, START)
    assert teams.is_open(jam, START + timedelta(days=33))
    assert not teams.is_open(jam, START - timedelta(microseconds=1))


async def test_a_solo_jam_has_no_teams(db_session):
    jam = await _jam(db_session, max_team_size=1)

    made = await teams.create_team(
        db_session, jam, name="Elves", thread_id=THREAD, user_id="1", now=OPEN
    )

    assert made.refusal == teams.SOLO_JAM
    assert await _count(db_session, JamTeam) == 0


async def test_a_member_is_on_one_team_per_jam(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")
    await _team(db_session, jam, "501", "3", name="Reindeer")

    again = await teams.create_team(
        db_session, jam, name="More", thread_id="502", user_id="2", now=OPEN
    )
    poached = await _add(db_session, jam, "2", by="3", thread_id="501")

    assert again.refusal == teams.ON_A_TEAM
    assert again.team.thread_id == THREAD
    assert poached.refusal == teams.ON_A_TEAM
    assert poached.team.thread_id == THREAD
    assert await _count(db_session, JamTeam) == 2
    assert await _roster(db_session, jam, "501") == ["3"]


async def test_the_same_member_can_be_on_a_team_in_another_jam(db_session):
    first = await _jam(db_session)
    await _team(db_session, first, THREAD, "1")
    later = await _jam(
        db_session,
        starts_at=START + timedelta(days=365),
        submissions_close_at=START + timedelta(days=398),
    )

    made = await teams.create_team(
        db_session,
        later,
        name="Elves again",
        thread_id="900",
        user_id="1",
        now=START + timedelta(days=370),
    )

    assert made.refusal is None
    assert (await teams.team_of(db_session, first.id, "1")).thread_id == THREAD
    assert (await teams.team_of(db_session, later.id, "1")).thread_id == "900"


async def test_a_repeated_create_is_the_same_team(db_session):
    """The bot's API client sends a request again when it times out."""
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1")

    again = await teams.create_team(
        db_session, jam, name="Elves", thread_id=THREAD, user_id="1", now=OPEN
    )

    assert again.refusal is None and again.already is True
    assert await _count(db_session, JamTeam) == 1
    assert await _count(db_session, JamTeamMember) == 1


async def test_the_database_refuses_a_second_team_for_a_member(db_session):
    """The rule holds even if a writer skips the checks."""
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1")
    await _team(db_session, jam, "501", "2")
    other = await teams.team_for_thread(db_session, jam.id, "501")

    added = await teams._add_row(
        db_session, JamTeamMember(team_id=other.id, jam_id=jam.id, user_id="1")
    )

    assert added is False
    await db_session.commit()
    assert await _roster(db_session, jam, "501") == ["2"]


async def test_a_member_row_cannot_name_a_jam_its_team_is_not_in(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1")
    later = await _jam(
        db_session,
        starts_at=START + timedelta(days=365),
        submissions_close_at=START + timedelta(days=398),
    )
    team = await teams.team_for_thread(db_session, jam.id, THREAD)

    added = await teams._add_row(
        db_session, JamTeamMember(team_id=team.id, jam_id=later.id, user_id="9")
    )

    assert added is False


# -- inviting -----------------------------------------------------------------


async def test_a_member_adds_a_teammate(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1")

    added = await _add(db_session, jam, "2")

    assert added.refusal is None and added.already is False
    assert await _roster(db_session, jam) == ["1", "2"]


async def test_adding_stops_at_the_jams_team_size(db_session):
    jam = await _jam(db_session, max_team_size=2)
    await _team(db_session, jam, THREAD, "1", "2")

    added = await _add(db_session, jam, "3")

    assert added.refusal == teams.FULL
    assert await _roster(db_session, jam) == ["1", "2"]


async def test_only_a_member_of_the_team_can_add_to_it(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1")

    outsider = await _add(db_session, jam, "3", by="2")
    nowhere = await _add(db_session, jam, "3", thread_id="999")

    assert outsider.refusal == teams.NOT_YOUR_TEAM
    assert nowhere.refusal == teams.NOT_A_TEAM
    assert await _roster(db_session, jam) == ["1"]


async def test_adding_a_teammate_twice_changes_nothing(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")

    again = await _add(db_session, jam, "2")

    assert again.refusal is None and again.already is True
    assert await _roster(db_session, jam) == ["1", "2"]


async def test_another_jams_team_thread_is_not_a_team_here(db_session):
    old = await _jam(
        db_session,
        starts_at=START - timedelta(days=365),
        submissions_close_at=START - timedelta(days=330),
    )
    db_session.add(
        JamTeam(jam_id=old.id, name="Last year", thread_id=THREAD, created_by="1")
    )
    jam = await _jam(db_session)

    assert (await _add(db_session, jam, "2")).refusal == teams.NOT_A_TEAM


async def test_two_invites_at_once_cannot_share_the_last_seat(db_session, test_engine):
    jam = await _jam(db_session, max_team_size=2)
    await _team(db_session, jam, THREAD, "1")

    async with AsyncSession(test_engine, expire_on_commit=False) as other:
        outcomes = await asyncio.gather(
            _add(db_session, jam, "2"), _add(other, jam, "3")
        )

    assert sorted(str(outcome.refusal) for outcome in outcomes) == ["None", "full"]
    assert len(await _roster(db_session, jam)) == 2


# -- leaving ------------------------------------------------------------------


async def test_a_member_leaves_and_can_join_another_team(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")
    await _team(db_session, jam, "501", "3")

    left = await teams.leave_team(db_session, jam, thread_id=THREAD, user_id="2")
    joined = await _add(db_session, jam, "2", by="3", thread_id="501")

    assert left.refusal is None and left.members_left == 1
    assert left.team.name == "Elves"
    assert joined.refusal is None
    assert await _roster(db_session, jam) == ["1"]
    assert await _roster(db_session, jam, "501") == ["2", "3"]


async def test_a_team_its_last_member_leaves_is_gone(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1")

    left = await teams.leave_team(db_session, jam, thread_id=THREAD, user_id="1")

    assert left.refusal is None and left.members_left == 0
    assert await _count(db_session, JamTeam) == 0
    assert await _count(db_session, JamTeamMember) == 0
    assert await teams.cannot_join(db_session, jam, "1", OPEN) is None


async def test_leaving_twice_or_a_team_you_are_not_on_changes_nothing(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")

    stranger = await teams.leave_team(db_session, jam, thread_id=THREAD, user_id="9")
    nowhere = await teams.leave_team(db_session, jam, thread_id="999", user_id="1")

    assert stranger.refusal is None and stranger.already is True
    assert stranger.members_left == 2
    assert nowhere.refusal is None and nowhere.already is True
    assert await _roster(db_session, jam) == ["1", "2"]


# -- entries ------------------------------------------------------------------


async def test_a_team_members_post_is_the_teams_entry(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")

    posted = await _post(db_session, jam, "700", "2")

    assert posted.new is True
    assert posted.entry_thread_id == "700"
    assert posted.team.thread_id == THREAD
    assert posted.team.member_ids == ("1", "2")
    [row] = await teams.list_entries(db_session, jam.id)
    assert row.team.name == "Elves" and row.extra_to is None


async def test_a_post_from_someone_with_no_team_is_a_solo_entry(db_session):
    jam = await _jam(db_session)

    posted = await _post(db_session, jam, "700", "1")

    assert posted.team is None and posted.entry_thread_id == "700"
    [row] = await teams.list_entries(db_session, jam.id)
    assert row.team is None and row.submission.author_id == "1"


async def test_a_team_has_one_entry_and_later_posts_are_extra(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")
    await _post(db_session, jam, "700", "1")

    second = await _post(db_session, jam, "701", "2")

    assert second.new is True
    assert second.entry_thread_id == "700"
    rows = await teams.list_entries(db_session, jam.id)
    assert [(row.submission.thread_id, row.extra_to) for row in rows] == [
        ("700", None),
        ("701", "700"),
    ]


async def test_the_earlier_post_is_the_entry_whichever_is_recorded_first(db_session):
    """Discord ids grow with time; two posts can be reported out of order."""
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")
    await _post(db_session, jam, "1000", "1")

    earlier = await _post(db_session, jam, "999", "2")

    assert earlier.entry_thread_id == "999"
    rows = await teams.list_entries(db_session, jam.id)
    assert [(row.submission.thread_id, row.extra_to) for row in rows] == [
        ("999", None),
        ("1000", "999"),
    ]


async def test_a_solo_member_has_one_entry_too(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "1")

    second = await _post(db_session, jam, "701", "1")
    someone_else = await _post(db_session, jam, "702", "2")

    assert second.entry_thread_id == "700"
    assert someone_else.entry_thread_id == "702"


async def test_a_post_reported_twice_is_recorded_once(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1")
    await _post(db_session, jam, "700", "1")

    again = await _post(db_session, jam, "700", "1")

    assert again.new is False and again.entry_thread_id == "700"
    assert again.team.thread_id == THREAD
    assert await _count(db_session, JamSubmission) == 1


@pytest.mark.parametrize(
    "when",
    [START - timedelta(seconds=1), START + timedelta(days=33, seconds=1)],
    ids=["before the jam starts", "after submissions close"],
)
async def test_a_post_outside_the_jam_is_not_an_entry(db_session, when):
    jam = await _jam(db_session)

    assert await _post(db_session, jam, "700", "1", now=when) is None
    assert await _count(db_session, JamSubmission) == 0


async def test_a_team_with_an_entry_cannot_change(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")
    await _post(db_session, jam, "700", "1")

    added = await _add(db_session, jam, "3")
    left = await teams.leave_team(db_session, jam, thread_id=THREAD, user_id="2")

    assert added.refusal == teams.TEAM_HAS_ENTRY
    assert left.refusal == teams.TEAM_HAS_ENTRY
    assert await _roster(db_session, jam) == ["1", "2"]


async def test_a_member_with_a_solo_entry_cannot_be_on_a_team(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1")
    await _post(db_session, jam, "700", "2")

    made = await teams.create_team(
        db_session, jam, name="Late", thread_id="501", user_id="2", now=OPEN
    )
    added = await _add(db_session, jam, "2")

    assert made.refusal == teams.HAS_ENTRY
    assert added.refusal == teams.HAS_ENTRY
    assert await _roster(db_session, jam) == ["1"]


async def test_a_post_made_on_a_team_stays_the_teams(db_session):
    """The entry belongs to the team its author was on when they posted."""
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")
    await _post(db_session, jam, "700", "2")

    [row] = await teams.list_entries(db_session, jam.id)

    assert row.submission.team_id == row.team.id
    assert (await _post(db_session, jam, "700", "2")).team.name == "Elves"


# -- removing an entry --------------------------------------------------------


async def test_removing_the_entry_lets_the_team_change_again(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")
    await _post(db_session, jam, "700", "1")

    assert await teams.remove_submission(db_session, jam.guild_id, "700") is True
    assert await teams.remove_submission(db_session, jam.guild_id, "700") is False

    left = await teams.leave_team(db_session, jam, thread_id=THREAD, user_id="2")
    assert left.refusal is None


async def test_removing_the_entry_makes_the_next_post_the_entry(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")
    await _post(db_session, jam, "700", "1")
    await _post(db_session, jam, "701", "2")

    assert await teams.remove_entry(db_session, jam, "700") is True

    [row] = await teams.list_entries(db_session, jam.id)
    assert (row.submission.thread_id, row.extra_to) == ("701", None)
    assert (await _add(db_session, jam, "3")).refusal == teams.TEAM_HAS_ENTRY


async def test_a_post_is_only_removed_from_its_own_guild_and_jam(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "1")
    elsewhere = await _jam(db_session, guild_id="222222222222222222")

    assert await teams.remove_submission(db_session, elsewhere.guild_id, "700") is False
    assert await teams.remove_entry(db_session, elsewhere, "700") is False
    assert await _count(db_session, JamSubmission) == 1


async def test_deleting_a_jam_deletes_its_teams_and_entries(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, THREAD, "1", "2")
    await _post(db_session, jam, "700", "1")
    await _post(db_session, jam, "701", "9")

    await db_session.delete(jam)
    await db_session.commit()

    for model in (JamTeam, JamTeamMember, JamSubmission):
        assert await _count(db_session, model) == 0
