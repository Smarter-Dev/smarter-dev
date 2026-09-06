"""The chain that arms a time-triggered handler's fires.

A recurring schedule has no cron daemon: the fire that just ran enqueues the
next occurrence, the sweep re-arms a chain whose fire never came, and an
install or edit arms the first fire. Every path must enqueue the same tier
payload and stamp the same job id, so all of them go through one chain per
tier, and the chain alone knows which handler row its tier keeps.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import smarter_dev.web.handler_recurrence as handler_recurrence
from smarter_dev.web.handler_fire_payloads import AdminHandlerFirePayload
from smarter_dev.web.handler_fire_payloads import HandlerFirePayload
from smarter_dev.web.handler_recurrence import RECURRING_CHAINS
from smarter_dev.web.handler_recurrence import RecurringFireChain
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ChannelHandler

RECURRING_SETTINGS = {"interval_seconds": 300, "start_at": "2099-01-01T00:00:00Z"}
NEXT_OCCURRENCE = datetime(2099, 1, 1, tzinfo=UTC)
FIRST_FIRE = datetime(2099, 6, 1, tzinfo=UTC)
CREATED = datetime(2026, 7, 1, tzinfo=UTC)


class _HandlerModel:
    """Stand-in for the tier's ORM model — only its identity is used."""


class _Record:
    def __init__(self, enabled: bool = True, trigger_type: str = "schedule"):
        self.id = uuid4()
        self.enabled = enabled
        self.trigger_type = trigger_type
        self.scheduled_job_id = None


class _Session:
    def __init__(self, record):
        self._record = record
        self.committed = False
        self.asked_for = None

    async def get(self, model, id_):
        self.asked_for = (model, id_)
        return self._record

    async def commit(self):
        self.committed = True


class _SessionCtx:
    def __init__(self, record):
        self.session = _Session(record)

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def _capture_submits(monkeypatch) -> list:
    submits: list = []

    async def fake_submit(payload, scheduled_for=None, job_id=None):
        submits.append((payload, scheduled_for, job_id))

    monkeypatch.setattr(handler_recurrence, "worker_submit", fake_submit)
    return submits


def _chain_under_test(
    monkeypatch, record
) -> tuple[RecurringFireChain, list, _SessionCtx]:
    submits = _capture_submits(monkeypatch)
    session_ctx = _SessionCtx(record)
    monkeypatch.setattr(handler_recurrence, "get_db_session_context", session_ctx)
    chain = RecurringFireChain(
        handler_model=_HandlerModel,
        build_fire_payload=lambda handler_id, trigger_context: {
            "handler_id": handler_id,
            "trigger_context": trigger_context,
        },
    )
    return chain, submits, session_ctx


# -- rearm_after_fire: the fire that just ran enqueues its successor ----------


@pytest.mark.parametrize(
    ("trigger_type", "trigger_context", "rearms"),
    [
        ("schedule", {"trigger_type": "schedule"}, True),
        ("schedule", {}, True),
        # A schedule handler that self-arms a timer must not fork a second
        # perpetual chain when the timer re-fires it.
        ("schedule", {"trigger_type": "timer"}, False),
        ("message", {"trigger_type": "message"}, False),
        ("timer", {"trigger_type": "timer"}, False),
    ],
)
async def test_only_a_genuine_scheduled_fire_re_arms(
    monkeypatch, trigger_type, trigger_context, rearms
):
    chain, submits, _ = _chain_under_test(monkeypatch, _Record())
    await chain.rearm_after_fire(
        handler_id=uuid4(),
        trigger_type=trigger_type,
        trigger_context=trigger_context,
        handler_settings=RECURRING_SETTINGS,
    )
    assert bool(submits) is rearms


async def test_a_re_arm_enqueues_the_tier_payload_at_the_next_occurrence(monkeypatch):
    record = _Record()
    chain, submits, session_ctx = _chain_under_test(monkeypatch, record)
    await chain.rearm_after_fire(
        handler_id=record.id,
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings=RECURRING_SETTINGS,
    )
    payload, scheduled_for, job_id = submits[0]
    assert payload == {
        "handler_id": str(record.id),
        "trigger_context": {"trigger_type": "schedule"},
    }
    assert scheduled_for == NEXT_OCCURRENCE
    # The enqueued job id is stamped on the tier's row so a later disable or
    # edit can cancel the occurrence this fire just armed.
    assert session_ctx.session.asked_for == (_HandlerModel, record.id)
    assert record.scheduled_job_id == job_id
    assert session_ctx.session.committed is True


