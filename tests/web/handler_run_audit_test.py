"""The owner of every durable row a handler fire leaves behind.

Both fire jobs — the member one and the admin one — write the same two rows: a
completed fire with its outcome and spend, and a retry that declined to re-run
an already-started script. One module owns both, and it redacts the context it
is handed rather than trusting its caller to have done it, so no call site can
store what a member actually said.
"""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import smarter_dev.web.handler_run_audit as handler_run_audit
from smarter_dev.shared.message_content import MESSAGE_CONTENT_PLACEHOLDER
from smarter_dev.web.handler_run_audit import record_completed_run
from smarter_dev.web.handler_run_audit import record_skipped_run
from smarter_dev.web.models import HandlerRun

VERBATIM_MESSAGE_CONTEXT = {
    "trigger_type": "message",
    "message_id": "M1",
    "author_id": "U1",
    "content": "the plaintext nobody may keep",
    "attachments": ["https://cdn.discordapp.com/a.png"],
}


class _SessionCtx:
    """``get_db_session_context()`` over the test engine."""

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


async def _record(engine, monkeypatch, handler_kind: str, context: dict) -> HandlerRun:
    handler_id = uuid4()
    monkeypatch.setattr(handler_run_audit, "get_db_session_context", _SessionCtx(engine))
    await record_skipped_run(handler_id, handler_kind, context)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        rows = list(
            await session.scalars(
                select(HandlerRun).where(HandlerRun.handler_id == UUID(str(handler_id)))
            )
        )
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize("handler_kind", ["standard", "admin"])
async def test_the_skipped_row_names_its_tier_and_why_it_skipped(
    monkeypatch, test_engine, handler_kind
):
    run = await _record(
        test_engine, monkeypatch, handler_kind, {"trigger_type": "schedule"}
    )
    assert run.handler_kind == handler_kind
    assert run.outcome == "skipped"
    assert "duplicate side effects" in run.error
    assert run.finished_at is not None


@pytest.mark.parametrize("handler_kind", ["standard", "admin"])
async def test_it_redacts_the_verbatim_context_it_is_handed(
    monkeypatch, test_engine, handler_kind
):
    run = await _record(
        test_engine, monkeypatch, handler_kind, dict(VERBATIM_MESSAGE_CONTEXT)
    )
    assert run.trigger_context["content"] == MESSAGE_CONTENT_PLACEHOLDER
    assert run.trigger_context["attachments"] == []
    assert run.trigger_context["message_id"] == "M1"
    assert run.trigger_context["trigger_type"] == "message"


async def test_it_leaves_the_context_the_script_runs_against_alone(
    monkeypatch, test_engine
):
    context = dict(VERBATIM_MESSAGE_CONTEXT)
    await _record(test_engine, monkeypatch, "standard", context)
    assert context == VERBATIM_MESSAGE_CONTEXT


class _Result:
    """The HandlerResult shape the runtime hands back after a fire."""

    def __init__(self, usage: dict, **fields):
        self.usage = usage
        self.outcome = fields.get("outcome", "ok")
        self.cap = fields.get("cap")
        self.error = fields.get("error")
        self.duration_ms = fields.get("duration_ms", 7)


STANDARD_USAGE = {
    "messages_sent": 2,
    "web_searches": 1,
    "web_reads": 1,
    "agent_calls": 1,
    "discord_reads": 1,
    "thread_ops": 0,
    "role_changes": 0,
    "timers_scheduled": 1,
}

ADMIN_USAGE = dict(STANDARD_USAGE, mod_actions=3, lookups=4)


async def _record_completed(engine, handler_kind, context, result) -> HandlerRun:
    handler_id = uuid4()
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await record_completed_run(
            session,
            handler_id=handler_id,
            handler_kind=handler_kind,
            trigger_context=context,
            result=result,
        )
        await session.commit()
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        rows = list(
            await session.scalars(
                select(HandlerRun).where(HandlerRun.handler_id == handler_id)
            )
        )
    assert len(rows) == 1
    return rows[0]


async def test_the_completed_row_records_the_outcome_and_every_counter(test_engine):
    run = await _record_completed(
        test_engine,
        "admin",
        {"trigger_type": "schedule"},
        _Result(ADMIN_USAGE, outcome="cap_exceeded", cap="messages", error="boom"),
    )
    assert run.handler_kind == "admin"
    assert (run.outcome, run.cap, run.error) == ("cap_exceeded", "messages", "boom")
    assert (run.messages_sent, run.web_searches, run.web_reads) == (2, 1, 1)
    assert (run.agent_calls, run.discord_reads, run.timers_scheduled) == (1, 1, 1)
    assert (run.mod_actions, run.lookups) == (3, 4)
    assert run.duration_ms == 7
    assert run.finished_at is not None


async def test_a_standard_fire_names_its_tier_and_zeroes_the_admin_counters(
    test_engine,
):
    run = await _record_completed(
        test_engine, "standard", {"trigger_type": "message"}, _Result(STANDARD_USAGE)
    )
    assert run.handler_kind == "standard"
    assert (run.mod_actions, run.lookups) == (0, 0)


async def test_the_completed_row_redacts_the_verbatim_context_it_is_handed(
    test_engine,
):
    run = await _record_completed(
        test_engine,
        "standard",
        dict(VERBATIM_MESSAGE_CONTEXT),
        _Result(STANDARD_USAGE),
    )
    assert run.trigger_context["content"] == MESSAGE_CONTENT_PLACEHOLDER
    assert run.trigger_context["attachments"] == []
    assert run.trigger_context["message_id"] == "M1"


async def test_the_completed_row_joins_the_caller_transaction(test_engine):
    # The caller owns the session so the row, the memory write and the commit
    # are one transaction: nothing is durable until the caller commits.
    handler_id = uuid4()
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        await record_completed_run(
            session,
            handler_id=handler_id,
            handler_kind="standard",
            trigger_context={"trigger_type": "message"},
            result=_Result(STANDARD_USAGE),
        )
        await session.rollback()
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        rows = list(
            await session.scalars(
                select(HandlerRun).where(HandlerRun.handler_id == handler_id)
            )
        )
    assert rows == []
