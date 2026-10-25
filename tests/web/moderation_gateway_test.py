"""The action gateway over a fake Discord and a real (SQLite) database."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationIncident
from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.models import ModerationOperation
from smarter_dev.web.models import ModerationOutbox
from smarter_dev.web.moderation_gateway import ModerationGateway
from smarter_dev.web.moderation_gateway import Proposal
from smarter_dev.web.moderation_gateway import Trigger
from smarter_dev.web.moderation_gateway import types as t
from smarter_dev.web.moderation_gateway.locks import MemberLocks
from smarter_dev.web.moderation_gateway.locks import _LocalLocks
from tests.moderation_fakes import GUILD
from tests.moderation_fakes import REPORT_CHANNEL
from tests.moderation_fakes import FakeModerationDiscord
from tests.moderation_fakes import gateway_session_factory
from tests.moderation_fakes import refused

SCAMMER = "5001"
CHANNEL = "7001"
MESSAGE = "8001"


class Clock:
    def __init__(self, start: float = 1_790_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def db(tmp_path):
    engine, factory = await gateway_session_factory(tmp_path)
    yield factory
    await engine.dispose()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def discord(clock):
    fake = FakeModerationDiscord(clock=clock)
    fake.add_member(SCAMMER)
    fake.post(CHANNEL, MESSAGE, SCAMMER, "free nitro https://scam.example @everyone")
    return fake


def make_gateway(
    discord, db, clock, *, separate_process: bool = False
) -> ModerationGateway:
    locks = MemberLocks(
        db, clock=clock, local=_LocalLocks() if separate_process else None
    )
    return ModerationGateway(discord, session_factory=db, clock=clock, locks=locks)


def spam_trigger(**overrides) -> Trigger:
    values = {
        "guild_id": GUILD,
        "actor": "spam_engine",
        "reason": "mass mention, scam link",
        "channel_id": CHANNEL,
        "message_id": MESSAGE,
        "author_id": SCAMMER,
        "content": "free nitro https://scam.example @everyone",
        "report_channel_id": REPORT_CHANNEL,
        "ping_role_id": "644390354157568014",
    }
    values.update(overrides)
    return Trigger(**values)


def handler_trigger(**overrides) -> Trigger:
    values = {
        "guild_id": GUILD,
        "actor": "handler:scam-banner",
        "reason": "Automated review: confirmed scam or abuse",
        "channel_id": CHANNEL,
        "message_id": MESSAGE,
        "author_id": SCAMMER,
        "moderator_username": "handler:scam-banner",
    }
    values.update(overrides)
    return Trigger(**values)


async def rows(db, model, *where):
    async with db() as session:
        return list((await session.execute(select(model).where(*where))).scalars())


async def count(db, model, *where) -> int:
    async with db() as session:
        query = select(func.count()).select_from(model)
        if where:
            query = query.where(*where)
        return (await session.execute(query)).scalar_one()


# ---------------------------------------------------------------------------
# One incident, strongest wins, one report
# ---------------------------------------------------------------------------


async def test_a_timeout_and_a_delete_land_once_with_one_row_each_and_one_report(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)

    result = await gateway.act(
        spam_trigger(),
        [
            Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400),
            Proposal(
                kind=t.DELETE,
                target_user_id=SCAMMER,
                channel_id=CHANNEL,
                message_ids=(MESSAGE,),
            ),
        ],
    )

    assert [outcome.state for outcome in result.outcomes] == [t.CONFIRMED, t.CONFIRMED]
    assert discord.count("timeout") == 1
    assert discord.count("delete") == 1
    actions = await rows(db, ModerationAction)
    assert sorted(action.action_type for action in actions) == ["delete", "timeout"]
    assert all(action.incident_id == result.incident_id for action in actions)
    assert all(action.operation_id is not None for action in actions)
    timeout_row = next(action for action in actions if action.action_type == "timeout")
    assert timeout_row.applied_duration_seconds == 86400
    assert timeout_row.actor == "spam_engine"
    assert timeout_row.moderator_user_id == "1000"
    assert len(discord.reports()) == 1
    report = discord.reports()[0]
    assert report["role_mention_ids"] == ["644390354157568014"]
    assert "mass mention, scam link" in report["content"]
    assert "https://scam.example" not in report["content"]
    assert len(discord.announced) == 2


async def test_a_later_ban_upgrades_the_incident_and_edits_the_same_report(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    first = await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )
    clock.now += 4

    second = await gateway.act(
        handler_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)]
    )

    assert second.incident_id == first.incident_id
    assert second.outcomes[0].state == t.CONFIRMED
    assert discord.count("ban") == 1
    assert len(discord.reports()) == 1
    text = discord.latest_report_text()
    assert f"Banned <@{SCAMMER}>" in text
    assert "scam-banner" in text and "spam engine" in text


async def test_a_weaker_or_duplicate_sanction_after_a_ban_is_never_applied(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    await gateway.act(handler_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)])

    result = await gateway.act(
        spam_trigger(),
        [
            Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400),
            Proposal(kind=t.BAN, target_user_id=SCAMMER),
        ],
    )

    assert [outcome.state for outcome in result.outcomes] == [t.SKIPPED, t.SKIPPED]
    assert "weaker than the ban" in result.outcomes[0].detail
    assert discord.count("timeout") == 0
    assert discord.count("ban") == 1
    assert await count(db, ModerationAction) == 1


async def test_a_message_is_deleted_once_however_many_detectors_ask(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    delete = Proposal(
        kind=t.DELETE,
        target_user_id=SCAMMER,
        channel_id=CHANNEL,
        message_ids=(MESSAGE,),
    )
    await gateway.act(spam_trigger(), [delete])
    result = await gateway.act(handler_trigger(), [delete])

    assert result.outcomes[0].state == t.SKIPPED
    assert result.outcomes[0].detail == "already deleted"
    assert discord.count("delete") == 1
    assert (
        await count(db, ModerationAction, ModerationAction.action_type == "delete") == 1
    )


async def test_a_copy_of_the_post_in_another_channel_joins_the_incident(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    discord.post("7002", "8002", SCAMMER, "Free  NITRO https://scam.example @everyone")
    first = await gateway.act(spam_trigger(), [])
    copy = await gateway.act(
        spam_trigger(
            channel_id="7002",
            message_id="8002",
            content="Free  NITRO https://scam.example @everyone",
        ),
        [],
    )
    unrelated = await gateway.act(
        spam_trigger(
            channel_id="7003", message_id="8003", content="something else entirely"
        ),
        [],
    )

    assert copy.incident_id == first.incident_id
    assert unrelated.incident_id != first.incident_id


async def test_an_image_only_copy_joins_the_incident_by_its_files(discord, db, clock):
    """#333: incident 4d3f0c64's burst had no text, so it had no fingerprint
    and its copy opened a second incident (one never reported)."""
    gateway = make_gateway(discord, db, clock)
    files = tuple(
        t.attachment_signature(f"IMG_{i}.png", 480_000 + i, 1080, 1920)
        for i in range(4)
    )
    first = await gateway.act(spam_trigger(content=None, attachments=files), [])
    copy = await gateway.act(
        spam_trigger(
            channel_id="7002",
            message_id="8002",
            content=None,
            attachments=tuple(reversed(files)),
        ),
        [],
    )
    other_files = await gateway.act(
        spam_trigger(
            channel_id="7003",
            message_id="8003",
            content=None,
            attachments=(t.attachment_signature("cat.png", 1234, 64, 64),),
        ),
        [],
    )

    assert copy.incident_id == first.incident_id
    assert other_files.incident_id != first.incident_id


def test_text_still_decides_a_posts_fingerprint_over_its_files():
    files = (t.attachment_signature("a.png", 1, 2, 3),)
    assert t.fingerprint(SCAMMER, "hello", files) == t.fingerprint(SCAMMER, "hello")
    assert t.fingerprint(SCAMMER, "", files) is not None
    assert t.fingerprint(SCAMMER, "", ()) is None
    assert t.fingerprint("5002", "", files) != t.fingerprint(SCAMMER, "", files)


async def test_an_alias_finds_the_incident_from_another_target(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    discord.add_member("5002")
    ring = await gateway.act(spam_trigger(aliases=("spam_ring:abc",)), [])
    follow_up = await gateway.act(
        Trigger(
            guild_id=GUILD,
            actor="spam_ring",
            reason="follow-up",
            channel_id=CHANNEL,
            message_id="8009",
            author_id="5002",
            aliases=("spam_ring:abc",),
        ),
        [Proposal(kind=t.TIMEOUT, target_user_id="5002", duration_seconds=600)],
    )
    assert follow_up.incident_id == ring.incident_id


async def test_a_protected_target_is_never_acted_on(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    discord.protected[SCAMMER] = "holds a staff role"

    result = await gateway.act(
        spam_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)]
    )

    assert result.outcomes[0].state == t.SKIPPED
    assert "protected" in result.outcomes[0].detail
    assert discord.calls == []
    assert await count(db, ModerationAction) == 0


async def test_a_refused_call_is_a_failed_operation_and_no_row(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    discord.fail["ban"] = refused()

    result = await gateway.act(
        handler_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)]
    )

    assert result.outcomes[0].state == t.FAILED
    assert await count(db, ModerationAction) == 0
    ops = await rows(db, ModerationOperation)
    assert [op.state for op in ops] == [t.FAILED]


# ---------------------------------------------------------------------------
# Timeouts: claims, never shorter, someone else's left alone
# ---------------------------------------------------------------------------


async def test_a_timeout_never_shortens_a_longer_one(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )
    result = await gateway.act(
        handler_trigger(),
        [
            Proposal(
                kind=t.TIMEOUT,
                target_user_id=SCAMMER,
                duration_seconds=300,
                claim="hold:x",
            )
        ],
    )
    assert result.outcomes[0].state == t.COVERED
    assert discord.count("timeout") == 1


async def test_a_covered_timeout_says_when_the_one_in_place_ends(discord, db, clock):
    """#342: the member's DM gave the end the covered request asked for; the
    timeout already in place is what they live with."""
    gateway = make_gateway(discord, db, clock)
    first = await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )
    result = await gateway.act(
        handler_trigger(),
        [
            Proposal(
                kind=t.TIMEOUT,
                target_user_id=SCAMMER,
                duration_seconds=300,
                claim="hold:x",
            )
        ],
    )

    [outcome] = result.outcomes
    assert outcome.state == t.COVERED
    assert outcome.in_place_until == first.outcomes[0].expires_at
    assert outcome.expires_at != outcome.in_place_until
    assert first.outcomes[0].in_place_until is None


async def test_someone_elses_timeout_is_left_alone(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    discord.members[SCAMMER] = clock.now + 600  # a moderator's 10 minutes

    result = await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )

    assert result.outcomes[0].state == t.SKIPPED
    assert "someone else" in result.outcomes[0].detail
    assert discord.members[SCAMMER] == clock.now + 600


async def test_provisional_timeout_then_ban_then_nothing_left_to_lift(
    discord, db, clock
):
    """Step 3's shape: hold while evaluating, then a final decision."""
    gateway = make_gateway(discord, db, clock)
    hold = await gateway.act(
        handler_trigger(),
        [
            Proposal(
                kind=t.TIMEOUT,
                target_user_id=SCAMMER,
                duration_seconds=300,
                claim="provisional",
            )
        ],
    )
    ban = await gateway.act(
        handler_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)]
    )
    assert hold.outcomes[0].state == t.CONFIRMED
    assert ban.outcomes[0].state == t.CONFIRMED
    assert discord.count("ban") == 1


