"""The recurring chain a scheduled fire keeps alive.

A recurring schedule has no cron daemon: the fire that just ran enqueues the
next occurrence, and the sweep re-arms a chain whose fire never came. Both
paths must enqueue the same tier payload and stamp the same job id, so both go
through one chain per tier.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest

import smarter_dev.web.handler_recurrence as handler_recurrence
from smarter_dev.web.handler_fire_payloads import AdminHandlerFirePayload
from smarter_dev.web.handler_fire_payloads import HandlerFirePayload
from smarter_dev.web.handler_recurrence import RECURRING_CHAINS
from smarter_dev.web.handler_recurrence import RecurringFireChain
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ChannelHandler

RECURRING_SETTINGS = {"interval_seconds": 300, "start_at": "2099-01-01T00:00:00Z"}
NEXT_OCCURRENCE = datetime(2099, 1, 1, tzinfo=UTC)


class _HandlerModel:
    """Stand-in for the tier's ORM model — only its identity is used."""


class _Record:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
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


def _chain_under_test(
    monkeypatch, record
) -> tuple[RecurringFireChain, list, _SessionCtx]:
    submits: list = []

    async def fake_submit(payload, scheduled_for=None, job_id=None):
        submits.append((payload, scheduled_for, job_id))

    session_ctx = _SessionCtx(record)
    monkeypatch.setattr(handler_recurrence, "worker_submit", fake_submit)
    monkeypatch.setattr(handler_recurrence, "get_db_session_context", session_ctx)
    chain = RecurringFireChain(
        handler_model=_HandlerModel,
        build_fire_payload=lambda handler_id: {"handler_id": handler_id},
    )
    return chain, submits, session_ctx


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
    chain, submits, session_ctx = _chain_under_test(monkeypatch, _Record())
    handler_id = uuid4()
    await chain.rearm_after_fire(
        handler_id=handler_id,
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings=RECURRING_SETTINGS,
    )
    payload, scheduled_for, job_id = submits[0]
    assert payload == {"handler_id": str(handler_id)}
    assert scheduled_for == NEXT_OCCURRENCE
    # The enqueued job id is stamped on the tier's row so a later disable or
    # edit can cancel the occurrence this fire just armed.
    assert session_ctx.session.asked_for == (_HandlerModel, handler_id)
    assert session_ctx.session._record.scheduled_job_id == job_id
    assert session_ctx.session.committed is True


async def test_a_one_shot_schedule_ends_the_chain(monkeypatch):
    # Settings with no recurring key (next_fire_at returns None): nothing is
    # enqueued and the row is left alone.
    chain, submits, session_ctx = _chain_under_test(monkeypatch, _Record())
    await chain.rearm_after_fire(
        handler_id=uuid4(),
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings={},
    )
    assert submits == []
    assert session_ctx.session.asked_for is None


async def test_a_handler_disabled_mid_fire_is_not_stamped(monkeypatch):
    chain, submits, session_ctx = _chain_under_test(monkeypatch, _Record(enabled=False))
    await chain.rearm_after_fire(
        handler_id=uuid4(),
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings=RECURRING_SETTINGS,
    )
    assert len(submits) == 1
    assert session_ctx.session._record.scheduled_job_id is None


async def test_a_deleted_handler_is_not_stamped(monkeypatch):
    chain, submits, session_ctx = _chain_under_test(monkeypatch, None)
    await chain.rearm_after_fire(
        handler_id=uuid4(),
        trigger_type="schedule",
        trigger_context={"trigger_type": "schedule"},
        handler_settings=RECURRING_SETTINGS,
    )
    assert len(submits) == 1


# -- arm_next: the enqueue-and-stamp step the sweep shares with the fire -------


async def test_arm_next_enqueues_and_stamps_in_the_caller_session(monkeypatch):
    chain, submits, _ = _chain_under_test(monkeypatch, None)
    record = _Record()
    session = _Session(record)
    handler_id = uuid4()

    job_id = await chain.arm_next(session, handler_id, NEXT_OCCURRENCE)

    assert submits == [({"handler_id": str(handler_id)}, NEXT_OCCURRENCE, job_id)]
    assert record.scheduled_job_id == job_id
    # The caller owns the transaction: nothing is committed here.
    assert session.committed is False


async def test_arm_next_leaves_a_disabled_row_unstamped(monkeypatch):
    chain, submits, _ = _chain_under_test(monkeypatch, None)
    record = _Record(enabled=False)
    await chain.arm_next(_Session(record), uuid4(), NEXT_OCCURRENCE)
    assert len(submits) == 1
    assert record.scheduled_job_id is None


# -- one chain per tier, looked up by the audit row's handler_kind -------------


def test_the_standard_chain_fires_a_channel_handler_payload():
    chain = RECURRING_CHAINS["standard"]
    assert chain.handler_model is ChannelHandler
    payload = chain.build_fire_payload("abc")
    assert isinstance(payload, HandlerFirePayload)
    assert payload.handler_id == "abc"
    assert payload.trigger_context == {"trigger_type": "schedule"}
    assert payload.chain_depth == 0


def test_the_admin_chain_fires_an_admin_handler_payload():
    chain = RECURRING_CHAINS["admin"]
    assert chain.handler_model is AdminHandler
    payload = chain.build_fire_payload("abc")
    assert isinstance(payload, AdminHandlerFirePayload)
    assert payload.admin_handler_id == "abc"
    assert payload.channel_id == ""
    assert payload.trigger_context == {"trigger_type": "schedule"}
    assert payload.chain_depth == 0


def test_there_is_exactly_one_chain_per_tier():
    assert set(RECURRING_CHAINS) == {"standard", "admin"}
