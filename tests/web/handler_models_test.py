"""Tests for the handler data model — name uniqueness keying in particular."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from smarter_dev.web.models import (
    ADMIN_HANDLER_EVENT_TRIGGERS,
    ADMIN_HANDLER_TRIGGER_TYPES,
    ADMIN_ONLY_TRIGGER_TYPES,
    ADMIN_SYNTHETIC_TRIGGER_TYPES,
    HANDLER_EVENT_TRIGGERS,
    HANDLER_TRIGGER_TYPES,
    AdminHandler,
    ChannelHandler,
    GuildHandlerMemory,
)


def _handler(
    trigger_type: str, channel_id: str = "C1", name: str = "helper"
) -> ChannelHandler:
    return ChannelHandler(
        guild_id="G1",
        channel_id=channel_id,
        name=name,
        trigger_type=trigger_type,
        settings={},
        description="d",
        script="await send_message('hi')\n",
        created_by="U1",
    )


async def test_same_trigger_handlers_coexist_under_different_names(db_session):
    db_session.add(_handler("message", name="greeter"))
    db_session.add(_handler("message", name="mood-tracker"))
    await db_session.commit()  # multiple listeners per (channel, trigger) are fine


async def test_name_is_unique_per_channel(db_session):
    db_session.add(_handler("message", name="greeter"))
    await db_session.commit()
    db_session.add(_handler("reaction", name="greeter"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_same_name_in_different_channels(db_session):
    db_session.add(_handler("message", channel_id="C1", name="greeter"))
    db_session.add(_handler("message", channel_id="C2", name="greeter"))
    await db_session.commit()  # uniqueness is per channel


def test_standard_trigger_vocabulary_unchanged():
    # The standard tier's four triggers must not grow (§2, §6).
    assert HANDLER_TRIGGER_TYPES == ("message", "reaction", "schedule", "timer")
    assert HANDLER_EVENT_TRIGGERS == ("message", "reaction")


def test_admin_only_trigger_types_include_member_thread_and_dm():
    assert ADMIN_ONLY_TRIGGER_TYPES == (
        "member_join",
        "member_leave",
        "member_rules_accepted",
        "member_role_change",
        "thread_create",
        "dm_message",
        "message_edit",
    )


def test_message_edit_is_admin_only_not_standard():
    # message_edit is an admin-tier auto-mod trigger; the standard vocabulary
    # does not grow (§3.3), so a member-authored channel handler can't select it.
    assert "message_edit" in ADMIN_HANDLER_TRIGGER_TYPES
    assert "message_edit" not in HANDLER_TRIGGER_TYPES
    assert "message_edit" in ADMIN_HANDLER_EVENT_TRIGGERS


def test_dm_message_is_admin_only_not_standard():
    # A member-authored channel handler must never see other users' DMs, so
    # dm_message is admin-only and stays out of the standard vocabulary (§E1).
    assert "dm_message" in ADMIN_HANDLER_TRIGGER_TYPES
    assert "dm_message" not in HANDLER_TRIGGER_TYPES
    assert "dm_message" in ADMIN_HANDLER_EVENT_TRIGGERS


def test_admin_trigger_tuple_is_the_union_of_standard_and_new():
    assert ADMIN_HANDLER_TRIGGER_TYPES == (
        HANDLER_TRIGGER_TYPES + ADMIN_ONLY_TRIGGER_TYPES + ADMIN_SYNTHETIC_TRIGGER_TYPES
    )
    # Every standard trigger, every new admin-only trigger, and the synthetic
    # mod_action trigger is admissible.
    for trigger in (
        HANDLER_TRIGGER_TYPES + ADMIN_ONLY_TRIGGER_TYPES + ADMIN_SYNTHETIC_TRIGGER_TYPES
    ):
        assert trigger in ADMIN_HANDLER_TRIGGER_TYPES


def test_admin_event_triggers_extend_the_gateway_subset():
    # Gateway-dispatched (event) triggers: message/reaction plus the new admin
    # ones and the synthetic mod_action; the time triggers (schedule/timer) stay
    # out.
    assert ADMIN_HANDLER_EVENT_TRIGGERS == (
        HANDLER_EVENT_TRIGGERS + ADMIN_ONLY_TRIGGER_TYPES + ADMIN_SYNTHETIC_TRIGGER_TYPES
    )
    assert "schedule" not in ADMIN_HANDLER_EVENT_TRIGGERS
    assert "timer" not in ADMIN_HANDLER_EVENT_TRIGGERS


@pytest.mark.parametrize("trigger", ADMIN_ONLY_TRIGGER_TYPES)
async def test_admin_handler_accepts_new_trigger_types(db_session, trigger):
    db_session.add(
        AdminHandler(
            guild_id="G1",
            name=f"{trigger}-handler",
            trigger_type=trigger,
            settings={},
            channel_ids=[],
            description="d",
            script="pass\n",
            created_by_admin="A1",
        )
    )
    await db_session.commit()  # extended CHECK constraint admits it


@pytest.mark.parametrize("trigger", ADMIN_ONLY_TRIGGER_TYPES)
async def test_channel_handler_rejects_new_trigger_types(db_session, trigger):
    # The standard-tier CHECK constraint is deliberately NOT extended (§6).
    db_session.add(_handler(trigger, name="nope"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_handler_run_has_discord_reads_and_thread_ops(db_session):
    from smarter_dev.web.models import HandlerRun
    from uuid import uuid4

    run = HandlerRun(
        handler_id=uuid4(),
        trigger_context={},
        outcome="ok",
        discord_reads=3,
        thread_ops=2,
    )
    db_session.add(run)
    await db_session.commit()
    assert run.discord_reads == 3
    assert run.thread_ops == 2


async def test_handler_run_role_changes_defaults_zero(db_session):
    from smarter_dev.web.models import HandlerRun
    from uuid import uuid4

    run = HandlerRun(handler_id=uuid4(), trigger_context={}, outcome="ok")
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    assert run.role_changes == 0

    graded = HandlerRun(
        handler_id=uuid4(), trigger_context={}, outcome="ok", role_changes=4
    )
    db_session.add(graded)
    await db_session.commit()
    assert graded.role_changes == 4


async def test_admin_handler_name_is_unique_per_guild(db_session):
    def _admin(guild_id: str, name: str) -> AdminHandler:
        return AdminHandler(
            guild_id=guild_id,
            name=name,
            trigger_type="message",
            settings={},
            channel_ids=[],
            description="d",
            script="pass\n",
            created_by_admin="A1",
        )

    db_session.add(_admin("G1", "scam-banner"))
    db_session.add(_admin("G2", "scam-banner"))  # other guild — fine
    await db_session.commit()
    db_session.add(_admin("G1", "scam-banner"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_guild_handler_memory_unique_guild_key_upsert(db_session):
    # Two different keys for the same guild coexist; a duplicate (guild, key)
    # is rejected so the store's per-key upsert has a real conflict target.
    db_session.add(GuildHandlerMemory(guild_id="G1", key="a", value={"v": 1}))
    db_session.add(GuildHandlerMemory(guild_id="G1", key="b", value=2))
    db_session.add(GuildHandlerMemory(guild_id="G2", key="a", value=3))  # other guild
    await db_session.commit()

    db_session.add(GuildHandlerMemory(guild_id="G1", key="a", value={"v": 9}))
    with pytest.raises(IntegrityError):
        await db_session.commit()


def test_trigger_type_columns_fit_their_vocabulary():
    # The test DB is SQLite, which ignores VARCHAR(n) limits, so a too-narrow
    # trigger_type column only surfaces in Postgres (prod): "member_rules_accepted"
    # (21 chars) truncation-errored every insert against a varchar(20) column.
    # This backend-independent invariant guards the column width directly.
    admin_len = AdminHandler.__table__.c.trigger_type.type.length
    longest_admin = max(len(t) for t in ADMIN_HANDLER_TRIGGER_TYPES)
    assert admin_len >= longest_admin, (
        f"admin_handlers.trigger_type varchar({admin_len}) cannot hold "
        f"{longest_admin}-char triggers like "
        f"{max(ADMIN_HANDLER_TRIGGER_TYPES, key=len)!r}"
    )

    channel_len = ChannelHandler.__table__.c.trigger_type.type.length
    longest_channel = max(len(t) for t in HANDLER_TRIGGER_TYPES)
    assert channel_len >= longest_channel, (
        f"channel_handlers.trigger_type varchar({channel_len}) cannot hold "
        f"{longest_channel}-char triggers"
    )