async def test_provisional_timeout_cleared_lifts_only_its_own_claim(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        handler_trigger(),
        [
            Proposal(
                kind=t.TIMEOUT,
                target_user_id=SCAMMER,
                duration_seconds=300,
                claim="provisional",
            )
        ],
    )
    released = await gateway.act(
        handler_trigger(reason="review: clean"),
        [Proposal(kind=t.UNTIMEOUT, target_user_id=SCAMMER, claim="provisional")],
    )
    assert released.outcomes[0].state == t.CONFIRMED
    assert discord.members[SCAMMER] is None
    actions = await rows(db, ModerationAction)
    assert sorted(action.action_type for action in actions) == ["timeout", "untimeout"]


async def test_a_release_falls_back_to_the_longest_remaining_claim(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        handler_trigger(),
        [
            Proposal(
                kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=3600, claim="a"
            )
        ],
    )
    clock.now += 1
    await gateway.act(
        handler_trigger(message_id="8002"),
        [
            Proposal(
                kind=t.TIMEOUT,
                target_user_id=SCAMMER,
                duration_seconds=86400,
                claim="b",
            )
        ],
    )
    result = await gateway.act(
        handler_trigger(message_id="8002"),
        [Proposal(kind=t.UNTIMEOUT, target_user_id=SCAMMER, claim="b")],
    )
    assert result.outcomes[0].state == t.CONFIRMED
    assert discord.members[SCAMMER] == pytest.approx(clock.now - 1 + 3600, abs=2)


async def test_a_release_never_lifts_a_moderators_timeout(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        handler_trigger(),
        [
            Proposal(
                kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=300, claim="a"
            )
        ],
    )
    discord.members[SCAMMER] = clock.now + 7200  # a moderator extended it by hand

    result = await gateway.act(
        handler_trigger(),
        [Proposal(kind=t.UNTIMEOUT, target_user_id=SCAMMER, claim="a")],
    )

    assert result.outcomes[0].state == t.SKIPPED
    assert discord.members[SCAMMER] == clock.now + 7200


async def test_a_moderators_reversal_stands_automation_down(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    first = await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )
    async with db() as session:
        session.add(
            ModerationAction(
                guild_id=GUILD,
                target_user_id=SCAMMER,
                target_username="scammer",
                moderator_user_id="42",
                moderator_username="mod",
                action_type="untimeout",
                source="audit_log",
                created_at=t.from_epoch(clock.now + 1),
            )
        )
        await session.commit()
    discord.members[SCAMMER] = None
    clock.now += 5

    again = await gateway.act(
        handler_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )

    assert again.incident_id == first.incident_id
    assert again.outcomes[0].state == t.SKIPPED
    assert "reversed" in again.outcomes[0].detail
    assert discord.count("timeout") == 1


