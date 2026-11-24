"""Tests for jam storage: overlap, the send claim and its reports."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.jams import store
from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamMessage
from tests.web.jam_helpers import GUILD
from tests.web.jam_helpers import START
from tests.web.jam_helpers import make_jam
from tests.web.jam_helpers import make_message
from tests.web.jam_helpers import make_setup

_OTHER_GUILD = "222222222222222222"
_DUE = START + timedelta(minutes=1)


async def _seed(db_session, *, setup=True, **message) -> tuple[Jam, JamMessage]:
    jam = make_jam()
    db_session.add(jam)
    if setup:
        db_session.add(make_setup())
    await db_session.commit()
    row = make_message(jam, **message)
    db_session.add(row)
    await db_session.commit()
    return jam, row


async def _status(db_session, message: JamMessage) -> JamMessage:
    await db_session.refresh(message)
    return message


# -- overlap ------------------------------------------------------------------


async def test_overlap_uses_the_last_date_a_jam_has(db_session):
    jam = make_jam(
        voting_ends_at=START + timedelta(days=40),
        winners_at=START + timedelta(days=41),
    )
    db_session.add(jam)
    await db_session.commit()
    end = START + timedelta(days=41)

    async def clash(starts, ends):
        return await store.overlapping_jam(db_session, GUILD, starts, ends)

    assert await clash(end + timedelta(seconds=1), end + timedelta(days=5)) is None
    assert await clash(START - timedelta(days=9), START - timedelta(seconds=1)) is None
    # Touching ends count: the tagging rule needs one jam at any moment.
    assert (await clash(end, end + timedelta(days=5))).id == jam.id
    assert (await clash(START - timedelta(days=9), START)).id == jam.id
    assert (await clash(START + timedelta(days=1), START + timedelta(days=2))).id == (
        jam.id
    )
    assert (await clash(START - timedelta(days=1), end + timedelta(days=1))).id == (
        jam.id
    )


async def test_overlap_ignores_the_jam_being_edited_and_other_guilds(db_session):
    jam = make_jam()
    elsewhere = make_jam(guild_id=_OTHER_GUILD)
    db_session.add_all([jam, elsewhere])
    await db_session.commit()
    ends = START + timedelta(days=33)

    assert (
        await store.overlapping_jam(
            db_session, GUILD, START, ends, except_jam_id=jam.id
        )
        is None
    )
    assert (await store.overlapping_jam(db_session, GUILD, START, ends)).id == jam.id


async def test_get_jam_is_scoped_to_its_guild(db_session):
    jam = make_jam()
    db_session.add(jam)
    await db_session.commit()

    assert (await store.get_jam(db_session, GUILD, jam.id)).id == jam.id
    assert await store.get_jam(db_session, _OTHER_GUILD, jam.id) is None


# -- showcase forum -----------------------------------------------------------


async def test_changing_the_showcase_forum_forgets_tag_ids(db_session):
    jam = make_jam(showcase_tag_id="1", year_tag_id="2")
    other_guild_jam = make_jam(guild_id=_OTHER_GUILD, showcase_tag_id="3")
    db_session.add_all([jam, other_guild_jam, make_setup()])
    await db_session.commit()

    await store.set_showcase_forum(db_session, GUILD, "14")
    await db_session.commit()
    await db_session.refresh(jam)
    assert (jam.showcase_tag_id, jam.year_tag_id) == ("1", "2")

    await store.set_showcase_forum(db_session, GUILD, "77")
    await db_session.commit()
    await db_session.refresh(jam)
    await db_session.refresh(other_guild_jam)

    assert (await store.get_setup(db_session, GUILD)).showcase_channel_id == "77"
    assert (jam.showcase_tag_id, jam.year_tag_id) == (None, None)
    assert other_guild_jam.showcase_tag_id == "3"


async def test_showcase_forum_can_be_set_before_setup_ever_ran(db_session):
    await store.set_showcase_forum(db_session, GUILD, "77")
    await db_session.commit()

    setup = await store.get_setup(db_session, GUILD)
    assert setup.showcase_channel_id == "77"
    assert "showcase" not in setup.missing_parts
    assert "category" in setup.missing_parts


# -- claim --------------------------------------------------------------------


async def test_claim_returns_a_due_message_once(db_session):
    _, message = await _seed(db_session, mention_role=True)

    first = await store.claim_due_messages(db_session, _DUE)
    second = await store.claim_due_messages(db_session, _DUE)

    assert [(m.id, m.guild_id, m.channel_id, m.content, m.role_id) for m in first] == [
        (message.id, GUILD, "11", "The jam is open!", "20")
    ]
    assert second == []
    assert (await _status(db_session, message)).status == "sending"


async def test_claim_leaves_the_role_out_unless_the_message_pings(db_session):
    await _seed(db_session, channel_key="teams")

    (outgoing,) = await store.claim_due_messages(db_session, _DUE)

    assert outgoing.channel_id == "13"
    assert outgoing.role_id is None


async def test_claim_skips_messages_that_are_not_due(db_session):
    _, message = await _seed(db_session)

    assert (
        await store.claim_due_messages(db_session, START - timedelta(seconds=1)) == []
    )
    assert (await _status(db_session, message)).status == "pending"


async def test_claim_orders_by_send_time(db_session):
    jam, first = await _seed(db_session, content="one")
    db_session.add(
        make_message(jam, content="zero", send_at=START - timedelta(minutes=5))
    )
    await db_session.commit()

    outgoing = await store.claim_due_messages(db_session, _DUE)

    assert [m.content for m in outgoing] == ["zero", "one"]


async def test_a_message_too_late_to_send_fails_instead(db_session):
    _, message = await _seed(db_session)
    late = START + store.MAX_SEND_DELAY + timedelta(seconds=1)

    assert await store.claim_due_messages(db_session, late) == []

    message = await _status(db_session, message)
    assert message.status == "failed"
    assert "6 hours" in message.error


async def test_a_message_at_the_delay_limit_still_sends(db_session):
    await _seed(db_session)

    assert (
        len(await store.claim_due_messages(db_session, START + store.MAX_SEND_DELAY))
        == 1
    )


async def test_a_message_with_no_channel_fails_instead(db_session):
    _, message = await _seed(db_session, setup=False)

    assert await store.claim_due_messages(db_session, _DUE) == []

    message = await _status(db_session, message)
    assert message.status == "failed"
    assert "news channel has not been created" in message.error


async def test_a_ping_with_no_role_fails_instead(db_session):
    jam = make_jam()
    db_session.add_all([jam, make_setup(role_id=None)])
    await db_session.commit()
    message = make_message(jam, mention_role=True)
    quiet = make_message(jam, content="no ping")
    db_session.add_all([message, quiet])
    await db_session.commit()

    outgoing = await store.claim_due_messages(db_session, _DUE)

    assert [m.content for m in outgoing] == ["no ping"]
    assert "role has not been created" in (await _status(db_session, message)).error


# -- reports ------------------------------------------------------------------


async def test_mark_sent_needs_a_claim(db_session):
    _, message = await _seed(db_session)

    assert not await store.mark_message_sent(db_session, message.id)
    await store.claim_due_messages(db_session, _DUE)
    assert await store.mark_message_sent(db_session, message.id, _DUE)
    assert not await store.mark_message_sent(db_session, message.id)

    message = await _status(db_session, message)
    assert message.status == "sent"
    assert message.sent_at is not None


async def test_mark_failed_records_a_bounded_reason(db_session):
    _, message = await _seed(db_session)
    await store.claim_due_messages(db_session, _DUE)

    assert await store.mark_message_failed(db_session, message.id, "x" * 900)
    assert not await store.mark_message_failed(db_session, message.id, "again")

    message = await _status(db_session, message)
    assert message.status == "failed"
    assert len(message.error) == 500


async def test_a_sent_message_cannot_be_marked_failed(db_session):
    _, message = await _seed(db_session)
    await store.claim_due_messages(db_session, _DUE)
    await store.mark_message_sent(db_session, message.id)

    assert not await store.mark_message_failed(db_session, message.id, "late report")
    assert (await _status(db_session, message)).status == "sent"


# -- retry --------------------------------------------------------------------


async def test_retry_queues_a_failed_message_for_now(db_session):
    _, message = await _seed(db_session)
    late = START + timedelta(days=2)
    await store.claim_due_messages(db_session, late)
    message = await _status(db_session, message)
    assert message.status == "failed"

    assert await store.retry_message(db_session, message, late)

    assert (message.status, message.error, message.claimed_at) == (
        "pending",
        None,
        None,
    )
    assert len(await store.claim_due_messages(db_session, late)) == 1


async def test_retry_refuses_a_fresh_claim_and_takes_a_stuck_one(db_session):
    _, message = await _seed(db_session)
    await store.claim_due_messages(db_session, _DUE)
    message = await _status(db_session, message)

    at_limit = _DUE + store.STUCK_AFTER
    assert not store.is_stuck(message, at_limit)
    assert not await store.retry_message(db_session, message, at_limit)
    assert message.status == "sending"

    stuck = at_limit + timedelta(seconds=1)
    assert store.is_stuck(message, stuck)
    assert await store.retry_message(db_session, message, stuck)
    assert message.status == "pending"


async def test_retry_refuses_pending_and_sent(db_session):
    _, message = await _seed(db_session)

    assert not await store.retry_message(db_session, message, _DUE)
    await store.claim_due_messages(db_session, _DUE)
    await store.mark_message_sent(db_session, message.id)
    assert not await store.retry_message(db_session, await _status(db_session, message))


async def test_deleting_a_jam_deletes_its_messages(db_session):
    jam, message = await _seed(db_session)

    await db_session.delete(jam)
    await db_session.commit()

    assert await store.get_message(db_session, jam.id, message.id) is None


async def test_retry_is_decided_by_the_row_not_a_stale_copy(db_session, test_engine):
    """The bot reports "sent" after the admin's page loaded the failed row."""
    _, message = await _seed(db_session)
    late = START + timedelta(days=2)
    await store.claim_due_messages(db_session, late)
    message = await _status(db_session, message)
    assert message.status == "failed"
    async with AsyncSession(test_engine) as bot:
        await bot.execute(
            update(JamMessage)
            .where(JamMessage.id == message.id)
            .values(status="sent", error=None)
        )
        await bot.commit()
    assert message.status == "failed"

    assert not await store.retry_message(db_session, message, late)

    assert message.status == "sent"