async def test_a_one_shot_schedule_ends_the_chain(monkeypatch):
    # Settings with no recurring key (next_fire_at returns None): nothing is
    # enqueued and the row is never even loaded.
    chain, submits, session_ctx = _chain_under_test(monkeypatch, _Record())
    await chain.rearm_after_fire(
        handler_id=uuid4(),
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings={},
    )
    assert submits == []
    assert session_ctx.session.asked_for is None


async def test_a_handler_disabled_mid_fire_is_not_re_armed(monkeypatch):
    # The row is the truth: a chain whose handler was switched off while its
    # fire ran ends here, with no orphan job left in the queue.
    record = _Record(enabled=False)
    chain, submits, _ = _chain_under_test(monkeypatch, record)
    await chain.rearm_after_fire(
        handler_id=record.id,
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings=RECURRING_SETTINGS,
    )
    assert submits == []
    assert record.scheduled_job_id is None


async def test_a_deleted_handler_is_not_re_armed(monkeypatch):
    chain, submits, _ = _chain_under_test(monkeypatch, None)
    await chain.rearm_after_fire(
        handler_id=uuid4(),
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings=RECURRING_SETTINGS,
    )
    assert submits == []


# -- load_enabled_handler: the chain owns its tier's row ----------------------


async def test_load_enabled_handler_returns_the_enabled_row(monkeypatch):
    record = _Record()
    chain, _, _ = _chain_under_test(monkeypatch, record)
    session = _Session(record)
    assert await chain.load_enabled_handler(session, record.id) is record
    assert session.asked_for == (_HandlerModel, record.id)


@pytest.mark.parametrize("stored", [_Record(enabled=False), None])
async def test_load_enabled_handler_hides_a_disabled_or_missing_row(
    monkeypatch, stored
):
    chain, _, _ = _chain_under_test(monkeypatch, stored)
    assert await chain.load_enabled_handler(_Session(stored), uuid4()) is None


def test_the_chain_keeps_its_model_to_itself():
    chain = RecurringFireChain(
        handler_model=_HandlerModel, build_fire_payload=lambda *_: {}
    )
    assert not hasattr(chain, "handler_model")


# -- arm_occurrence: enqueue one fire and stamp it, first or next -------------


async def test_arm_occurrence_enqueues_the_rows_trigger_and_stamps_it(monkeypatch):
    chain, submits, _ = _chain_under_test(monkeypatch, None)
    record = _Record()

    job_id = await chain.arm_occurrence(record, FIRST_FIRE)

    assert submits == [
        (
            {
                "handler_id": str(record.id),
                "trigger_context": {"trigger_type": "schedule"},
            },
            FIRST_FIRE,
            job_id,
        )
    ]
    assert record.scheduled_job_id == job_id


async def test_arm_occurrence_names_a_one_shot_timers_trigger(monkeypatch):
    # An install of a delay-triggered handler arms its only fire the same way;
    # the fire's context says "timer" so the fire job never re-arms it.
    chain, submits, _ = _chain_under_test(monkeypatch, None)
    record = _Record(trigger_type="timer")
    await chain.arm_occurrence(record, FIRST_FIRE)
    assert submits[0][0]["trigger_context"] == {"trigger_type": "timer"}


async def test_every_armed_occurrence_gets_its_own_job_id(monkeypatch):
    chain, submits, _ = _chain_under_test(monkeypatch, None)
    await chain.arm_occurrence(_Record(), FIRST_FIRE)
    await chain.arm_occurrence(_Record(), FIRST_FIRE)
    assert submits[0][2] != submits[1][2]


# -- one chain per tier, looked up by the audit row's handler_kind -------------


async def _seed(engine, model, **fields):
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        record = model(**fields)
        session.add(record)
        await session.commit()
        return record