# ---------------------------------------------------------------------------
# Unknown outcomes are reconciled, never blindly retried
# ---------------------------------------------------------------------------


async def test_a_lost_response_is_reconciled_from_discord_not_resent(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    discord.crash_after["ban"] = ConnectionError("response lost")

    first = await gateway.act(
        handler_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)]
    )
    assert first.outcomes[0].state == t.UNKNOWN
    assert await count(db, ModerationAction) == 0

    clock.now += 30
    settled = await gateway.reconcile_pending()

    assert settled == 1
    assert discord.count("ban") == 1
    [op] = await rows(db, ModerationOperation)
    assert op.state == t.CONFIRMED
    assert await count(db, ModerationAction, ModerationAction.action_type == "ban") == 1


async def test_a_crash_after_the_call_is_reconciled_when_its_lease_runs_out(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)

    class Crash(BaseException):
        """Stands in for the process dying: nothing after it runs."""

    original = gateway._settle_confirmed

    async def die(*args, **kwargs):
        raise Crash()

    gateway._settle_confirmed = die
    with pytest.raises(Crash):
        await gateway.act(
            spam_trigger(),
            [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
        )
    gateway._settle_confirmed = original
    [op] = await rows(db, ModerationOperation)
    assert op.state == t.CLAIMED

    # A restarted process: the lease is still live, so nothing is touched yet.
    restarted = make_gateway(discord, db, clock, separate_process=True)
    assert await restarted.reconcile_pending() == 0
    clock.now += 120
    assert await restarted.reconcile_pending() == 1

    assert discord.count("timeout") == 1
    [op] = await rows(db, ModerationOperation)
    assert op.state == t.CONFIRMED
    assert (
        await count(db, ModerationAction, ModerationAction.action_type == "timeout")
        == 1
    )


async def test_a_call_that_never_landed_is_failed_and_may_be_retried(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    discord.fail["ban"] = ConnectionError("never arrived")
    first = await gateway.act(
        handler_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)]
    )
    assert first.outcomes[0].state == t.UNKNOWN
    clock.now += 30

    retry = await gateway.act(
        handler_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)]
    )

    assert retry.outcomes[0].state == t.CONFIRMED
    assert discord.count("ban") == 1
    assert await count(db, ModerationAction) == 1


async def test_a_repeated_timeout_is_the_same_request_across_a_second_boundary(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    discord.fail["set_timeout"] = ConnectionError("never arrived")
    clock.now = 1_790_000_000.8
    first = await gateway.act(
        handler_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=600)],
    )
    assert first.outcomes[0].state == t.UNKNOWN
    # Its expiry now lands in the next second.
    clock.now += 0.5

    again = await gateway.act(
        handler_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=600)],
    )

    # The failure is injected once, so a second call would have landed.
    assert again.outcomes[0].state == t.UNKNOWN
    assert discord.count("timeout") == 0
    assert await count(db, ModerationOperation) == 1

    # Once reconciled as never applied, the repeat is the one allowed retry.
    clock.now += 30
    await gateway.reconcile_pending()
    retry = await gateway.act(
        handler_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=600)],
    )

    assert retry.outcomes[0].state == t.CONFIRMED
    assert discord.count("timeout") == 1
    assert await count(db, ModerationOperation) == 1
    assert await count(db, ModerationAction) == 1


async def test_a_retried_short_timeout_runs_from_the_retry_not_the_first_request(
    discord, db, clock
):
    # #249's delta review: a 30 s timeout repeated at +45 s was sent with the
    # first request's expiry, 15 s in the past, and recorded as confirmed.
    gateway = make_gateway(discord, db, clock)
    proposal = Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=30)
    discord.fail["set_timeout"] = ConnectionError("never arrived")
    first = await gateway.act(spam_trigger(), [proposal])
    assert first.outcomes[0].state == t.UNKNOWN
    clock.now += 10
    await gateway.reconcile_pending()
    clock.now += 35
    until = clock.now + 30
    # The retry lands but its answer is lost: the row is all there is.
    discord.crash_after["set_timeout"] = ConnectionError("response lost")

    retry = await gateway.act(spam_trigger(), [proposal])

    assert retry.outcomes[0].state == t.UNKNOWN
    assert retry.outcomes[0].expires_at.timestamp() == until
    assert [call[2] for call in discord.calls if call[0] == "timeout"] == [until]
    (op,) = await rows(db, ModerationOperation)
    assert op.expires_at == until
    assert op.requested["until"] == until
    assert op.requested["retried"] is True
    clock.now += 10
    await gateway.reconcile_pending()
    (op,) = await rows(db, ModerationOperation)
    assert op.state == t.CONFIRMED
    assert op.expires_at == until
    assert await count(db, ModerationAction) == 1


async def test_a_timeout_operation_is_retried_at_most_once(discord, db, clock):
    # #254's review: each retry wrote its own expiry on the row, so the
    # retry window slid along and one row could be revived again and again.
    gateway = make_gateway(discord, db, clock)
    proposal = Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=30)
    for _ in range(4):
        discord.fail["set_timeout"] = ConnectionError("never arrived")
        outcome = await gateway.act(spam_trigger(), [proposal])
        assert outcome.outcomes[0].state == t.UNKNOWN
        clock.now += 10
        await gateway.reconcile_pending()
        clock.now += 35

    ops = sorted(await rows(db, ModerationOperation), key=lambda op: op.idempotency_key)
    # Each row: its request, then its one retry. The third request is new.
    assert [op.idempotency_key.rsplit(":", 1)[1] for op in ops] == ["0", "1"]
    assert all(op.requested["retried"] for op in ops)
    assert all(op.state == t.FAILED for op in ops)
    assert discord.count("timeout") == 0


@pytest.mark.parametrize("gap", [0.0, 1.0, 4.0])
async def test_an_unknown_timeout_is_not_resent_inside_the_grace_and_gets_one_row(
    discord, db, clock, gap
):
    # #249's repro: the spam engine asks again inside the 5 s reconcile grace.
    gateway = make_gateway(discord, db, clock)
    proposal = Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=600)
    discord.crash_after["set_timeout"] = ConnectionError("response lost")
    first = await gateway.act(spam_trigger(), [proposal])
    assert first.outcomes[0].state == t.UNKNOWN
    clock.now += gap

    second = await gateway.act(spam_trigger(), [proposal])

    assert second.outcomes[0].state == t.UNKNOWN
    assert discord.count("timeout") == 1
    clock.now += 30
    await gateway.reconcile_pending()
    assert (
        await count(db, ModerationAction, ModerationAction.action_type == "timeout")
        == 1
    )
    await gateway.deliver_outbox()
    assert len(discord.announced) == 1


