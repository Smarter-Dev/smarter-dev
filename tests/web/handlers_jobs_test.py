"""Regression tests: the fire jobs wire the fire's guild into the DiscordEmitter.

Without a guild id the emitter's ``list_threads()`` hits
``GET /guilds//threads/active`` — a malformed URL Discord 404s — so the
gone-channel policy would swallow it and every deployed ``list_threads()`` would
silently return ``[]``. These tests pin that both the standard and admin fire
jobs construct their emitter with the handler's guild id.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

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
}


class _FakeSession:
    async def get(self, model, id_):
        return _FakeSession.record

    def add(self, obj):
        pass

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
