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
    engine, guild_id: str = "G1", settings: dict | None = None
) -> str:
    handler_id = str(uuid4())
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        s.add(
            AdminHandler(
                id=UUID(handler_id),
                guild_id=guild_id,
                name=f"h-{handler_id[:8]}",
                trigger_type="message",
                settings=settings or {},
                channel_ids=["C1"],
                description="d",
                script="pass\n",
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