async def test_a_longer_timeout_from_the_same_claim_still_extends(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=600)],
    )
    clock.now += 2

    longer = await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )

    assert longer.outcomes[0].state == t.CONFIRMED
    assert discord.count("timeout") == 2
    keys = [op.idempotency_key for op in await rows(db, ModerationOperation)]
    assert sorted(key.rsplit(":", 1)[1] for key in keys) == ["0", "1"]


# ---------------------------------------------------------------------------
# Races
# ---------------------------------------------------------------------------


async def test_two_detectors_in_two_processes_within_milliseconds(discord, db, clock):
    bot = make_gateway(discord, db, clock, separate_process=True)
    worker = make_gateway(discord, db, clock, separate_process=True)

    results = await asyncio.gather(
        bot.act(
            spam_trigger(),
            [
                Proposal(
                    kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400
                ),
                Proposal(
                    kind=t.DELETE,
                    target_user_id=SCAMMER,
                    channel_id=CHANNEL,
                    message_ids=(MESSAGE,),
                ),
            ],
        ),
        worker.act(
            handler_trigger(),
            [
                Proposal(
                    kind=t.DELETE,
                    target_user_id=SCAMMER,
                    channel_id=CHANNEL,
                    message_ids=(MESSAGE,),
                ),
                Proposal(kind=t.BAN, target_user_id=SCAMMER),
            ],
        ),
    )

    assert results[0].incident_id == results[1].incident_id
    assert await count(db, ModerationIncident) == 1
    assert discord.count("ban") == 1
    assert discord.count("delete") == 1
    assert len(discord.reports()) == 1
    assert await count(db, ModerationAction, ModerationAction.action_type == "ban") == 1
    assert (
        await count(db, ModerationAction, ModerationAction.action_type == "delete") == 1
    )


async def test_a_manual_timeout_racing_an_automated_one_wins(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    manual = Trigger(
        guild_id=GUILD,
        actor="manual",
        reason="calm down",
        source=t.SOURCE_MANUAL,
        channel_id=CHANNEL,
        moderator_user_id="42",
        moderator_username="mod",
    )
    await asyncio.gather(
        gateway.act(
            spam_trigger(),
            [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
        ),
        gateway.act(
            manual,
            [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=600)],
        ),
    )
    # Whichever ran first, the moderator's ten minutes stand.
    assert discord.members[SCAMMER] == pytest.approx(clock.now + 600, abs=2)
    later = await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )
    assert later.outcomes[0].state in (t.SKIPPED, t.COVERED)
    assert discord.members[SCAMMER] == pytest.approx(clock.now + 600, abs=2)


# ---------------------------------------------------------------------------
# Reserve: caller-performed effects
# ---------------------------------------------------------------------------


async def test_a_reserved_warn_records_one_row_and_a_second_is_skipped(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    proposal = Proposal(kind=t.WARN, target_user_id=SCAMMER, reason="stop")
    async with gateway.reserve(spam_trigger(report_channel_id=None), proposal) as slot:
        assert slot.go
        slot.confirm()
    async with gateway.reserve(handler_trigger(), proposal) as second:
        assert not second.go
    assert (
        await count(db, ModerationAction, ModerationAction.action_type == "warn") == 1
    )


async def test_an_incident_without_a_report_channel_posts_nothing(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        spam_trigger(report_channel_id=None),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=60)],
    )
    assert discord.posts == []


async def test_a_report_that_may_have_posted_is_found_by_its_marker(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    discord.crash_after["send_message"] = ConnectionError("lost")
    result = await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=60)],
    )
    assert len(discord.posts) == 1
    await gateway.add_note(result.incident_id, source="triage", text="follow-up")
    assert len(discord.posts) == 1
    assert discord.edits and "follow-up" in discord.edits[-1]["content"]


async def test_outbox_rows_are_settled_and_emptied(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=60)],
    )
    [row] = await rows(db, ModerationOutbox)
    assert row.state == "sent"
    assert row.payload is None


async def test_evidence_is_kept_once_per_message(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    await gateway.act(spam_trigger(), [])
    await gateway.act(
        spam_trigger(actor="attachment_filter", reason="blocked file"), []
    )
    evidence = await rows(
        db, ModerationIncidentEntry, ModerationIncidentEntry.kind == t.ENTRY_EVIDENCE
    )
    reasons = await rows(
        db, ModerationIncidentEntry, ModerationIncidentEntry.kind == t.ENTRY_REASON
    )
    assert len(evidence) == 1
    assert len(reasons) == 2


async def test_a_webhook_token_in_a_post_is_never_kept_or_reported(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    leak = "oops https://discord.com/api/webhooks/123456789/abcDEF-_token"

    await gateway.act(spam_trigger(content=leak), [])

    [evidence] = await rows(
        db, ModerationIncidentEntry, ModerationIncidentEntry.kind == t.ENTRY_EVIDENCE
    )
    assert "abcDEF-_token" not in evidence.content
    assert "123456789" in evidence.content
    assert "abcDEF-_token" not in discord.latest_report_text()


# ---------------------------------------------------------------------------
# Review findings (#238): each pins one defect found in review
# ---------------------------------------------------------------------------


async def test_the_bots_own_timeout_stays_its_own_after_other_rows_change(
    discord, db, clock
):
    """Ownership is by the stored expiry, not by which row was written last."""
    from smarter_dev.web.moderation_gateway.gateway import link_audit_entry

    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        handler_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=600)],
    )
    clock.now += 1
    await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )
    # The audit echoes of both timeouts touch their rows, oldest last.
    assert await link_audit_entry(
        guild_id=GUILD,
        target_user_id=SCAMMER,
        kinds=(t.TIMEOUT,),
        audit_entry_id="1",
        session_factory=db,
    )
    assert await link_audit_entry(
        guild_id=GUILD,
        target_user_id=SCAMMER,
        kinds=(t.TIMEOUT,),
        audit_entry_id="2",
        session_factory=db,
    )

    released = await gateway.act(
        handler_trigger(reason="cleared"),
        [Proposal(kind=t.UNTIMEOUT, target_user_id=SCAMMER)],
    )

    assert released.outcomes[0].state == t.CONFIRMED
    assert discord.members[SCAMMER] is None


async def test_a_release_whose_outcome_was_unknown_still_ends_its_claim(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        handler_trigger(),
        [
            Proposal(
                kind=t.TIMEOUT,
                target_user_id=SCAMMER,
                duration_seconds=86400,
                claim="a",
            )
        ],
    )
    discord.crash_after["set_timeout"] = ConnectionError("lost")
    lost = await gateway.act(
        handler_trigger(),
        [Proposal(kind=t.UNTIMEOUT, target_user_id=SCAMMER, claim="a")],
    )
    assert lost.outcomes[0].state == t.UNKNOWN
    clock.now += 30
    await gateway.reconcile_pending()

    await gateway.act(
        handler_trigger(message_id="8002"),
        [
            Proposal(
                kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=3600, claim="b"
            )
        ],
    )
    released = await gateway.act(
        handler_trigger(message_id="8002"),
        [Proposal(kind=t.UNTIMEOUT, target_user_id=SCAMMER, claim="b")],
    )

    assert released.outcomes[0].state == t.CONFIRMED
    assert discord.members[SCAMMER] is None


