"""The host services a fire lends to its sandboxed script.

The timer scheduler is tier-agnostic: it knows how to descend one generation
and enqueue, and the tier tells it how to build its payload. The admin services
are bound to one fire and never fail a warn over a name lookup.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

import smarter_dev.web.handler_script_services as handler_script_services
from smarter_dev.web.admin_actions import AdminActionError
from smarter_dev.web.handler_script_services import AdminScriptServices
from smarter_dev.web.handler_script_services import HandlerTimerScheduler

FIRE_AT = datetime.now(UTC) + timedelta(minutes=5)
REFIRE_CONTEXT = {"trigger_type": "timer", "payload": {"user_id": "U1"}}


def _capture_submits(monkeypatch) -> list:
    submits: list = []

    async def fake_submit(payload, scheduled_for=None, job_id=None):
        submits.append((payload, scheduled_for, job_id))

    monkeypatch.setattr(handler_script_services, "worker_submit", fake_submit)
    return submits


async def test_a_timer_refire_descends_one_generation(monkeypatch):
    submits = _capture_submits(monkeypatch)
    built: list = []

    def build(refire_context: dict, chain_depth: int) -> dict:
        built.append((refire_context, chain_depth))
        return {"depth": chain_depth}

    scheduler = HandlerTimerScheduler(chain_depth=2, build_refire_payload=build)
    await scheduler.schedule_timer(FIRE_AT, REFIRE_CONTEXT)

    assert built == [(REFIRE_CONTEXT, 3)]
    payload, scheduled_for, job_id = submits[0]
    assert payload == {"depth": 3}
    assert scheduled_for == FIRE_AT
    assert job_id


async def test_every_armed_timer_gets_its_own_job_id(monkeypatch):
    submits = _capture_submits(monkeypatch)
    scheduler = HandlerTimerScheduler(
        chain_depth=0, build_refire_payload=lambda context, depth: context
    )
    await scheduler.schedule_timer(FIRE_AT, REFIRE_CONTEXT)
    await scheduler.schedule_timer(FIRE_AT, REFIRE_CONTEXT)
    assert submits[0][2] != submits[1][2]


class _Actor:
    def __init__(self, error: Exception | None = None, username: str = "zed"):
        self._error = error
        self._username = username

    async def get_member_info(self, user_id: str) -> dict:
        if self._error is not None:
            raise self._error
        return {"username": self._username}


def _services(actor) -> AdminScriptServices:
    return AdminScriptServices(
        handler_id=uuid4(),
        handler_name="h",
        guild_id="G1",
        channel_id="C1",
        chain_depth=0,
        actor=actor,
    )


async def test_username_comes_from_discord_for_the_audit_row():
    assert await _services(_Actor())._resolve_username("U1") == "zed"


async def test_a_discord_lookup_failure_degrades_to_the_raw_id():
    services = _services(_Actor(error=AdminActionError("boom")))
    assert await services._resolve_username("U1") == "U1"


async def test_a_programming_error_in_the_lookup_is_not_swallowed():
    services = _services(_Actor(error=TypeError("bad signature")))
    with pytest.raises(TypeError):
        await services._resolve_username("U1")


def _patch_dispatch(monkeypatch, error: Exception) -> None:
    async def failing_dispatch(session, **kwargs):
        raise error

    monkeypatch.setattr(
        handler_script_services, "dispatch_handler_event", failing_dispatch
    )
    monkeypatch.setattr(
        handler_script_services, "build_mod_action_context", lambda action: {}
    )


@pytest.mark.parametrize(
    "transport_error",
    [RedisError("fire-window limiter down"), SQLAlchemyError("handler lookup down")],
)
async def test_a_mod_action_dispatch_transport_failure_never_breaks_the_warn(
    monkeypatch, transport_error
):
    _patch_dispatch(monkeypatch, transport_error)
    await _services(_Actor())._announce_warn(object(), object())


async def test_a_programming_error_in_the_dispatch_is_not_swallowed(monkeypatch):
    _patch_dispatch(monkeypatch, TypeError("bad keyword"))
    with pytest.raises(TypeError):
        await _services(_Actor())._announce_warn(object(), object())
