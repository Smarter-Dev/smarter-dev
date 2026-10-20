"""The host services a fire lends to its sandboxed script.

The timer scheduler is tier-agnostic: it knows how to descend one generation
and enqueue, and the tier tells it how to build its payload. (The warn's row
and its name lookup moved to the moderation gateway; see admin_actions_test.)
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import smarter_dev.web.handler_script_services as handler_script_services
from smarter_dev.web.handler_script_services import HandlerTimerScheduler

FIRE_AT = datetime.now(UTC) + timedelta(minutes=5)
REFIRE_CONTEXT = {"trigger_type": "timer", "payload": {"user_id": "U1"}}


def _capture_submits(monkeypatch) -> list:
    submits: list = []

    async def fake_submit(payload, scheduled_for=None, job_id=None):
        submits.append((payload, scheduled_for, job_id))

    monkeypatch.setattr(handler_script_services, "worker_submit", fake_submit)
    return submits


class _TierChain:
    """The tier chain the scheduler asks for a payload — the real chains' shape."""

    def __init__(self) -> None:
        self.asked: list = []

    def build_fire_payload(
        self,
        handler_id: str,
        trigger_context: dict,
        *,
        chain_depth: int = 0,
        channel_id: str = "",
    ) -> dict:
        self.asked.append((handler_id, trigger_context, chain_depth, channel_id))
        return {"handler_id": handler_id, "depth": chain_depth, "channel": channel_id}


def _scheduler(chain: _TierChain, chain_depth: int = 0) -> HandlerTimerScheduler:
    return HandlerTimerScheduler(
        chain=chain, handler_id="h1", channel_id="C1", chain_depth=chain_depth
    )


async def test_a_timer_refire_descends_one_generation(monkeypatch):
    submits = _capture_submits(monkeypatch)
    chain = _TierChain()

    await _scheduler(chain, chain_depth=2).schedule_timer(FIRE_AT, REFIRE_CONTEXT)

    assert chain.asked == [("h1", REFIRE_CONTEXT, 3, "C1")]
    payload, scheduled_for, job_id = submits[0]
    assert payload == {"handler_id": "h1", "depth": 3, "channel": "C1"}
    assert scheduled_for == FIRE_AT
    assert job_id


async def test_the_tier_chain_is_the_only_thing_that_names_the_payload(monkeypatch):
    """The scheduler enqueues whatever its tier's chain built, and nothing else."""
    _capture_submits(monkeypatch)
    chain = _TierChain()

    await _scheduler(chain).schedule_timer(FIRE_AT, REFIRE_CONTEXT)

    assert chain.asked == [("h1", REFIRE_CONTEXT, 1, "C1")]


async def test_every_armed_timer_gets_its_own_job_id(monkeypatch):
    submits = _capture_submits(monkeypatch)
    scheduler = _scheduler(_TierChain())
    await scheduler.schedule_timer(FIRE_AT, REFIRE_CONTEXT)
    await scheduler.schedule_timer(FIRE_AT, REFIRE_CONTEXT)
    assert submits[0][2] != submits[1][2]