async def test_a_found_report_whose_edit_failed_is_not_posted_again(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    discord.crash_after["send_message"] = ConnectionError("lost")
    result = await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=60)],
    )
    discord.fail["edit_message"] = ConnectionError("lost again")
    await gateway.add_note(result.incident_id, source="triage", text="one")
    await gateway.add_note(result.incident_id, source="triage", text="two")

    assert len(discord.reports()) == 1
    assert "two" in discord.latest_report_text()


async def test_a_deleted_report_is_posted_anew(discord, db, clock):
    from smarter_dev.web.moderation_gateway.types import EffectNotFound

    gateway = make_gateway(discord, db, clock)
    result = await gateway.act(spam_trigger(), [])
    discord.fail["edit_message"] = EffectNotFound("unknown message")

    await gateway.act(handler_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)])

    assert len(discord.reports()) == 2
    assert f"Banned <@{SCAMMER}>" in discord.reports()[-1]["content"]
    assert result.incident_id is not None


async def test_the_report_keeps_its_marker_and_newest_effect_when_too_long(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    for index in range(15):
        await gateway.act(spam_trigger(actor=f"handler:h{index}", reason="r" * 190), [])
    await gateway.act(handler_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)])

    text = discord.latest_report_text()
    assert len(text) <= 2000
    assert "-# Incident " in text
    assert f"Banned <@{SCAMMER}>" in text


