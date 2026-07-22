"""Regression tests: the fire jobs wire the fire's guild into the DiscordEmitter.

Without a guild id the emitter's ``list_threads()`` hits
``GET /guilds//threads/active`` — a malformed URL Discord 404s — so the
gone-channel policy would swallow it and every deployed ``list_threads()`` would
silently return ``[]``. These tests pin that both the standard and admin fire
jobs construct their emitter with the handler's guild id.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import smarter_dev.web.admin_handlers_jobs as admin_handlers_jobs
import smarter_dev.web.handler_agent as handler_agent
import smarter_dev.web.handlers_jobs as handlers_jobs
import smarter_dev.web.handler_runtime as handler_runtime
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.handler_runtime import HandlerResult
from smarter_dev.web.handlers_jobs import HandlerFirePayload

_USAGE = {
    "messages_sent": 0,
    "web_searches": 0,
    "web_reads": 0,
    "agent_calls": 0,
    "mod_actions": 0,
    "discord_reads": 0,
    "thread_ops": 0,
    "role_changes": 0,
    "timers_scheduled": 0,
}


class _FakeSession:
    async def get(self, model, id_):
        return _FakeSession.record

    def add(self, obj):
        pass

    async def scalars(self, statement):
        return []  # empty guild-memory snapshot for the emitter-guild regression

    async def commit(self):
        pass


class _FakeSessionCtx:
    def __init__(self, record):
        _FakeSession.record = record

    async def __aenter__(self):
        return _FakeSession()

    async def __aexit__(self, *exc):
        return False


def _ok_result():
    return HandlerResult(outcome="ok", usage=dict(_USAGE), duration_ms=1)


@pytest.fixture
def capture_emitter(monkeypatch):
    captured = {}

    async def fake_run(script, context, **kwargs):
        captured["emitter"] = kwargs["emitter"]
        return _ok_result()

    async def fake_agent(*args, **kwargs):
        return ""

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    return captured


async def test_standard_fire_sets_emitter_guild_id(monkeypatch, capture_emitter):
    record = SimpleNamespace(
        enabled=True,
        script="pass",
        channel_id="C1",
        guild_id="G99",
        trigger_type="message",
        settings={},
        memory={},
    )
    monkeypatch.setattr(
        handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        handlers_jobs, "get_db_session_context", lambda: _FakeSessionCtx(record)
    )
    monkeypatch.setattr(handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(handlers_jobs, "WindowedLimiter", lambda **kwargs: object())

    await handlers_jobs.run_handler_fire(HandlerFirePayload(handler_id=str(uuid4())))
    assert capture_emitter["emitter"].guild_id == "G99"


async def test_admin_fire_sets_emitter_guild_id(monkeypatch, capture_emitter):
    record = SimpleNamespace(
        enabled=True,
        script="pass",
        guild_id="G77",
        trigger_type="message",
        channel_ids=["C1"],
        settings={},
        memory={},
    )
    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_db_session_context",
        lambda: _FakeSessionCtx(record),
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(
        admin_handlers_jobs, "WindowedLimiter", lambda **kwargs: object()
    )

    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(admin_handler_id=str(uuid4()), channel_id="C1")
    )
    assert capture_emitter["emitter"].guild_id == "G77"


# -- guild-shared memory load/persist around an admin fire ---------------------

from sqlalchemy.ext.asyncio import async_sessionmaker

from smarter_dev.web.handler_guild_memory import (
    load_guild_memory,
    persist_guild_memory,
)
from smarter_dev.web.models import AdminHandler, ChannelHandler


class _RealSessionCtx:
    """A get_db_session_context() stand-in that yields real sessions on the
    test engine, so the job's load_guild_memory/persist_guild_memory run real
    SQL against the guild_handler_memory table."""

    def __init__(self, engine):
        self._maker = async_sessionmaker(engine, expire_on_commit=False)

    def __call__(self):
        return self

    async def __aenter__(self):
        self._session = self._maker()
        return self._session

    async def __aexit__(self, *exc):
        await self._session.close()
        return False


def _result_with_guild_memory(writes=None, deletes=None, outcome="ok"):
    return HandlerResult(
        outcome=outcome,
        usage=dict(_USAGE),
        duration_ms=1,
        error="boom" if outcome == "error" else None,
        guild_memory_writes=dict(writes or {}),
        guild_memory_deletes=list(deletes or []),
        guild_memory_changed=bool(writes or deletes),
    )


async def _seed_admin_handler(
    engine,
    guild_id: str = "G1",
    settings: dict | None = None,
    trigger_type: str = "message",
    script: str = "pass\n",
) -> str:
    handler_id = str(uuid4())
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        s.add(
            AdminHandler(
                id=UUID(handler_id),
                guild_id=guild_id,
                name=f"h-{handler_id[:8]}",
                trigger_type=trigger_type,
                settings=settings or {},
                channel_ids=["C1"],
                description="d",
                script=script,
                created_by_admin="A1",
            )
        )
        await s.commit()
    return handler_id


def _patch_admin_job(monkeypatch, engine, fake_result, captured=None):
    async def fake_run(script, context, **kwargs):
        if captured is not None:
            captured["guild_memory"] = kwargs.get("guild_memory")
            captured["allowed_role_ids"] = kwargs.get("allowed_role_ids")
        return fake_result

    async def fake_agent(*args, **kwargs):
        return ""

    async def fake_notify(**kwargs):
        return None

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    monkeypatch.setattr(admin_handlers_jobs, "notify_handler_error", fake_notify)
    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        admin_handlers_jobs, "get_db_session_context", _RealSessionCtx(engine)
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(
        admin_handlers_jobs, "WindowedLimiter", lambda **kwargs: object()
    )


async def _load(engine, guild_id: str) -> dict:
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        return await load_guild_memory(s, guild_id)


async def test_admin_fire_persists_guild_memory_write(monkeypatch, test_engine):
    handler_id = await _seed_admin_handler(test_engine, "G1")
    captured = {}
    _patch_admin_job(
        monkeypatch,
        test_engine,
        _result_with_guild_memory(writes={"relay_bind_target": {"id": "7"}}),
        captured,
    )
    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(admin_handler_id=handler_id, channel_id="C1")
    )
    assert captured["guild_memory"] == {}  # loaded snapshot was empty
    assert await _load(test_engine, "G1") == {"relay_bind_target": {"id": "7"}}


async def test_admin_fire_loads_existing_guild_memory(monkeypatch, test_engine):
    handler_id = await _seed_admin_handler(test_engine, "G1")
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as s:
        await persist_guild_memory(s, "G1", {"seen": 3}, [])
        await s.commit()
    captured = {}
    _patch_admin_job(monkeypatch, test_engine, _result_with_guild_memory(), captured)
    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(admin_handler_id=handler_id, channel_id="C1")
    )
    assert captured["guild_memory"] == {"seen": 3}


async def test_admin_fire_persists_guild_memory_delete(monkeypatch, test_engine):
    handler_id = await _seed_admin_handler(test_engine, "G1")
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as s:
        await persist_guild_memory(s, "G1", {"gone": 1, "keep": 2}, [])
        await s.commit()
    _patch_admin_job(
        monkeypatch, test_engine, _result_with_guild_memory(deletes=["gone"])
    )
    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(admin_handler_id=handler_id, channel_id="C1")
    )
    assert await _load(test_engine, "G1") == {"keep": 2}


async def test_guild_memory_write_survives_a_later_script_error(
    monkeypatch, test_engine
):
    handler_id = await _seed_admin_handler(test_engine, "G1")
    _patch_admin_job(
        monkeypatch,
        test_engine,
        _result_with_guild_memory(writes={"bind": {"id": "9"}}, outcome="error"),
    )
    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(admin_handler_id=handler_id, channel_id="C1")
    )
    # Emitted-effects-stay: the write made before the error persisted.
    assert await _load(test_engine, "G1") == {"bind": {"id": "9"}}


async def test_standard_fire_never_touches_guild_memory(monkeypatch, test_engine):
    # A standard (non-admin) fire has no guild-memory wiring, so even a result
    # that claims guild-memory changes leaves the table untouched.
    handler_id = str(uuid4())
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as s:
        s.add(
            ChannelHandler(
                id=UUID(handler_id),
                guild_id="G1",
                channel_id="C1",
                name="std",
                trigger_type="message",
                settings={},
                description="d",
                script="pass\n",
                created_by="U1",
            )
        )
        await s.commit()

    async def fake_run(script, context, **kwargs):
        assert "guild_memory" not in kwargs  # standard job never passes it
        return _result_with_guild_memory(writes={"x": 1})

    async def fake_agent(*args, **kwargs):
        return ""

    async def fake_notify(**kwargs):
        return None

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    monkeypatch.setattr(handlers_jobs, "notify_handler_error", fake_notify)
    monkeypatch.setattr(
        handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        handlers_jobs, "get_db_session_context", _RealSessionCtx(test_engine)
    )
    monkeypatch.setattr(handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(handlers_jobs, "WindowedLimiter", lambda **kwargs: object())

    await handlers_jobs.run_handler_fire(HandlerFirePayload(handler_id=handler_id))
    assert await _load(test_engine, "G1") == {}


async def test_concurrent_different_key_writes_and_same_key_last_write_wins(
    monkeypatch, test_engine
):
    handler_id = await _seed_admin_handler(test_engine, "G1")

    async def _fire(result):
        _patch_admin_job(monkeypatch, test_engine, result)
        await admin_handlers_jobs.run_admin_handler_fire(
            AdminHandlerFirePayload(admin_handler_id=handler_id, channel_id="C1")
        )

    # Two fires writing DIFFERENT keys — both survive (per-key upsert).
    await _fire(_result_with_guild_memory(writes={"a": 1}))
    await _fire(_result_with_guild_memory(writes={"b": 2}))
    assert await _load(test_engine, "G1") == {"a": 1, "b": 2}

    # Two fires writing the SAME key — last write wins, no error.
    await _fire(_result_with_guild_memory(writes={"a": 10}))
    await _fire(_result_with_guild_memory(writes={"a": 20}))
    assert await _load(test_engine, "G1") == {"a": 20, "b": 2}


# -- role_changes recording + allowed_role_ids passthrough (E2) ----------------

from sqlalchemy import select

from smarter_dev.web.models import HandlerRun


async def _load_runs(engine, handler_id: str) -> list[HandlerRun]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        rows = await s.scalars(
            select(HandlerRun).where(HandlerRun.handler_id == UUID(handler_id))
        )
        return list(rows)


async def test_admin_fire_records_role_changes(monkeypatch, test_engine):
    handler_id = await _seed_admin_handler(test_engine, "G1")
    usage = dict(_USAGE, role_changes=3)
    result = HandlerResult(outcome="ok", usage=usage, duration_ms=1)
    _patch_admin_job(monkeypatch, test_engine, result)
    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(admin_handler_id=handler_id, channel_id="C1")
    )
    runs = await _load_runs(test_engine, handler_id)
    assert len(runs) == 1
    assert runs[0].role_changes == 3


async def test_admin_fire_passes_allowed_role_ids_from_settings(
    monkeypatch, test_engine
):
    handler_id = await _seed_admin_handler(
        test_engine, "G1", settings={"allowed_role_ids": ["R1", "R2"]}
    )
    captured = {}
    _patch_admin_job(
        monkeypatch, test_engine, _result_with_guild_memory(), captured
    )
    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(admin_handler_id=handler_id, channel_id="C1")
    )
    assert captured["allowed_role_ids"] == ["R1", "R2"]


async def test_standard_fire_records_zero_role_changes(monkeypatch, test_engine):
    handler_id = str(uuid4())
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as s:
        s.add(
            ChannelHandler(
                id=UUID(handler_id),
                guild_id="G1",
                channel_id="C1",
                name="std-rc",
                trigger_type="message",
                settings={},
                description="d",
                script="pass\n",
                created_by="U1",
            )
        )
        await s.commit()

    async def fake_run(script, context, **kwargs):
        return HandlerResult(outcome="ok", usage=dict(_USAGE), duration_ms=1)

    async def fake_agent(*args, **kwargs):
        return ""

    async def fake_notify(**kwargs):
        return None

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    monkeypatch.setattr(handlers_jobs, "notify_handler_error", fake_notify)
    monkeypatch.setattr(
        handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        handlers_jobs, "get_db_session_context", _RealSessionCtx(test_engine)
    )
    monkeypatch.setattr(handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(handlers_jobs, "WindowedLimiter", lambda **kwargs: object())

    await handlers_jobs.run_handler_fire(HandlerFirePayload(handler_id=handler_id))
    runs = await _load_runs(test_engine, handler_id)
    assert len(runs) == 1
    assert runs[0].role_changes == 0


# -- schedule_timer wiring (persisted one-shot self re-arm, E3) ----------------

from datetime import datetime, timedelta, timezone

from smarter_dev.web.handler_caps import TIMER_ARMING_WINDOW_SECONDS


def _patch_std_job(monkeypatch, record, *, fake_run, submits, limiter_kwargs):
    async def fake_agent(*a, **k):
        return ""

    async def fake_notify(**kwargs):
        return None

    async def fake_submit(payload, scheduled_for=None, job_id=None):
        submits.append((payload, scheduled_for, job_id))

    def fake_limiter(**kwargs):
        limiter_kwargs.append(kwargs)
        return object()

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    monkeypatch.setattr(handlers_jobs, "notify_handler_error", fake_notify)
    monkeypatch.setattr(handlers_jobs, "worker_submit", fake_submit)
    monkeypatch.setattr(
        handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        handlers_jobs, "get_db_session_context", lambda: _FakeSessionCtx(record)
    )
    monkeypatch.setattr(handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(handlers_jobs, "WindowedLimiter", fake_limiter)


async def test_standard_fire_arms_timer_submits_fire_payload(monkeypatch):
    record = SimpleNamespace(
        enabled=True, script="pass", channel_id="C1", guild_id="G1",
        trigger_type="message", settings={}, memory={},
    )
    captured, submits, limiter_kwargs = {}, [], []

    async def fake_run(script, context, **kwargs):
        captured.update(kwargs)
        return _ok_result()

    _patch_std_job(
        monkeypatch, record, fake_run=fake_run, submits=submits,
        limiter_kwargs=limiter_kwargs,
    )
    hid = str(uuid4())
    await handlers_jobs.run_handler_fire(HandlerFirePayload(handler_id=hid))

    # A dedicated 3600s timer-arming limiter was constructed and injected.
    assert any(
        k.get("window_seconds") == TIMER_ARMING_WINDOW_SECONDS for k in limiter_kwargs
    )
    assert captured["handler_id"] == hid
    # Invoke the injected closure: it enqueues a HandlerFirePayload of THIS
    # handler with the refire context and scheduled_for.
    fire_at = datetime.now(timezone.utc) + timedelta(seconds=120)
    refire = {"trigger_type": "timer", "payload": {"user_id": "U1"},
              "scheduled_at": "2026-07-18T00:00:00+00:00"}
    await captured["timer_scheduler"](fire_at, refire)
    assert len(submits) == 1
    payload, scheduled_for, job_id = submits[0]
    assert isinstance(payload, HandlerFirePayload)
    assert payload.handler_id == hid
    assert payload.trigger_context == refire
    assert scheduled_for == fire_at
    assert job_id  # a job id was minted for the durable enqueue


async def test_admin_fire_arms_timer_submits_admin_payload(monkeypatch):
    record = SimpleNamespace(
        enabled=True, script="pass", guild_id="G1", trigger_type="message",
        channel_ids=["C1"], settings={}, memory={},
    )
    captured, submits, limiter_kwargs = {}, [], []

    async def fake_run(script, context, **kwargs):
        captured.update(kwargs)
        return _ok_result()

    async def fake_agent(*a, **k):
        return ""

    async def fake_notify(**kwargs):
        return None

    async def fake_submit(payload, scheduled_for=None, job_id=None):
        submits.append((payload, scheduled_for, job_id))

    def fake_limiter(**kwargs):
        limiter_kwargs.append(kwargs)
        return object()

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    monkeypatch.setattr(admin_handlers_jobs, "notify_handler_error", fake_notify)
    monkeypatch.setattr(admin_handlers_jobs, "worker_submit", fake_submit)
    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )

    async def fake_load_guild_memory(session, guild_id):
        return {}

    monkeypatch.setattr(
        admin_handlers_jobs, "load_guild_memory", fake_load_guild_memory
    )
    monkeypatch.setattr(
        admin_handlers_jobs, "get_db_session_context", lambda: _FakeSessionCtx(record)
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(admin_handlers_jobs, "WindowedLimiter", fake_limiter)

    hid = str(uuid4())
    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(admin_handler_id=hid, channel_id="C1")
    )
    assert any(
        k.get("window_seconds") == TIMER_ARMING_WINDOW_SECONDS for k in limiter_kwargs
    )
    fire_at = datetime.now(timezone.utc) + timedelta(seconds=300)
    refire = {"trigger_type": "timer", "payload": {"user_id": "U9"},
              "scheduled_at": "2026-07-18T00:00:00+00:00"}
    await captured["timer_scheduler"](fire_at, refire)
    assert len(submits) == 1
    payload, scheduled_for, job_id = submits[0]
    assert isinstance(payload, AdminHandlerFirePayload)
    assert payload.admin_handler_id == hid
    assert payload.trigger_context == refire
    assert scheduled_for == fire_at


async def test_timer_refire_runs_same_handler_with_payload_context(monkeypatch):
    record = SimpleNamespace(
        enabled=True, script="pass", channel_id="C1", guild_id="G1",
        trigger_type="message", settings={}, memory={},
    )
    captured, submits, limiter_kwargs = {}, [], []

    async def fake_run(script, context, **kwargs):
        captured["context"] = context
        return _ok_result()

    _patch_std_job(
        monkeypatch, record, fake_run=fake_run, submits=submits,
        limiter_kwargs=limiter_kwargs,
    )
    timer_ctx = {"trigger_type": "timer", "payload": {"user_id": "U1"},
                 "scheduled_at": "2026-07-18T00:00:00+00:00"}
    await handlers_jobs.run_handler_fire(
        HandlerFirePayload(handler_id=str(uuid4()), trigger_context=timer_ctx)
    )
    # The fire job runs the script with the timer context verbatim — nothing
    # checks it against the row's trigger_type ("message" here).
    assert captured["context"] == timer_ctx


async def test_timer_trigger_row_does_not_reschedule(monkeypatch):
    # A row whose trigger_type is "timer" is one-shot: only "schedule" rows
    # enqueue a next occurrence. The mocked run never invokes the scheduler, so
    # no worker_submit should happen at all.
    record = SimpleNamespace(
        enabled=True, script="pass", channel_id="C1", guild_id="G1",
        trigger_type="timer", settings={"delay_seconds": 120}, memory={},
    )
    submits, limiter_kwargs = [], []

    async def fake_run(script, context, **kwargs):
        return _ok_result()

    _patch_std_job(
        monkeypatch, record, fake_run=fake_run, submits=submits,
        limiter_kwargs=limiter_kwargs,
    )
    await handlers_jobs.run_handler_fire(
        HandlerFirePayload(handler_id=str(uuid4()), trigger_context={"trigger_type": "timer"})
    )
    assert submits == []


async def test_schedule_row_scheduled_fire_reschedules(monkeypatch):
    # A genuine scheduled fire of a "schedule" row enqueues the next occurrence.
    record = SimpleNamespace(
        enabled=True, script="pass", channel_id="C1", guild_id="G1",
        trigger_type="schedule",
        settings={
            "interval_seconds": 300,
            "start_at": "2099-01-01T00:00:00Z",
        },
        memory={},
    )
    submits, limiter_kwargs = [], []

    async def fake_run(script, context, **kwargs):
        return _ok_result()

    _patch_std_job(
        monkeypatch, record, fake_run=fake_run, submits=submits,
        limiter_kwargs=limiter_kwargs,
    )
    await handlers_jobs.run_handler_fire(
        HandlerFirePayload(
            handler_id=str(uuid4()), trigger_context={"trigger_type": "schedule"}
        )
    )
    assert len(submits) == 1
    payload, scheduled_for, _ = submits[0]
    assert payload.trigger_context == {"trigger_type": "schedule"}
    assert scheduled_for.isoformat() == "2099-01-01T00:00:00+00:00"


async def test_schedule_row_timer_refire_does_not_reschedule(monkeypatch):
    # A "schedule" row that self-arms a schedule_timer re-fires with a "timer"
    # context; that re-fire must NOT re-enter _reschedule (which would fork a
    # duplicate perpetual chain and clobber scheduled_job_id). The mocked run
    # arms nothing, so ANY submit here would be a spurious reschedule.
    record = SimpleNamespace(
        enabled=True, script="pass", channel_id="C1", guild_id="G1",
        trigger_type="schedule", settings={"interval_seconds": 300}, memory={},
    )
    submits, limiter_kwargs = [], []

    async def fake_run(script, context, **kwargs):
        return _ok_result()

    _patch_std_job(
        monkeypatch, record, fake_run=fake_run, submits=submits,
        limiter_kwargs=limiter_kwargs,
    )
    await handlers_jobs.run_handler_fire(
        HandlerFirePayload(
            handler_id=str(uuid4()),
            trigger_context={"trigger_type": "timer", "payload": {}},
        )
    )
    assert submits == []


def _patch_admin_reschedule_job(monkeypatch, record, submits):
    async def fake_run(script, context, **kwargs):
        return _ok_result()

    async def fake_agent(*a, **k):
        return ""

    async def fake_notify(**kwargs):
        return None

    async def fake_submit(payload, scheduled_for=None, job_id=None):
        submits.append((payload, scheduled_for, job_id))

    async def fake_load_guild_memory(session, guild_id):
        return {}

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    monkeypatch.setattr(admin_handlers_jobs, "notify_handler_error", fake_notify)
    monkeypatch.setattr(admin_handlers_jobs, "worker_submit", fake_submit)
    monkeypatch.setattr(
        admin_handlers_jobs, "load_guild_memory", fake_load_guild_memory
    )
    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        admin_handlers_jobs, "get_db_session_context", lambda: _FakeSessionCtx(record)
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(
        admin_handlers_jobs, "WindowedLimiter", lambda **kwargs: object()
    )


async def test_admin_schedule_row_scheduled_fire_reschedules(monkeypatch):
    record = SimpleNamespace(
        enabled=True, script="pass", guild_id="G1", trigger_type="schedule",
        channel_ids=["C1"],
        settings={
            "interval_seconds": 300,
            "start_at": "2099-01-01T00:00:00Z",
        },
        memory={},
    )
    submits: list = []
    _patch_admin_reschedule_job(monkeypatch, record, submits)
    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=str(uuid4()), channel_id="C1",
            trigger_context={"trigger_type": "schedule"},
        )
    )
    assert len(submits) == 1
    payload, scheduled_for, _ = submits[0]
    assert payload.trigger_context == {"trigger_type": "schedule"}
    assert scheduled_for.isoformat() == "2099-01-01T00:00:00+00:00"


async def test_admin_schedule_row_timer_refire_does_not_reschedule(monkeypatch):
    # Same fork guard on the admin fire job: a timer re-fire of a schedule admin
    # handler must not enqueue a second next-occurrence chain.
    record = SimpleNamespace(
        enabled=True, script="pass", guild_id="G1", trigger_type="schedule",
        channel_ids=["C1"], settings={"interval_seconds": 300}, memory={},
    )
    submits: list = []
    _patch_admin_reschedule_job(monkeypatch, record, submits)
    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=str(uuid4()), channel_id="C1",
            trigger_context={"trigger_type": "timer", "payload": {}},
        )
    )
    assert submits == []


async def test_refire_of_deleted_handler_returns_missing_and_emits_nothing(monkeypatch):
    ran = {}

    async def fake_run(script, context, **kwargs):
        ran["yes"] = True
        return _ok_result()

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(
        handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    # A deleted handler: the row is gone.
    monkeypatch.setattr(
        handlers_jobs, "get_db_session_context", lambda: _FakeSessionCtx(None)
    )
    result = await handlers_jobs.run_handler_fire(
        HandlerFirePayload(
            handler_id=str(uuid4()),
            trigger_context={"trigger_type": "timer", "payload": {}},
        )
    )
    assert result == {"status": "missing"}
    assert "yes" not in ran  # nothing ran, nothing emitted


async def test_disabled_handler_timer_refire_noops(monkeypatch):
    ran = {}

    async def fake_run(script, context, **kwargs):
        ran["yes"] = True
        return _ok_result()

    record = SimpleNamespace(
        enabled=False, script="pass", channel_id="C1", guild_id="G1",
        trigger_type="message", settings={}, memory={},
    )
    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(
        handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        handlers_jobs, "get_db_session_context", lambda: _FakeSessionCtx(record)
    )
    result = await handlers_jobs.run_handler_fire(
        HandlerFirePayload(
            handler_id=str(uuid4()),
            trigger_context={"trigger_type": "timer", "payload": {}},
        )
    )
    assert result == {"status": "missing"}
    assert "yes" not in ran


async def test_handler_run_records_timers_scheduled(monkeypatch, test_engine):
    handler_id = str(uuid4())
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as s:
        s.add(
            ChannelHandler(
                id=UUID(handler_id),
                guild_id="G1",
                channel_id="C1",
                name="timer-std",
                trigger_type="message",
                settings={},
                description="d",
                script="pass\n",
                created_by="U1",
            )
        )
        await s.commit()

    async def fake_run(script, context, **kwargs):
        return HandlerResult(
            outcome="ok", usage=dict(_USAGE, timers_scheduled=1), duration_ms=1
        )

    async def fake_agent(*a, **k):
        return ""

    async def fake_notify(**kwargs):
        return None

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    monkeypatch.setattr(handlers_jobs, "notify_handler_error", fake_notify)
    monkeypatch.setattr(
        handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        handlers_jobs, "get_db_session_context", _RealSessionCtx(test_engine)
    )
    monkeypatch.setattr(handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(handlers_jobs, "WindowedLimiter", lambda **kwargs: object())

    await handlers_jobs.run_handler_fire(HandlerFirePayload(handler_id=handler_id))
    runs = await _load_runs(test_engine, handler_id)
    assert len(runs) == 1
    assert runs[0].timers_scheduled == 1


async def test_dm_message_admin_fire_skips_error_notice(monkeypatch):
    # A dm_message fire has NO home channel (channel_id="") so a broken handler's
    # error notice is never posted — critically, never into the user's DM. The
    # fire's channel_id must be "" and NOT the dm_channel_id.
    record = SimpleNamespace(
        enabled=True,
        script="pass",
        guild_id="G5",
        trigger_type="dm_message",
        channel_ids=[],
        settings={},
        memory={},
    )
    notify_channel_ids: list = []

    async def fake_run(script, context, **kwargs):
        return HandlerResult(
            outcome="error", usage=dict(_USAGE), duration_ms=1, error="boom"
        )

    async def fake_agent(*args, **kwargs):
        return ""

    async def fake_notify(**kwargs):
        notify_channel_ids.append(kwargs["channel_id"])
        return False

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    monkeypatch.setattr(admin_handlers_jobs, "notify_handler_error", fake_notify)
    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_db_session_context",
        lambda: _FakeSessionCtx(record),
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(
        admin_handlers_jobs, "WindowedLimiter", lambda **kwargs: object()
    )

    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=str(uuid4()),
            channel_id="",
            trigger_context={
                "trigger_type": "dm_message",
                "author_id": "U9",
                "dm_channel_id": "DM123",
            },
        )
    )
    # notify_handler_error was called with the EMPTY channel (which short-circuits
    # to a no-op), never with the DM channel id.
    assert notify_channel_ids == [""]


# -- mod_action reader injection + lookups audit + loop rail --------------------

from datetime import datetime, timezone

from smarter_dev.web.models import HandlerRun, ModerationAction


async def _handler_run_for(engine, handler_id: str) -> HandlerRun:
    from sqlalchemy import select

    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        rows = (
            await s.execute(
                select(HandlerRun).where(HandlerRun.handler_id == UUID(handler_id))
            )
        ).scalars().all()
    assert len(rows) == 1
    return rows[0]


async def test_admin_fire_wires_mod_action_reader_and_records_lookups(
    monkeypatch, test_engine
):
    handler_id = await _seed_admin_handler(test_engine, "G1")
    # A committed action the injected reader must surface for this guild+user.
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as s:
        s.add(
            ModerationAction(
                guild_id="G1",
                target_user_id="U1",
                target_username="bob",
                action_type="ban",
                source="manual",
                channel_id="C9",
                trigger_message_id="M9",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        await s.commit()

    captured = {}

    async def fake_run(script, context, **kwargs):
        captured["reader"] = kwargs.get("mod_action_reader")
        usage = dict(_USAGE)
        usage["lookups"] = 2
        return HandlerResult(outcome="ok", usage=usage, duration_ms=1)

    _patch_admin_job(monkeypatch, test_engine, _ok_result())
    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)

    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(admin_handler_id=handler_id, channel_id="C1")
    )

    # The reader is DB-backed and binds the fire's guild host-side.
    rows = await captured["reader"]("U1", 10)
    assert len(rows) == 1
    assert rows[0]["action_type"] == "ban"
    assert rows[0]["channel_id"] == "C9"
    assert rows[0]["trigger_message_id"] == "M9"
    # A different guild's action is never visible through this fire's reader.
    assert await captured["reader"]("U9", 10) == []

    # usage['lookups'] lands on the durable run record.
    run = await _handler_run_for(test_engine, handler_id)
    assert run.lookups == 2


class _NullActor:
    def __init__(self, *args, **kwargs):
        pass

    async def timeout_user(self, *args, **kwargs):
        raise AssertionError("timeout_user must never reach the actor (budget=0)")


async def test_mod_action_fire_forces_zero_mod_action_budget(monkeypatch, test_engine):
    # A mod_action-triggered handler whose script tries to timeout a user must
    # breach CapExceeded('mod_actions') immediately — the loop rail forces its
    # mod-action budget to 0 (§3.5).
    handler_id = await _seed_admin_handler(
        test_engine,
        "G1",
        trigger_type="mod_action",
        script="await timeout_user('U1')\n",
    )

    async def fake_agent(*args, **kwargs):
        return ""

    async def fake_notify(**kwargs):
        return None

    import smarter_dev.web.admin_actions as admin_actions

    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    monkeypatch.setattr(admin_handlers_jobs, "notify_handler_error", fake_notify)
    monkeypatch.setattr(admin_actions, "AdminActor", _NullActor)
    monkeypatch.setattr(
        admin_handlers_jobs, "DiscordEmitter", lambda **kwargs: object()
    )
    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        admin_handlers_jobs, "get_db_session_context", _RealSessionCtx(test_engine)
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: object())
    monkeypatch.setattr(
        admin_handlers_jobs, "WindowedLimiter", lambda **kwargs: object()
    )

    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=handler_id,
            channel_id="C1",
            trigger_context={"trigger_type": "mod_action", "action_type": "ban"},
        )
    )

    run = await _handler_run_for(test_engine, handler_id)
    assert run.outcome == "cap_exceeded"
    assert run.cap == "mod_actions"