def _channel_handler_fields(**overrides) -> dict:
    fields = {
        "id": uuid4(),
        "guild_id": "G1",
        "channel_id": "C1",
        "name": "six-hourly",
        "trigger_type": "schedule",
        "settings": RECURRING_SETTINGS,
        "description": "d",
        "script": "pass\n",
        "created_by": "U1",
        "created_at": CREATED,
    }
    return {**fields, **overrides}


def _admin_handler_fields(**overrides) -> dict:
    fields = {
        "id": uuid4(),
        "guild_id": "G1",
        "name": "six-hourly-admin",
        "trigger_type": "schedule",
        "settings": RECURRING_SETTINGS,
        "channel_ids": ["MODCHAT"],
        "description": "d",
        "script": "pass\n",
        "created_by_admin": "A1",
        "created_at": CREATED,
    }
    return {**fields, **overrides}


def test_there_is_exactly_one_chain_per_tier():
    assert set(RECURRING_CHAINS) == {"standard", "admin"}


async def test_each_tier_chain_loads_only_its_own_rows(test_engine):
    channel_row = await _seed(test_engine, ChannelHandler, **_channel_handler_fields())
    admin_row = await _seed(test_engine, AdminHandler, **_admin_handler_fields())

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        standard = RECURRING_CHAINS["standard"]
        admin = RECURRING_CHAINS["admin"]
        assert isinstance(
            await standard.load_enabled_handler(session, channel_row.id), ChannelHandler
        )
        assert isinstance(
            await admin.load_enabled_handler(session, admin_row.id), AdminHandler
        )
        assert await standard.load_enabled_handler(session, admin_row.id) is None
        assert await admin.load_enabled_handler(session, channel_row.id) is None


async def test_each_tier_chain_lists_only_its_enabled_schedules(test_engine):
    scheduled = await _seed(test_engine, ChannelHandler, **_channel_handler_fields())
    await _seed(
        test_engine, ChannelHandler, **_channel_handler_fields(name="off", enabled=False)
    )
    await _seed(
        test_engine,
        ChannelHandler,
        **_channel_handler_fields(name="on-message", trigger_type="message", settings={}),
    )
    admin_scheduled = await _seed(test_engine, AdminHandler, **_admin_handler_fields())
    await _seed(
        test_engine,
        AdminHandler,
        **_admin_handler_fields(name="on-join", trigger_type="member_join", settings={}),
    )

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        standard_rows = await RECURRING_CHAINS["standard"].load_enabled_schedule_handlers(
            session
        )
        admin_rows = await RECURRING_CHAINS["admin"].load_enabled_schedule_handlers(
            session
        )
    assert [row.id for row in standard_rows] == [scheduled.id]
    assert [row.id for row in admin_rows] == [admin_scheduled.id]


async def test_the_standard_chain_arms_a_channel_handler_payload(monkeypatch, test_engine):
    submits = _capture_submits(monkeypatch)
    record = await _seed(test_engine, ChannelHandler, **_channel_handler_fields())

    job_id = await RECURRING_CHAINS["standard"].arm_occurrence(record, FIRST_FIRE)

    payload, scheduled_for, submitted_job_id = submits[0]
    assert isinstance(payload, HandlerFirePayload)
    assert payload.handler_id == str(record.id)
    assert payload.trigger_context == {"trigger_type": "schedule"}
    assert payload.chain_depth == 0
    assert scheduled_for == FIRST_FIRE
    assert submitted_job_id == job_id == record.scheduled_job_id


async def test_the_admin_chain_arms_an_admin_handler_payload(monkeypatch, test_engine):
    submits = _capture_submits(monkeypatch)
    record = await _seed(
        test_engine, AdminHandler, **_admin_handler_fields(trigger_type="timer")
    )

    await RECURRING_CHAINS["admin"].arm_occurrence(record, FIRST_FIRE)

    payload, _, _ = submits[0]
    assert isinstance(payload, AdminHandlerFirePayload)
    assert payload.admin_handler_id == str(record.id)
    # The fire job resolves the channel from the row at fire time, so the
    # payload never pins one — an edit between arming and firing is honoured.
    assert payload.channel_id == ""
    assert payload.trigger_context == {"trigger_type": "timer"}
    assert payload.chain_depth == 0