async def test_a_kick_whose_answer_was_lost_is_recorded_once_checked(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    discord.crash_after["kick"] = ConnectionError("lost")
    await gateway.act(
        handler_trigger(), [Proposal(kind=t.KICK, target_user_id=SCAMMER)]
    )
    clock.now += 30

    await gateway.reconcile_pending()

    assert [row.action_type for row in await rows(db, ModerationAction)] == ["kick"]


async def test_a_long_hold_key_still_fits_the_operation_key(discord, db, clock):
    gateway = make_gateway(discord, db, clock)
    claim = f"{'h' * 36}:hold:{'k' * 128}"
    await gateway.act(
        handler_trigger(),
        [
            Proposal(
                kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=60, claim=claim
            )
        ],
    )
    [op] = await rows(db, ModerationOperation)
    assert len(op.idempotency_key) <= 200
    assert op.claim_key == claim


async def test_a_held_lock_is_renewed_past_its_lease(db, clock):
    import time

    real = MemberLocks(db, ttl=0.6, wait=0.3, local=_LocalLocks())
    rival = MemberLocks(db, ttl=0.6, wait=0.3, local=_LocalLocks())
    async with real.hold(GUILD, SCAMMER):
        await asyncio.sleep(1.0)  # longer than the lease
        with pytest.raises(Exception, match="not free"):
            async with rival.hold(GUILD, SCAMMER):
                pass
    assert time.time()


async def test_a_retried_delete_belongs_to_the_incident_that_retried_it(
    discord, db, clock
):
    gateway = make_gateway(discord, db, clock)
    delete = Proposal(
        kind=t.DELETE,
        target_user_id=SCAMMER,
        channel_id=CHANNEL,
        message_ids=(MESSAGE,),
    )
    discord.fail["delete_messages"] = ConnectionError("never arrived")
    first = await gateway.act(spam_trigger(), [delete])
    assert first.outcomes[0].state == t.UNKNOWN
    discord.post(CHANNEL, MESSAGE, SCAMMER, "still here")
    clock.now += 30
    await gateway.reconcile_pending()  # still there: not applied

    second = await gateway.act(
        spam_trigger(message_id="8100", aliases=("other",), content="different"),
        [delete],
    )

    assert second.outcomes[0].state == t.CONFIRMED
    assert second.incident_id != first.incident_id
    [action] = await rows(db, ModerationAction)
    assert action.incident_id == second.incident_id


def test_the_report_is_a_timeline_then_a_summary():
    """#333: Zech found incident 4d3f0c64's report noisy (three reasons, an
    "already covered" row, a contradictory note, the history twice). It is
    one stamped line per step in time order, then one summary line."""
    from uuid import UUID

    from smarter_dev.web.moderation_gateway.report import ReportEntry
    from smarter_dev.web.moderation_gateway.report import ReportFacts
    from smarter_dev.web.moderation_gateway.report import ReportOperation
    from smarter_dev.web.moderation_gateway.report import render_report

    user, start = "1024298331503083550", 1_792_849_330.0
    hold = "possible scam under review: four or more images"

    def op(kind, state, at, **values):
        fields = {
            "kind": kind,
            "state": state,
            "actor": "coordinator",
            "target_user_id": user,
            "target_username": "luluhoney",
            "detail": None,
            "channel_id": None,
            "message_ids": (),
            "deleted": (),
            "duration_seconds": None,
            "expires_at": None,
            "at": start + at,
        }
        fields.update(values)
        return ReportOperation(**fields)

    def entry(kind, at, rule, details=None):
        return ReportEntry(
            kind=kind,
            source="coordinator",
            rule=rule,
            content=None,
            target_user_id=user,
            channel_id=None,
            message_id=None,
            at=start + at,
            details=details,
        )

    history = f"History: no earlier moderation actions on record for <@{user}>."
    facts = ReportFacts(
        incident_id=UUID("4d3f0c64-5dd1-4ac0-8daf-a6e207e85347"),
        guild_id=GUILD,
        subject_user_id=user,
        channel_id="7001",
        trigger_message_id="8001",
        opened_at=start,
        ping_role_id=None,
        entries=(
            entry(t.ENTRY_REASON, 0.1, hold),
            entry(t.ENTRY_REASON, 4.1, "confirmed scam"),
            entry(t.ENTRY_HISTORY, 8, history, {"count": 0}),
            entry(t.ENTRY_SUMMARY, 8, "Confirmed scam from a likely hacked account"),
            entry(
                t.ENTRY_DELIVERY,
                7,
                f"DM to <@{user}> delivered.",
                {"delivered": True, "message": t.MESSAGE_DM},
            ),
            entry(t.ENTRY_HISTORY, 10, history, {"count": 0}),
        ),
        operations=(
            op(
                t.TIMEOUT,
                t.CONFIRMED,
                0.5,
                duration_seconds=300,
                expires_at=start + 300,
                reason=hold,
                provisional=True,
            ),
            op(
                t.TIMEOUT,
                t.COVERED,
                2.6,
                duration_seconds=300,
                provisional=True,
                detail="already timed out",
            ),
            op(
                t.TIMEOUT,
                t.CONFIRMED,
                4.7,
                duration_seconds=86400,
                expires_at=start + 86404,
                reason="confirmed scam",
            ),
            op(
                t.DELETE,
                t.CONFIRMED,
                5.2,
                channel_id="7001",
                message_ids=("8001",),
                deleted=("8001",),
            ),
            op(
                t.DELETE,
                t.CONFIRMED,
                5.9,
                channel_id="7002",
                message_ids=("8002",),
                deleted=("8002",),
            ),
            op(
                t.DELETE,
                t.SKIPPED,
                6.3,
                channel_id="7002",
                message_ids=("8002",),
                detail="already deleted",
            ),
        ),
    )

    who = f"<@{user}> luluhoney"
    link = f"https://discord.com/channels/{GUILD}"
    assert render_report(facts).splitlines() == [
        "🛡️ **Moderation incident** in <#7001> · <t:1792849330:f>",
        f"**Member:** <@{user}> · luluhoney · {user}",
        f"**Message:** {link}/7001/8001",
        f"- <t:1792849330:f> Held {who} for 5 minutes: {hold}",
        f"- <t:1792849334:T> Hold on {who} upgraded to a 24-hour timeout "
        "(until <t:1792935734:f>): confirmed scam",
        f"- <t:1792849335:T> Deleted a message from {who}: {link}/7001/8001",
        f"- <t:1792849335:T> Deleted a message from {who}: {link}/7002/8002",
        f"- <t:1792849337:T> DM to <@{user}> delivered.",
        "**Summary:** Confirmed scam from a likely hacked account: 2 messages "
        "removed, timed out for 24 hours, told by DM. No earlier actions.",
        "To undo a timeout: open the member and choose Remove Timeout.",
        "-# Incident 4d3f0c64 · edited as it develops",
    ]


def test_a_refusal_to_act_on_staff_still_shows_while_no_ops_do_not():
    """Idle effects get no line; skips a moderator needs to see do (#342)."""
    from uuid import uuid4

    from smarter_dev.web.moderation_gateway.report import ReportFacts
    from smarter_dev.web.moderation_gateway.report import ReportOperation
    from smarter_dev.web.moderation_gateway.report import render_report

    def timeout(state, detail):
        return ReportOperation(
            kind=t.TIMEOUT,
            state=state,
            actor="coordinator",
            target_user_id=SCAMMER,
            target_username=None,
            detail=detail,
            channel_id=None,
            message_ids=(),
            deleted=(),
            duration_seconds=86400,
            expires_at=None,
        )

    text = render_report(
        ReportFacts(
            incident_id=uuid4(),
            guild_id=GUILD,
            subject_user_id=SCAMMER,
            channel_id=CHANNEL,
            trigger_message_id=MESSAGE,
            opened_at=1_790_000_000.0,
            ping_role_id=None,
            operations=(
                timeout(t.SKIPPED, "protected: staff role"),
                timeout(t.COVERED, "already timed out"),
                timeout(t.SKIPPED, "weaker than the ban already in this incident"),
                timeout(t.SKIPPED, "not a member"),
            ),
        )
    )

    assert f"<@{SCAMMER}>: timeout 1 day: not done (protected: staff role)" in text
    assert f"<@{SCAMMER}>: timeout 1 day: not done (not a member)" in text
    assert "already timed out" not in text and "weaker than" not in text
    assert (
        "**Summary:** Recorded: **refused on a protected member**, "
        "not done: not a member."
    ) in text


def _report_op(kind, state, at, **values):
    from smarter_dev.web.moderation_gateway.report import ReportOperation

    fields = {
        "kind": kind,
        "state": state,
        "actor": "coordinator",
        "target_user_id": SCAMMER,
        "target_username": None,
        "detail": None,
        "channel_id": CHANNEL,
        "message_ids": (),
        "deleted": (),
        "duration_seconds": None,
        "expires_at": None,
        "at": at,
    }
    fields.update(values)
    return ReportOperation(**fields)


def _report_entry(kind, at, rule, details=None, target=SCAMMER):
    from smarter_dev.web.moderation_gateway.report import ReportEntry

    return ReportEntry(
        kind=kind,
        source="coordinator",
        rule=rule,
        content=None,
        target_user_id=target,
        channel_id=None,
        message_id=None,
        at=at,
        details=details,
    )


def _report(operations=(), entries=(), opened_at=1_790_000_000.0, **render):
    from uuid import uuid4

    from smarter_dev.web.moderation_gateway.report import ReportFacts
    from smarter_dev.web.moderation_gateway.report import render_report

    return render_report(
        ReportFacts(
            incident_id=uuid4(),
            guild_id=GUILD,
            subject_user_id=SCAMMER,
            channel_id=CHANNEL,
            trigger_message_id=MESSAGE,
            opened_at=opened_at,
            ping_role_id=None,
            entries=tuple(entries),
            operations=tuple(operations),
        ),
        **render,
    )


def test_a_moderator_stand_down_shows_in_the_timeline_and_the_summary():
    """#342: a moderator lifts the hold; the scam timeout that follows stands
    down. The report showed nothing for it."""
    start = 1_790_000_000.0
    stand_down = "a moderator reversed it (untimeout); automation stands down"
    text = _report(
        operations=(
            _report_op(
                t.TIMEOUT,
                t.CONFIRMED,
                start + 1,
                duration_seconds=300,
                expires_at=start + 301,
                provisional=True,
                reason="possible scam under review: a link",
            ),
            _report_op(
                t.TIMEOUT,
                t.SKIPPED,
                start + 9,
                duration_seconds=86400,
                detail=stand_down,
            ),
        ),
        entries=(_report_entry(t.ENTRY_SUMMARY, start + 9, "Confirmed scam"),),
    )

    lines = text.splitlines()
    assert (
        f"- <t:{int(start) + 9}:T> <@{SCAMMER}>: timeout 1 day: not done ({stand_down})"
    ) in lines
    assert (
        "**Summary:** Confirmed scam: held for 5 minutes, stood down after a "
        "moderator reversed it."
    ) in lines


def test_someone_elses_timeout_and_a_ban_already_in_place_show():
    start = 1_790_000_000.0
    text = _report(
        operations=(
            _report_op(
                t.TIMEOUT,
                t.SKIPPED,
                start,
                duration_seconds=86400,
                detail="a timeout someone else set runs until <t:1790003600:f>; "
                "left as it is",
            ),
            _report_op(t.BAN, t.SKIPPED, start + 1, detail="already banned"),
            _report_op(
                t.DELETE,
                t.SKIPPED,
                start + 2,
                message_ids=(MESSAGE,),
                detail="already deleted",
            ),
        ),
    )

    assert "left as it is" in text and "(already banned)" in text
    assert "already deleted" not in text
    assert text.splitlines()[-2] == (
        "**Summary:** Recorded: left a timeout someone else set, already banned."
    )


def test_a_trimmed_report_keeps_the_staff_refusal():
    """#342: with about 80 deletes, a staff refusal ranked with confirmed
    effects and vanished into "…and 65 more (65 confirmed)"."""
    start = 1_790_000_000.0
    refusal = _report_op(
        t.TIMEOUT,
        t.SKIPPED,
        start,
        target_user_id="6001",
        target_username="a moderator",
        duration_seconds=604800,
        detail="protected: staff role",
    )
    deletes = [
        _report_op(
            t.DELETE,
            t.CONFIRMED,
            start + 1 + i,
            target_user_id=str(9000 + i),
            target_username=f"member{i}",
            message_ids=(str(8100 + i),),
            deleted=(str(8100 + i),),
        )
        for i in range(80)
    ]
    text = _report(operations=(refusal, *deletes))

    assert len(text) <= 2000
    assert "<@6001> a moderator: timeout 7 days: not done (protected: staff role)" in (
        text
    )
    assert "more (" in text and "refused" not in text.split("more (")[1].split(")")[0]
    assert "**refused on a protected member**" in text.splitlines()[-2]


def test_a_dropped_staff_refusal_is_counted_as_one():
    start = 1_790_000_000.0
    refusals = [
        _report_op(
            t.TIMEOUT,
            t.SKIPPED,
            start + i,
            target_user_id=str(6000 + i),
            target_username="staff",
            duration_seconds=604800,
            detail="protected: staff role " + "x" * 120,
        )
        for i in range(30)
    ]
    text = _report(operations=refusals)

    assert len(text) <= 2000
    assert "more (**22 refused (protected)**)" in text
    assert "**Summary:** Recorded: **refused on 30 protected members**." in text


def test_the_summary_tells_every_dm_not_only_the_last():
    start = 1_790_000_000.0

    def delivery(at, delivered, kind):
        word = "delivered." if delivered else "not delivered."
        return _report_entry(
            t.ENTRY_DELIVERY,
            at,
            f"{kind} to <@{SCAMMER}> {word}",
            {"delivered": delivered, "message": kind},
        )

    missed_first = _report(
        entries=(
            delivery(start, False, t.MESSAGE_WARNING_DM),
            delivery(start + 5, True, t.MESSAGE_DM),
        )
    )
    all_told = _report(entries=(delivery(start, True, t.MESSAGE_DM),))

    assert "**1 of 2 DMs not delivered**" in missed_first
    assert "told by DM" not in missed_first
    assert "told by DM" in all_told


def test_a_confirmed_lead_outranks_a_later_unknown_one():
    start = 1_790_000_000.0
    text = _report(
        entries=(
            _report_entry(t.ENTRY_SUMMARY, start, "Confirmed scam", {"priority": 3}),
            _report_entry(
                t.ENTRY_SUMMARY, start + 5, "Scam check incomplete", {"priority": 1}
            ),
        )
    )
    newer = _report(
        entries=(
            _report_entry(
                t.ENTRY_SUMMARY, start, "Scam check incomplete", {"priority": 1}
            ),
            _report_entry(
                t.ENTRY_SUMMARY, start + 5, "Confirmed scam", {"priority": 3}
            ),
        )
    )

    assert "**Summary:** Confirmed scam." in text
    assert "**Summary:** Confirmed scam." in newer


def test_the_timeline_dates_its_first_line_and_each_new_day():
    """#342: a bare time is ambiguous across midnight."""
    late = 1_792_886_340.0  # 23:59 UTC
    text = _report(
        operations=(
            _report_op(t.WARN, t.CONFIRMED, late, reason="spam"),
            _report_op(t.WARN, t.CONFIRMED, late + 30, reason="spam again"),
            _report_op(t.WARN, t.CONFIRMED, late + 90, reason="and again"),
            _report_op(t.WARN, t.CONFIRMED, late + 120, reason="once more"),
        ),
        opened_at=late,
    )

    stamps = [
        line.split(">")[0] for line in text.splitlines() if line.startswith("- <t:")
    ]
    assert stamps == [
        f"- <t:{int(late)}:f",
        f"- <t:{int(late) + 30}:T",
        f"- <t:{int(late) + 90}:f",
        f"- <t:{int(late) + 120}:T",
    ]


def test_trimming_keeps_failures_and_alerts_and_drops_the_least_important_first():
    from uuid import uuid4

    from smarter_dev.web.moderation_gateway.report import ReportEntry
    from smarter_dev.web.moderation_gateway.report import ReportFacts
    from smarter_dev.web.moderation_gateway.report import ReportOperation
    from smarter_dev.web.moderation_gateway.report import render_report

    def op(kind, state, user, *, detail=None, messages=()):
        return ReportOperation(
            kind=kind,
            state=state,
            actor="spam_ring",
            target_user_id=user,
            target_username=f"member{user}",
            detail=detail,
            channel_id=CHANNEL,
            message_ids=tuple(messages),
            deleted=tuple(messages) if state == t.CONFIRMED else (),
            duration_seconds=604800,
            expires_at=1_790_600_000.0,
        )

    def entry(kind, text, source="spam_ring"):
        note = kind in t.NOTE_KINDS
        return ReportEntry(
            kind=kind,
            source=source,
            rule=text if note or kind == t.ENTRY_REASON else None,
            content=None if note else text,
            target_user_id=None,
            channel_id=None,
            message_id=None,
        )

    # A 20-account ring: 3 timeouts failed, the rest confirmed, plus deletes.
    users = [str(9000 + i) for i in range(20)]
    operations = [
        op(
            t.TIMEOUT,
            t.FAILED if i in (3, 8, 15) else t.CONFIRMED,
            user,
            detail="missing permission" if i in (3, 8, 15) else None,
        )
        for i, user in enumerate(users)
    ] + [
        op(t.DELETE, t.CONFIRMED, user, messages=(str(8100 + i),))
        for i, user in enumerate(users)
    ]
    facts = ReportFacts(
        incident_id=uuid4(),
        guild_id=GUILD,
        subject_user_id=users[0],
        channel_id=CHANNEL,
        trigger_message_id=MESSAGE,
        opened_at=1_790_000_000.0,
        ping_role_id=None,
        entries=(
            entry(t.ENTRY_REASON, "hackathon spam ring"),
            entry(t.ENTRY_EVIDENCE, "join our hackathon " * 10),
            entry(t.ENTRY_ASSESSMENT, "the classifier said " * 30, source="triage"),
            entry(t.ENTRY_NOTE, "A notice was posted in the channel."),
            entry(
                t.ENTRY_ALERT,
                "**The participant check failed**, so only the ad's author was "
                "actioned.",
            ),
        ),
        operations=tuple(operations),
    )

    text = render_report(facts)

    assert len(text) <= 2000
    for i in (3, 8, 15):
        assert f"<@{users[i]}> member{users[i]}: timeout 7 days: **failed**" in text
    assert "The participant check failed" in text
    assert "-# Incident " in text
    # The newest effect stays; what was dropped is counted, not hidden.
    assert f"Deleted a message from <@{users[-1]}>" in text
    assert "more (" in text and "confirmed" in text
    # Quotes and the long assessment went before any failure did.
    assert "join our hackathon" not in text
    assert "the classifier said" not in text


def test_failures_and_alerts_that_overflow_alone_are_counted_not_cut():
    # #249's delta review: 40 failed timeouts cut the alert and the tail of
    # the failed lines, with no count.
    from uuid import uuid4

    from smarter_dev.web.moderation_gateway.report import ReportEntry
    from smarter_dev.web.moderation_gateway.report import ReportFacts
    from smarter_dev.web.moderation_gateway.report import ReportOperation
    from smarter_dev.web.moderation_gateway.report import render_report

    def alert(text):
        return ReportEntry(
            kind=t.ENTRY_ALERT,
            source="spam_ring",
            rule=text,
            content=None,
            target_user_id=None,
            channel_id=None,
            message_id=None,
        )

    users = [str(9000 + i) for i in range(40)]
    facts = ReportFacts(
        incident_id=uuid4(),
        guild_id=GUILD,
        subject_user_id=users[0],
        channel_id=CHANNEL,
        trigger_message_id=MESSAGE,
        opened_at=1_790_000_000.0,
        ping_role_id=None,
        entries=tuple(
            alert(f"**Sweep {i} failed**, " + "so some accounts were missed. " * 4)
            for i in range(12)
        ),
        operations=tuple(
            ReportOperation(
                kind=t.TIMEOUT,
                state=t.FAILED,
                actor="spam_ring",
                target_user_id=user,
                target_username=f"member{user}",
                detail="missing permission",
                channel_id=CHANNEL,
                message_ids=(),
                deleted=(),
                duration_seconds=604800,
                expires_at=None,
            )
            for user in users
        ),
    )

    text = render_report(facts)

    assert len(text) <= 2000
    assert text.rstrip().endswith("edited as it develops")
    assert "…" not in text.replace("…and", "")
    shown = sum(f"<@{user}> member{user}: timeout" in text for user in users)
    assert shown < 40
    assert f"- …and {40 - shown} more (**{40 - shown} failed or unresolved**)" in text
    assert "Sweep 11 failed" in text
    shown_alerts = sum(f"Sweep {i} failed" in text for i in range(12))
    assert f"…and {12 - shown_alerts} earlier alert(s)" in text


def test_the_newest_failure_stays_while_the_alerts_shrink():
    # #254's review: with the newest effect confirmed, every failure line
    # went while three full alerts stayed. Alerts now shrink first.
    from uuid import uuid4

    from smarter_dev.web.moderation_gateway.report import ReportEntry
    from smarter_dev.web.moderation_gateway.report import ReportFacts
    from smarter_dev.web.moderation_gateway.report import ReportOperation
    from smarter_dev.web.moderation_gateway.report import render_report

    def timeout(user, state, detail=None):
        return ReportOperation(
            kind=t.TIMEOUT,
            state=state,
            actor="spam_ring",
            target_user_id=user,
            target_username=f"member{user}",
            detail=detail,
            channel_id=CHANNEL,
            message_ids=(),
            deleted=(),
            duration_seconds=604800,
            expires_at=1_790_600_000.0 if state == t.CONFIRMED else None,
        )

    def alert(i):
        return ReportEntry(
            kind=t.ENTRY_ALERT,
            source="spam_ring",
            rule=f"**Check {i} failed**, " + "x" * 380,
            content=None,
            target_user_id=None,
            channel_id=None,
            message_id=None,
        )

    users = [str(9000 + i) for i in range(11)]
    facts = ReportFacts(
        incident_id=uuid4(),
        guild_id=GUILD,
        subject_user_id=users[0],
        channel_id=CHANNEL,
        trigger_message_id=MESSAGE,
        opened_at=1_790_000_000.0,
        ping_role_id=None,
        entries=tuple(alert(i) for i in range(6)),
        operations=(
            *(
                timeout(user, t.FAILED, "missing permission " * 8)
                for user in users[:10]
            ),
            timeout(users[10], t.CONFIRMED),
        ),
    )

    text = render_report(facts)

    assert len(text) <= 2000
    assert f"<@{users[9]}> member{users[9]}: timeout 7 days: **failed**" in text
    assert f"Timed out <@{users[10]}> member{users[10]} for 7 days" in text
    assert "- …and 9 more (**9 failed or unresolved**)" in text
    assert "Check 5 failed" in text
    assert "…and 3 earlier alert(s)" in text

    # Tighter still: the last failure goes only once a single alert is left.
    tight = render_report(facts, limit=1000)

    assert len(tight) <= 1000
    assert "- …and 10 more (**10 failed or unresolved**)" in tight
    assert f"Timed out <@{users[10]}> member{users[10]} for 7 days" in tight
    assert "Check 5 failed" in tight
    assert "…and 5 earlier alert(s)" in tight
    assert "…\n" not in tight  # no blind cut from the middle


async def test_the_spam_engines_ping_goes_out_when_it_joins_a_posted_report(
    discord, db, clock
):
    # Triage opened the incident and posted its report without a ping.
    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        spam_trigger(actor="triage", ping_role_id=None),
        [
            Proposal(
                kind=t.DELETE,
                target_user_id=SCAMMER,
                channel_id=CHANNEL,
                message_ids=(MESSAGE,),
            )
        ],
    )
    assert len(discord.reports()) == 1
    assert not discord.reports()[0]["role_mention_ids"]
    clock.now += 3

    await gateway.act(
        spam_trigger(),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )
    clock.now += 3
    await gateway.act(handler_trigger(), [Proposal(kind=t.BAN, target_user_id=SCAMMER)])

    assert len(discord.reports()) == 1
    [ping] = discord.pings()
    assert ping["role_mention_ids"] == ["644390354157568014"]
    report_id = discord.reports()[0]["message_id"]
    assert ping["content"].endswith(f"/{REPORT_CHANNEL}/{report_id}")
    assert f"Banned <@{SCAMMER}>" in discord.latest_report_text()


