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
    assert "ban: confirmed" in text
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
    assert "ban: confirmed" in discord.reports()[-1]["content"]
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
    assert "ban: confirmed" in text


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
