"""The owner of the handler-run audit rows a fire writes outside the fat row.

Both fire jobs — the member one and the admin one — audit a retry that declined
to re-run an already-started script, and both decide the same way whether the
fire owns re-arming a recurring schedule. One module owns both, and it redacts
the context it is handed rather than trusting its caller to have done it, so no
call site can store what a member actually said.
"""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import smarter_dev.web.handler_run_audit as handler_run_audit
from smarter_dev.shared.message_content import MESSAGE_CONTENT_PLACEHOLDER
from smarter_dev.web.handler_run_audit import is_schedule_fire
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


@pytest.mark.parametrize(
    ("trigger_type", "trigger_context", "expected"),
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
def test_only_a_genuine_scheduled_fire_owns_re_arming(
    trigger_type, trigger_context, expected
):
    assert is_schedule_fire(trigger_type, trigger_context) is expected