# -- #376: the member in plain text -----------------------------------------


async def test_the_report_names_the_member_once_as_they_were(discord, db, clock):
    """A mention renders as "unknown-user" on a client that has not cached
    the member: the report also names them in plain text, with the id to
    copy, from the first trigger that knew them."""
    gateway = make_gateway(discord, db, clock)
    delete = Proposal(
        kind=t.DELETE,
        target_user_id=SCAMMER,
        channel_id=CHANNEL,
        message_ids=(MESSAGE,),
    )
    await gateway.act(
        spam_trigger(author_username="d1sc_0n3ct", author_display_name="Dis*Co"),
        [delete],
    )
    await gateway.act(
        spam_trigger(
            message_id="8100",
            aliases=("burst",),
            related_message_ids=(MESSAGE,),
            author_username="renamed",
        ),
        [],
    )

    report = discord.latest_report_text(REPORT_CHANNEL)
    [member] = [line for line in report.splitlines() if line.startswith("**Member")]
    assert member == (f"**Member:** <@{SCAMMER}> · d1sc\\_0n3ct (Dis\\*Co) · {SCAMMER}")
    assert report.count("d1sc") == 1
    assert "renamed" not in report


async def test_a_member_nobody_named_is_said_to_be_unknown(discord, db, clock):
    """Control: no username from the trigger and none on an operation."""
    gateway = make_gateway(discord, db, clock)
    await gateway.act(
        spam_trigger(),
        [
            Proposal(
                kind=t.DELETE,
                target_user_id=SCAMMER,
                channel_id=CHANNEL,
                message_ids=(MESSAGE,),
            )
        ],
    )

    report = discord.latest_report_text(REPORT_CHANNEL)
    [member] = [line for line in report.splitlines() if line.startswith("**Member")]
    assert member == (f"**Member:** <@{SCAMMER}> · username unknown · {SCAMMER}")
