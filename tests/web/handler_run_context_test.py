"""A handler run's audit row must not keep what the member actually said.

A message-triggered fire hands the script the verbatim trigger context — the
handler is allowed to read the message it is reacting to — but the durable
``handler_runs`` row that outlives the fire stores placeholders for every key
that carries message text. These tests pin both halves for all four rows the
fire jobs write: the standard and admin fire rows, and the two skipped-retry
rows.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import smarter_dev.web.admin_handlers_jobs as admin_handlers_jobs
import smarter_dev.web.handler_agent as handler_agent
import smarter_dev.web.handler_runtime as handler_runtime
import smarter_dev.web.handlers_jobs as handlers_jobs
from smarter_dev.shared.message_content import MESSAGE_CONTENT_PLACEHOLDER
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.handler_runtime import HandlerResult
from smarter_dev.web.handlers_jobs import HandlerFirePayload
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ChannelHandler
from smarter_dev.web.models import HandlerRun

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

MESSAGE_TRIGGER_CONTEXT = {
    "trigger_type": "message",
    "message_id": "M1",
    "author_id": "U1",
    "channel_id": "C1",
    "content": "the plaintext nobody may keep",
    "message_content": "the plaintext nobody may keep",
    "attachments": ["https://cdn.discordapp.com/a.png"],
    "author_role_ids": ["R1", "R2"],
    "is_bot": False,
}

TIMER_REFIRE_CONTEXT = {
    "trigger_type": "timer",
    "message_id": "M1",
    "author_id": "U1",
    "strike_count": 3,
    "escalate": True,
}


class _SessionCtx:
    """``get_db_session_context()`` over the test engine, so the jobs write
    real ``handler_runs`` rows."""

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


def _worker_context() -> SimpleNamespace:
    return SimpleNamespace(job=SimpleNamespace(id=uuid4().hex))


def _patch_job(module, monkeypatch, engine, captured, *, claim_granted=True):
    async def fake_run(script, context, **kwargs):
        captured["context"] = context
        return HandlerResult(outcome="ok", usage=dict(_USAGE), duration_ms=1)

    async def fake_agent(*args, **kwargs):
        return ""

    async def fake_notify(**kwargs):
        return None

    async def fake_claim(_redis, _job_id):
        return claim_granted

    monkeypatch.setattr(handler_runtime, "run_handler_script", fake_run)
    monkeypatch.setattr(handler_agent, "run_gathering_agent", fake_agent)
    monkeypatch.setattr(module, "notify_handler_error", fake_notify)
    monkeypatch.setattr(module, "claim_fire_attempt", fake_claim)
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(module, "get_db_session_context", _SessionCtx(engine))
    monkeypatch.setattr(module, "get_redis_client", lambda: object())
    monkeypatch.setattr(module, "WindowedLimiter", lambda **kwargs: object())


async def _seed_channel_handler(engine, trigger_type: str = "message") -> str:
    handler_id = str(uuid4())
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(
            ChannelHandler(
                id=UUID(handler_id),
                guild_id="G1",
                channel_id="C1",
                name=f"std-{handler_id[:8]}",
                trigger_type=trigger_type,
                settings={},
                description="d",
                script="pass\n",
                created_by="U1",
            )
        )
        await session.commit()
    return handler_id


async def _seed_admin_handler(engine, trigger_type: str = "message") -> str:
    handler_id = str(uuid4())
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(
            AdminHandler(
                id=UUID(handler_id),
                guild_id="G1",
                name=f"adm-{handler_id[:8]}",
                trigger_type=trigger_type,
                settings={},
                channel_ids=["C1"],
                description="d",
                script="pass\n",
                created_by_admin="A1",
            )
        )
        await session.commit()
    return handler_id


async def _stored_run(engine, handler_id: str) -> HandlerRun:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        rows = list(
            await session.scalars(
                select(HandlerRun).where(HandlerRun.handler_id == UUID(handler_id))
            )
        )
    assert len(rows) == 1
    return rows[0]


async def _fire_standard(engine, monkeypatch, context: dict, **patch_kwargs) -> dict:
    handler_id = await _seed_channel_handler(engine)
    captured: dict = {}
    _patch_job(handlers_jobs, monkeypatch, engine, captured, **patch_kwargs)
    await handlers_jobs.run_handler_fire(
        HandlerFirePayload(handler_id=handler_id, trigger_context=context),
        _worker_context(),
    )
    captured["run"] = await _stored_run(engine, handler_id)
    return captured


async def _fire_admin(engine, monkeypatch, context: dict, **patch_kwargs) -> dict:
    handler_id = await _seed_admin_handler(engine)
    captured: dict = {}
    _patch_job(admin_handlers_jobs, monkeypatch, engine, captured, **patch_kwargs)
    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=handler_id, channel_id="C1", trigger_context=context
        ),
        _worker_context(),
    )
    captured["run"] = await _stored_run(engine, handler_id)
    return captured


@pytest.mark.parametrize("fire", [_fire_standard, _fire_admin])
async def test_message_fire_stores_placeholders_for_message_text(
    monkeypatch, test_engine, fire
):
    stored = (await fire(test_engine, monkeypatch, dict(MESSAGE_TRIGGER_CONTEXT)))[
        "run"
    ].trigger_context
    assert stored["content"] == MESSAGE_CONTENT_PLACEHOLDER
    assert stored["message_content"] == MESSAGE_CONTENT_PLACEHOLDER
    assert stored["attachments"] == []


@pytest.mark.parametrize("fire", [_fire_standard, _fire_admin])
async def test_message_fire_keeps_the_context_that_identifies_the_trigger(
    monkeypatch, test_engine, fire
):
    stored = (await fire(test_engine, monkeypatch, dict(MESSAGE_TRIGGER_CONTEXT)))[
        "run"
    ].trigger_context
    assert stored["trigger_type"] == "message"
    assert stored["message_id"] == "M1"
    assert stored["author_id"] == "U1"
    assert stored["channel_id"] == "C1"
    assert stored["author_role_ids"] == ["R1", "R2"]
    assert stored["is_bot"] is False


@pytest.mark.parametrize("fire", [_fire_standard, _fire_admin])
async def test_the_script_still_runs_against_the_verbatim_context(
    monkeypatch, test_engine, fire
):
    context = dict(MESSAGE_TRIGGER_CONTEXT)
    captured = await fire(test_engine, monkeypatch, context)
    assert captured["context"] == MESSAGE_TRIGGER_CONTEXT
    assert context == MESSAGE_TRIGGER_CONTEXT


@pytest.mark.parametrize("fire", [_fire_standard, _fire_admin])
async def test_timer_refire_context_without_message_text_is_stored_unchanged(
    monkeypatch, test_engine, fire
):
    stored = (await fire(test_engine, monkeypatch, dict(TIMER_REFIRE_CONTEXT)))[
        "run"
    ].trigger_context
    assert stored == TIMER_REFIRE_CONTEXT


@pytest.mark.parametrize("fire", [_fire_standard, _fire_admin])
async def test_skipped_retry_row_stores_placeholders_too(
    monkeypatch, test_engine, fire
):
    captured = await fire(
        test_engine, monkeypatch, dict(MESSAGE_TRIGGER_CONTEXT), claim_granted=False
    )
    run = captured["run"]
    assert run.outcome == "skipped"
    assert "context" not in captured
    assert run.trigger_context["content"] == MESSAGE_CONTENT_PLACEHOLDER
    assert run.trigger_context["message_content"] == MESSAGE_CONTENT_PLACEHOLDER
    assert run.trigger_context["attachments"] == []
    assert run.trigger_context["message_id"] == "M1"
