"""Wrong-answer cooldown on challenge submissions (#432).

Runs ``ChallengeSubmissionOperations.submit_solution`` against a real SQLite
session. Time passing is simulated by shifting every stored submission back by
the elapsed amount, which is what the cooldown reads.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update

from smarter_dev.web.crud import ChallengeSubmissionOperations
from smarter_dev.web.crud import SubmissionCooldownError
from smarter_dev.web.models import Campaign
from smarter_dev.web.models import Challenge
from smarter_dev.web.models import ChallengeInput
from smarter_dev.web.models import ChallengeSubmission
from smarter_dev.web.models import Squad

_GUILD = "123456789012345678"
_ANSWER = "K7Q2"


@pytest.fixture
async def setup(db_session):
    campaign = Campaign(
        guild_id=_GUILD,
        title="Pixel cipher",
        description="A campaign.",
        start_time=datetime.now(UTC) - timedelta(hours=1),
        release_cadence_hours=24,
        announcement_channels=["111"],
        is_active=True,
        created_by="admin",
    )
    db_session.add(campaign)
    await db_session.flush()
    challenge = Challenge(
        campaign_id=campaign.id,
        title="Frame one",
        description="Decode it.",
        order_position=1,
    )
    alpha = Squad(guild_id=_GUILD, role_id="201", name="Alpha")
    beta = Squad(guild_id=_GUILD, role_id="202", name="Beta")
    db_session.add_all([challenge, alpha, beta])
    await db_session.flush()
    for squad in (alpha, beta):
        db_session.add(
            ChallengeInput(
                challenge_id=challenge.id,
                squad_id=squad.id,
                input_data="gif",
                result_data=_ANSWER,
            )
        )
    await db_session.commit()
    return db_session, challenge.id, alpha.id, beta.id


async def _submit(session, challenge_id, squad_id, answer, user_id="1001"):
    return await ChallengeSubmissionOperations(session).submit_solution(
        challenge_id=challenge_id,
        squad_id=squad_id,
        user_id=user_id,
        username=f"user{user_id}",
        submitted_solution=answer,
    )


async def _advance(session, delta: timedelta) -> None:
    rows = (await session.execute(select(ChallengeSubmission))).scalars().all()
    for row in rows:
        stored = row.submitted_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        await session.execute(
            update(ChallengeSubmission)
            .where(ChallengeSubmission.id == row.id)
            .values(submitted_at=stored - delta)
        )
    await session.commit()


async def _count(session) -> int:
    return (
        await session.execute(select(func.count()).select_from(ChallengeSubmission))
    ).scalar_one()


async def _blocked(session, challenge_id, squad_id, answer="WRONG", user_id="1001"):
    with pytest.raises(SubmissionCooldownError) as info:
        await _submit(session, challenge_id, squad_id, answer, user_id=user_id)
    return info.value


async def test_first_wrong_answer_has_no_cooldown_before_it(setup):
    session, challenge_id, alpha, _ = setup

    assert await _submit(session, challenge_id, alpha, "NOPE") == (False, False, None)
    assert await _count(session) == 1


async def test_tiers_escalate_1_2_5_5(setup):
    session, challenge_id, alpha, _ = setup
    expected = [(1, 60), (2, 120), (3, 300), (3, 300)]

    for tier, seconds in expected:
        await _submit(session, challenge_id, alpha, "NOPE")
        err = await _blocked(session, challenge_id, alpha)
        assert err.tier == tier
        assert seconds - 2 <= err.retry_after <= seconds

        # One second short of the delay: still blocked.
        await _advance(session, timedelta(seconds=seconds - 1))
        err = await _blocked(session, challenge_id, alpha)
        assert err.retry_after <= 2

        await _advance(session, timedelta(seconds=1))

    assert await _count(session) == 4


async def test_cap_holds_while_guessing_at_the_cap(setup):
    session, challenge_id, alpha, _ = setup
    for delay in (60, 120, 300):
        await _submit(session, challenge_id, alpha, "NOPE")
        await _advance(session, timedelta(seconds=delay))

    # Guessing as each 5-minute cooldown ends keeps the streak at the cap, even
    # though a fixed 10-minute window would by now hold only two wrong answers.
    for _ in range(3):
        await _submit(session, challenge_id, alpha, "NOPE")
        assert (await _blocked(session, challenge_id, alpha)).tier == 3
        await _advance(session, timedelta(seconds=301))


async def test_gap_of_just_under_ten_minutes_keeps_the_streak(setup):
    session, challenge_id, alpha, _ = setup
    await _submit(session, challenge_id, alpha, "NOPE")
    await _advance(session, timedelta(minutes=9, seconds=55))
    await _submit(session, challenge_id, alpha, "NOPE")

    assert (await _blocked(session, challenge_id, alpha)).tier == 2


async def test_window_expiry_resets_to_one_minute(setup):
    session, challenge_id, alpha, _ = setup
    for delay in (60, 120):
        await _submit(session, challenge_id, alpha, "NOPE")
        await _advance(session, timedelta(seconds=delay))
    await _submit(session, challenge_id, alpha, "NOPE")
    assert (await _blocked(session, challenge_id, alpha)).tier == 3

    # Last wrong answer is now just over 10 minutes old.
    await _advance(session, timedelta(minutes=10, seconds=1))
    await _submit(session, challenge_id, alpha, "NOPE")

    err = await _blocked(session, challenge_id, alpha)
    assert err.tier == 1
    assert 58 <= err.retry_after <= 60


async def test_blocked_attempt_records_nothing(setup):
    session, challenge_id, alpha, _ = setup
    await _submit(session, challenge_id, alpha, "NOPE")

    for _ in range(3):
        await _blocked(session, challenge_id, alpha)

    assert await _count(session) == 1
    # Blocked attempts do not escalate the tier either.
    await _advance(session, timedelta(seconds=60))
    await _submit(session, challenge_id, alpha, "NOPE")
    assert (await _blocked(session, challenge_id, alpha)).tier == 2


async def test_correct_answer_during_cooldown_is_blocked_and_unscored(setup):
    session, challenge_id, alpha, _ = setup
    await _submit(session, challenge_id, alpha, "NOPE")

    await _blocked(session, challenge_id, alpha, answer=_ANSWER)

    rows = (await session.execute(select(ChallengeSubmission))).scalars().all()
    assert [(r.is_correct, r.points_earned) for r in rows] == [(False, None)]

    await _advance(session, timedelta(seconds=60))
    is_correct, is_first, _points = await _submit(session, challenge_id, alpha, _ANSWER)
    assert (is_correct, is_first) == (True, True)


async def test_solved_squad_keeps_old_behaviour(setup):
    session, challenge_id, alpha, _ = setup
    await _submit(session, challenge_id, alpha, _ANSWER)
    await _submit(session, challenge_id, alpha, _ANSWER)

    # Once solved there is no cooldown at all, wrong answers included.
    await _submit(session, challenge_id, alpha, "NOPE")
    await _submit(session, challenge_id, alpha, "NOPE")
    assert await _count(session) == 4


async def test_squad_members_share_the_cooldown(setup):
    session, challenge_id, alpha, _ = setup
    await _submit(session, challenge_id, alpha, "NOPE", user_id="1001")

    err = await _blocked(session, challenge_id, alpha, user_id="1002")
    assert err.tier == 1


async def test_other_squads_are_not_blocked(setup):
    session, challenge_id, alpha, beta = setup
    await _submit(session, challenge_id, alpha, "NOPE")
    await _blocked(session, challenge_id, alpha)

    assert await _submit(session, challenge_id, beta, "NOPE") == (False, False, None)
    # Beta's own first wrong answer puts it on tier 1, unaffected by Alpha.
    assert (await _blocked(session, challenge_id, beta)).tier == 1
