"""Parity tests for the native (Litestar) member-activity API (unit U8).

Assert the wire contract of the ported ``routers/activity.py`` directly: the
``POST /api/activity/batch`` happy path (200 + ``{"recorded": <count>}``), that
each event is upserted into ``MemberActivity`` with the same earliest-first /
latest-last semantics the bot relies on, and the 422 the FastAPI mount returned
on a malformed body. The path carries the final ``/api`` prefix and mirrors
exactly what the bot sends from ``smarter_dev/bot/plugins/handler_events.py``.

These run against a real in-memory SQLite session injected into a Litestar app
via ``httpx.ASGITransport`` (mirroring the legacy ``tests/web/test_api/
test_activity.py``) because the handler exercises the real ``record_activity``
upsert rather than a mockable crud class. Auth guards are cleared for the app
build — auth parity is covered separately by ``test_auth.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base
from smarter_dev.web.api_native import activity as activity_module
from smarter_dev.web.api_native.activity import ActivityController
from smarter_dev.web.models import MemberActivity


@pytest.fixture
async def session() -> AsyncIterator:
    """Real in-memory SQLite session with every model table created."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as opened_session:
        yield opened_session
    await engine.dispose()


@pytest.fixture
async def client(session) -> AsyncIterator[AsyncClient]:
    """Litestar app serving the activity controller with guards cleared.

    The routes share the ``activity.BOT_API_GUARDS`` list by reference, so
    emptying it before the app is built removes guards for these tests only.
    """
    original_guards = list(activity_module.BOT_API_GUARDS)
    activity_module.BOT_API_GUARDS.clear()
    try:
        app = Litestar(
            route_handlers=[ActivityController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: session, sync_to_thread=False)
            },
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as http_client:
            yield http_client
    finally:
        activity_module.BOT_API_GUARDS[:] = original_guards


def _iso(value: datetime) -> str:
    return value.isoformat()


async def _activity_rows(session) -> list[MemberActivity]:
    return list((await session.execute(select(MemberActivity))).scalars().all())


async def test_batch_creates_new_rows(client: AsyncClient, session):
    now = datetime.now(timezone.utc)
    response = await client.post(
        "/api/activity/batch",
        json={
            "events": [
                {"guild_id": "G1", "user_id": "U1", "message_at": _iso(now)},
                {"guild_id": "G1", "user_id": "U2", "message_at": _iso(now)},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json() == {"recorded": 2}
    rows = await _activity_rows(session)
    assert len(rows) == 2
    assert all(row.first_message_at is not None for row in rows)


async def test_empty_batch_records_zero(client: AsyncClient, session):
    response = await client.post("/api/activity/batch", json={"events": []})
    assert response.status_code == 200
    assert response.json() == {"recorded": 0}
    assert await _activity_rows(session) == []


async def test_batch_advances_last_but_keeps_first(client: AsyncClient, session):
    early = datetime.now(timezone.utc) - timedelta(days=10)
    late = datetime.now(timezone.utc)
    await client.post(
        "/api/activity/batch",
        json={"events": [{"guild_id": "G1", "user_id": "U1", "message_at": _iso(early)}]},
    )
    await client.post(
        "/api/activity/batch",
        json={"events": [{"guild_id": "G1", "user_id": "U1", "message_at": _iso(late)}]},
    )
    row = (await _activity_rows(session))[0]
    assert row.first_message_at.replace(tzinfo=timezone.utc) <= early + timedelta(seconds=1)
    assert row.last_message_at.replace(tzinfo=timezone.utc) >= late - timedelta(seconds=1)


async def test_malformed_body_is_422(client: AsyncClient):
    # ``message_at`` is required per event; omitting it is a validation error.
    response = await client.post(
        "/api/activity/batch",
        json={"events": [{"guild_id": "G1", "user_id": "U1"}]},
    )
    assert response.status_code == 422