async def test_an_edit_is_not_saved_over_a_message_the_bot_claimed(
    db_session, test_engine
):
    """The bot claims the message after the edit handler read it as pending."""
    _, message = await _seed(db_session)
    async with AsyncSession(test_engine) as bot:
        assert len(await store.claim_due_messages(bot, START + timedelta(hours=1))) == 1
    assert message.status == "pending"

    saved = await store.save_message_edit(db_session, message, {"content": "New"})

    assert not saved
    assert (message.status, message.content) != ("pending", "New")
    assert message.status == "sending"


async def test_an_edit_does_not_requeue_a_failed_message_sent_meanwhile(
    db_session, test_engine
):
    """ "Send now" and the bot both act after the edit handler read "failed"."""
    _, message = await _seed(db_session, status="failed", error="Too late")
    async with AsyncSession(test_engine) as other:
        await other.execute(
            update(JamMessage)
            .where(JamMessage.id == message.id)
            .values(status="sending", error=None)
        )
        await other.commit()
    assert message.status == "failed"

    assert not await store.save_message_edit(db_session, message, {"content": "New"})

    assert message.status == "sending"


async def test_an_edit_saves_and_only_a_failed_message_changes_state(db_session):
    _, pending = await _seed(db_session)

    assert await store.save_message_edit(db_session, pending, {"content": "New"})
    assert (pending.status, pending.content) == ("pending", "New")

    failed = make_message(pending.jam, status="failed", error="Too late")
    db_session.add(failed)
    await db_session.commit()

    assert await store.save_message_edit(db_session, failed, {"content": "Again"})
    assert (failed.status, failed.error, failed.content) == ("pending", None, "Again")


