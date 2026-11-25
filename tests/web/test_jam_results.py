"""After a jam starts: what the bot is asked to do, vote counts and results."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from smarter_dev.web.jams import results
from smarter_dev.web.jams import store
from smarter_dev.web.jams import teams
from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamSubmission
from tests.web.jam_helpers import GUILD
from tests.web.jam_helpers import START
from tests.web.jam_helpers import make_jam
from tests.web.jam_helpers import make_setup

OPEN = START + timedelta(days=5)
CLOSE = START + timedelta(days=33)
VOTING_ENDS = CLOSE + timedelta(days=7)
CLOSED = CLOSE + timedelta(minutes=1)
VOTED = VOTING_ENDS + timedelta(minutes=1)


async def _jam(db_session, *, setup=True, **overrides) -> Jam:
    values = {"voting_ends_at": VOTING_ENDS, "showcase_tag_id": "31"}
    values.update(overrides)
    jam = make_jam(**values)
    db_session.add(jam)
    if setup:
        db_session.add(make_setup())
    await db_session.commit()
    return jam


async def _team(db_session, jam, thread_id, *members, name="Elves"):
    first, *rest = members
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


async def _post(db_session, jam, thread_id, author_id, when=OPEN):
    return await teams.record_submission(
        db_session, jam, thread_id=thread_id, author_id=author_id, posted_at=when
    )


async def _work(db_session, now) -> results.JamWork | None:
    found = await results.list_work(db_session, now)
    assert len(found) <= 1
    return found[0] if found else None


def _votes(**counts: tuple[int, int]) -> list[results.VoteCount]:
    return [
        results.VoteCount(thread_id=thread[1:], reactions=score[0], reactors=score[1])
        for thread, score in counts.items()
    ]


async def _counts(db_session, jam) -> dict[str, tuple[int | None, int | None]]:
    rows = await db_session.execute(
        select(JamSubmission)
        .where(JamSubmission.jam_id == jam.id)
        .execution_options(populate_existing=True)
    )
    return {
        row.thread_id: (row.reaction_count, row.reactor_count) for row in rows.scalars()
    }


# -- what the bot is asked to do ----------------------------------------------


async def test_nothing_to_do_before_a_jam_starts(db_session):
    await _jam(db_session)

    assert await results.list_work(db_session, START - timedelta(seconds=1)) == []


async def test_an_open_jam_asks_only_for_missed_posts(db_session):
    jam = await _jam(db_session, year_tag_id="32")
    await _team(db_session, jam, "500", "1")
    await _post(db_session, jam, "700", "1")
    await _post(db_session, jam, "701", "9")

    work = await _work(db_session, OPEN)

    assert work.jam_id == jam.id and work.guild_id == GUILD
    assert work.name == "Holiday Chatbot Jam"
    assert work.showcase_channel_id == "14"
    assert work.tag_ids == ("31", "32")
    assert (work.starts_at, work.submissions_close_at) == (START, CLOSE)
    assert work.voting_ends_at == VOTING_ENDS
    assert work.known_posts == ("700", "701")
    assert work.find_missed_posts is True
    assert work.teams_to_close is None
    assert work.count_votes is False


async def test_the_jam_is_still_open_at_the_closing_moment(db_session):
    await _jam(db_session)

    work = await _work(db_session, CLOSE)

    assert work.find_missed_posts is True and work.teams_to_close is None


async def test_a_closed_jam_asks_for_its_teams_to_be_closed(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, "500", "1", "2", name="Elves")
    await _team(db_session, jam, "501", "3", name="Reindeer")
    await _post(db_session, jam, "700", "2")
    await _post(db_session, jam, "701", "2")
    await _post(db_session, jam, "702", "9")

    work = await _work(db_session, CLOSED)

    # Missed posts are looked for once more, so each team's entry is known.
    assert work.find_missed_posts is True
    assert sorted(work.teams_to_close, key=lambda team: team.thread_id) == [
        results.TeamToClose("Elves", "500", "700"),
        results.TeamToClose("Reindeer", "501", None),
    ]
    assert work.count_votes is False


async def test_a_jam_with_no_teams_is_still_closed(db_session):
    await _jam(db_session)

    work = await _work(db_session, CLOSED)

    assert work.teams_to_close == ()


async def test_a_removed_post_is_known_but_is_not_a_teams_entry(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, "500", "1")
    await _post(db_session, jam, "700", "1")
    await _post(db_session, jam, "701", "1")
    assert await teams.remove_entry(db_session, jam, "700")

    work = await _work(db_session, CLOSED)

    assert work.known_posts == ("700", "701")
    assert work.teams_to_close == (results.TeamToClose("Elves", "500", "701"),)


async def test_closing_is_reported_once_and_then_not_asked_for(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, "500", "1")

    assert await results.mark_teams_closed(db_session, jam.id, CLOSED) is None
    later = CLOSED + timedelta(hours=1)
    assert await results.mark_teams_closed(db_session, jam.id, later) is None

    await db_session.refresh(jam)
    assert store.as_utc(jam.teams_closed_at) == CLOSED
    # Nothing to do until voting ends.
    assert await results.list_work(db_session, later) == []


async def test_closing_is_refused_while_the_jam_is_open(db_session):
    jam = await _jam(db_session)

    assert (
        await results.mark_teams_closed(db_session, jam.id, CLOSE) == results.TOO_EARLY
    )
    assert (
        await results.mark_teams_closed(db_session, uuid4(), CLOSED) == results.NO_JAM
    )

    await db_session.refresh(jam)
    assert jam.teams_closed_at is None


async def test_a_jam_that_closed_long_ago_is_left_as_it_is(db_session):
    await _jam(db_session, voting_ends_at=None)
    edge = CLOSE + results.CLOSE_WITHIN

    assert (await _work(db_session, edge)).teams_to_close == ()
    assert await results.list_work(db_session, edge + timedelta(seconds=1)) == []


async def test_votes_are_counted_after_voting_ends_and_teams_are_closed(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "9")
    await results.mark_teams_closed(db_session, jam.id, CLOSED)

    assert await results.list_work(db_session, VOTING_ENDS) == []
    work = await _work(db_session, VOTED)

    assert work.count_votes is True
    assert work.known_posts == ("700",)
    assert work.find_missed_posts is False and work.teams_to_close is None


async def test_votes_wait_for_the_teams_to_be_closed(db_session):
    await _jam(db_session, voting_ends_at=CLOSE + timedelta(days=2))
    voted = CLOSE + timedelta(days=2, minutes=1)

    work = await _work(db_session, voted)

    assert work.teams_to_close == () and work.count_votes is False


async def test_votes_are_counted_even_if_the_teams_were_never_closed(db_session):
    await _jam(db_session, voting_ends_at=CLOSE + timedelta(days=2))

    work = await _work(db_session, CLOSE + results.CLOSE_WITHIN + timedelta(seconds=1))

    assert work.count_votes is True
    assert work.find_missed_posts is False and work.teams_to_close is None


async def test_a_jam_with_no_voting_end_is_never_counted(db_session):
    jam = await _jam(db_session, voting_ends_at=None)
    await results.mark_teams_closed(db_session, jam.id, CLOSED)

    assert await results.list_work(db_session, CLOSE + timedelta(days=400)) == []


async def test_each_jam_gets_its_own_work(db_session):
    old = await _jam(db_session, name="Old Jam")
    await results.mark_teams_closed(db_session, old.id, CLOSED)
    new_start = VOTING_ENDS + timedelta(days=1)
    new = await _jam(
        db_session,
        setup=False,
        name="New Jam",
        starts_at=new_start,
        submissions_close_at=new_start + timedelta(days=30),
        voting_ends_at=None,
    )

    found = await results.list_work(db_session, new_start + timedelta(hours=1))

    assert [(work.jam_id, work.count_votes) for work in found] == [
        (old.id, True),
        (new.id, False),
    ]
    assert found[1].find_missed_posts is True


async def test_work_without_a_discord_setup_has_no_showcase(db_session):
    await _jam(db_session, setup=False, showcase_tag_id=None)

    work = await _work(db_session, OPEN)

    assert work.showcase_channel_id is None and work.tag_ids == ()


# -- a post recorded late -----------------------------------------------------


async def test_a_post_is_judged_by_when_it_was_made(db_session):
    jam = await _jam(db_session)

    on_time = await _post(db_session, jam, "700", "9", when=CLOSE)
    late = await _post(db_session, jam, "701", "9", when=CLOSED)
    early = await _post(db_session, jam, "702", "9", when=START - timedelta(seconds=1))

    assert on_time.new is True and late is None and early is None
    assert list(await _counts(db_session, jam)) == ["700"]


# -- removing and restoring ---------------------------------------------------


async def test_a_removed_post_is_kept_but_counts_for_nothing(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, "500", "1", "2")
    await _post(db_session, jam, "700", "1")

    assert await teams.remove_entry(db_session, jam, "700") is True
    assert await teams.remove_entry(db_session, jam, "700") is False

    assert await teams.list_entries(db_session, jam.id) == []
    assert [
        post.thread_id for post in await teams.list_removed(db_session, jam.id)
    ] == ["700"]
    # The team has no entry again, so it can change.
    left = await teams.leave_team(db_session, jam, thread_id="500", user_id="2")
    assert left.refusal is None


async def test_a_removed_post_is_not_recorded_again(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "9")
    await teams.remove_entry(db_session, jam, "700")

    assert await _post(db_session, jam, "700", "9") is None

    assert await teams.list_entries(db_session, jam.id) == []
    # Nor does it stand in the way of the member's next post being the entry.
    again = await _post(db_session, jam, "701", "9")
    assert again.entry_thread_id == "701"


async def test_a_removed_solo_post_lets_the_member_join_a_team(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "9")
    await teams.remove_entry(db_session, jam, "700")

    made = await teams.create_team(
        db_session, jam, name="Elves", thread_id="500", user_id="9", now=OPEN
    )

    assert made.refusal is None


async def test_a_solo_post_is_not_restored_once_its_author_is_on_a_team(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "9")
    await teams.remove_entry(db_session, jam, "700")
    await _team(db_session, jam, "500", "9")

    assert await teams.restore_entry(db_session, jam, "700") == teams.ON_A_TEAM

    assert await teams.list_entries(db_session, jam.id) == []
    assert len(await teams.list_removed(db_session, jam.id)) == 1


async def test_a_team_post_is_not_restored_once_its_author_has_left_the_team(
    db_session,
):
    jam = await _jam(db_session)
    await _team(db_session, jam, "500", "9", "8")
    await _post(db_session, jam, "700", "9")
    await teams.remove_entry(db_session, jam, "700")
    left = await teams.leave_team(db_session, jam, thread_id="500", user_id="9")
    assert left.refusal is None and left.members_left == 1
    await _post(db_session, jam, "701", "9")

    assert await teams.restore_entry(db_session, jam, "700") == teams.LEFT_TEAM

    rows = await teams.list_entries(db_session, jam.id)
    assert [(row.submission.thread_id, row.team) for row in rows] == [("701", None)]
    # The team is not locked again by a post from someone no longer on it.
    gone = await teams.leave_team(db_session, jam, thread_id="500", user_id="8")
    assert gone.refusal is None


async def test_a_team_post_is_restored_while_its_author_is_still_on_the_team(
    db_session,
):
    jam = await _jam(db_session)
    await _team(db_session, jam, "500", "9", "8")
    await _post(db_session, jam, "700", "9")
    await teams.remove_entry(db_session, jam, "700")

    assert await teams.restore_entry(db_session, jam, "700") is None

    [row] = await teams.list_entries(db_session, jam.id)
    assert (row.submission.thread_id, row.team.name) == ("700", "Elves")


async def test_a_removed_post_outlives_its_team(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, "500", "9")
    await _post(db_session, jam, "700", "9")
    await teams.remove_entry(db_session, jam, "700")

    left = await teams.leave_team(db_session, jam, thread_id="500", user_id="9")

    assert left.refusal is None and left.members_left == 0
    assert await teams.list_teams(db_session, jam.id) == []
    # Still known, so the bot does not record it again from the forum.
    assert (await _work(db_session, OPEN)).known_posts == ("700",)
    assert await _post(db_session, jam, "700", "9") is None
    # Restored, it is the author's own entry: the team is gone.
    assert await teams.restore_entry(db_session, jam, "700") is None
    [row] = await teams.list_entries(db_session, jam.id)
    assert (row.submission.thread_id, row.team) == ("700", None)


async def test_a_restored_post_counts_again(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "9")
    await _post(db_session, jam, "701", "9")
    await teams.remove_entry(db_session, jam, "700")

    assert await teams.restore_entry(db_session, jam, "700") is None
    assert await teams.restore_entry(db_session, jam, "700") == teams.NOT_FOUND
    assert await teams.restore_entry(db_session, jam, "999") == teams.NOT_FOUND

    rows = await teams.list_entries(db_session, jam.id)
    assert [(row.submission.thread_id, row.extra_to) for row in rows] == [
        ("700", None),
        ("701", "700"),
    ]
    assert await teams.list_removed(db_session, jam.id) == []


async def test_removing_reaches_only_the_jams_own_posts(db_session):
    jam = await _jam(db_session)
    other_start = START + timedelta(days=365)
    other = await _jam(
        db_session,
        setup=False,
        starts_at=other_start,
        submissions_close_at=other_start + timedelta(days=30),
    )
    await _post(db_session, jam, "700", "9")

    assert await teams.remove_entry(db_session, other, "700") is False
    assert await teams.restore_entry(db_session, other, "700") == teams.NOT_FOUND

    assert len(await teams.list_entries(db_session, jam.id)) == 1


async def test_a_post_deleted_in_discord_is_forgotten_even_if_removed(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "9")
    await teams.remove_entry(db_session, jam, "700")

    assert await teams.remove_submission(db_session, GUILD, "700") is True

    assert await _counts(db_session, jam) == {}


# -- saving the count ---------------------------------------------------------


async def test_votes_are_saved_once_voting_has_ended(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "8")
    await _post(db_session, jam, "701", "9")

    refusal = await results.save_votes(
        db_session, jam.id, _votes(t700=(5, 3), t701=(0, 0), t999=(9, 9)), [], VOTED
    )

    assert refusal is None
    assert await _counts(db_session, jam) == {"700": (5, 3), "701": (0, 0)}
    await db_session.refresh(jam)
    assert store.as_utc(jam.votes_counted_at) == VOTED
    assert await results.list_work(db_session, VOTED) == []


@pytest.mark.parametrize("when", [OPEN, VOTING_ENDS], ids=["open", "last moment"])
async def test_votes_are_refused_before_voting_ends(db_session, when):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "9")

    refusal = await results.save_votes(
        db_session, jam.id, _votes(t700=(5, 3)), ["700"], when
    )

    assert refusal == results.TOO_EARLY
    assert await _counts(db_session, jam) == {"700": (None, None)}
    await db_session.refresh(jam)
    assert jam.votes_counted_at is None


async def test_votes_are_refused_without_a_voting_end_or_a_jam(db_session):
    jam = await _jam(db_session, voting_ends_at=None)

    assert (
        await results.save_votes(db_session, jam.id, [], [], VOTED) == results.TOO_EARLY
    )
    assert (
        await results.save_votes(db_session, uuid4(), [], [], VOTED) == results.NO_JAM
    )


async def test_a_post_gone_from_discord_is_forgotten_with_the_count(db_session):
    jam = await _jam(db_session)
    other_start = START + timedelta(days=365)
    other = await _jam(
        db_session,
        setup=False,
        starts_at=other_start,
        submissions_close_at=other_start + timedelta(days=30),
    )
    await _post(db_session, jam, "700", "9")
    await _post(db_session, jam, "701", "9")
    await _post(db_session, other, "800", "9", when=other_start)

    refusal = await results.save_votes(
        db_session, jam.id, _votes(t701=(2, 2)), ["700", "800"], VOTED
    )

    assert refusal is None
    assert await _counts(db_session, jam) == {"701": (2, 2)}
    # Another jam's post is not this count's to forget.
    assert list(await _counts(db_session, other)) == ["800"]
    [row] = await results.list_results(db_session, jam.id)
    assert (row.thread_id, row.rank) == ("701", 1)


async def test_counting_again_starts_from_nothing(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "9")
    await results.mark_teams_closed(db_session, jam.id, CLOSED)
    await results.save_votes(db_session, jam.id, _votes(t700=(5, 3)), [], VOTED)

    await results.clear_votes(db_session, jam)

    assert await _counts(db_session, jam) == {"700": (None, None)}
    await db_session.refresh(jam)
    assert jam.votes_counted_at is None
    assert (await _work(db_session, VOTED)).count_votes is True


# -- the results --------------------------------------------------------------


async def test_entries_rank_by_reactions_then_by_people(db_session):
    jam = await _jam(db_session)
    await _team(db_session, jam, "500", "1", "2", name="Elves")
    await _post(db_session, jam, "700", "1")
    await _post(db_session, jam, "701", "7")
    await _post(db_session, jam, "702", "8")
    await _post(db_session, jam, "703", "9")
    await results.save_votes(
        db_session,
        jam.id,
        _votes(t700=(4, 2), t701=(9, 1), t702=(4, 4), t703=(1, 1)),
        [],
        VOTED,
    )

    rows = await results.list_results(db_session, jam.id)

    assert [
        (row.rank, row.thread_id, row.reactions, row.reactors, row.tied) for row in rows
    ] == [
        (1, "701", 9, 1, False),
        (2, "702", 4, 4, False),
        (3, "700", 4, 2, False),
        (4, "703", 1, 1, False),
    ]
    assert [(row.team_name, row.author_id) for row in rows] == [
        (None, "7"),
        (None, "8"),
        ("Elves", "1"),
        (None, "9"),
    ]


async def test_entries_level_on_both_share_a_place(db_session):
    jam = await _jam(db_session)
    for thread, author in (("700", "6"), ("701", "7"), ("702", "8"), ("703", "9")):
        await _post(db_session, jam, thread, author)
    await results.save_votes(
        db_session,
        jam.id,
        _votes(t700=(1, 1), t701=(4, 3), t702=(9, 9), t703=(4, 3)),
        [],
        VOTED,
    )

    rows = await results.list_results(db_session, jam.id)

    # The tied pair keeps the order it was posted in; the next place is 4th.
    assert [(row.rank, row.thread_id, row.tied) for row in rows] == [
        (1, "702", False),
        (2, "701", True),
        (2, "703", True),
        (4, "700", False),
    ]


async def test_extra_and_removed_posts_are_not_ranked(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "8")
    await _post(db_session, jam, "701", "8")
    await _post(db_session, jam, "702", "9")
    await teams.remove_entry(db_session, jam, "702")
    await results.save_votes(
        db_session, jam.id, _votes(t700=(1, 1), t701=(50, 40), t702=(60, 50)), [], VOTED
    )

    rows = await results.list_results(db_session, jam.id)

    assert [(row.thread_id, row.reactions) for row in rows] == [("700", 1)]
    # The removed post's count is kept in case it is restored.
    assert (await _counts(db_session, jam))["702"] == (60, 50)


async def test_an_entry_never_counted_ranks_as_zero(db_session):
    jam = await _jam(db_session)
    await _post(db_session, jam, "700", "8")
    await _post(db_session, jam, "701", "9")
    await results.save_votes(db_session, jam.id, _votes(t701=(2, 1)), [], VOTED)

    rows = await results.list_results(db_session, jam.id)

    assert [(row.thread_id, row.reactions, row.reactors) for row in rows] == [
        ("701", 2, 1),
        ("700", 0, 0),
    ]