# -- the guild lock -----------------------------------------------------------


def test_the_lock_statement_is_a_transaction_advisory_lock():
    sql = str(
        store._advisory_lock("42").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert (
        sql
        == "SELECT pg_advisory_xact_lock(hashtextextended('jam:42', 0)) AS pg_advisory_xact_lock_1"
    )


class _Session:
    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.executed: list = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    async def execute(self, statement) -> None:
        self.executed.append(str(statement.compile(dialect=postgresql.dialect())))


async def test_postgres_takes_the_advisory_lock_and_sqlite_does_not():
    postgres, sqlite = _Session("postgresql"), _Session("sqlite")

    async with store.guild_lock(postgres, "42"):
        assert len(postgres.executed) == 1
    async with store.guild_lock(sqlite, "42"):
        pass

    assert "pg_advisory_xact_lock(hashtextextended(" in postgres.executed[0]
    assert sqlite.executed == []


async def test_the_lock_admits_one_holder_per_guild_at_a_time():
    session, order = _Session("sqlite"), []

    async def hold(name: str, guild: str) -> None:
        async with store.guild_lock(session, guild):
            order.append(f"{name} in")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            order.append(f"{name} out")

    await asyncio.gather(hold("a", "1"), hold("b", "1"), hold("c", "2"))

    assert order.index("a out") < order.index("b in")
    assert order.index("c in") < order.index("a out")
