"""Tests for member-activity ingestion and handler-dispatch fact enrichment."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base, get_skrift_db_session
from smarter_dev.web.api.app import api
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.models import ChannelHandler, MemberActivity


class _StubLimiter:
    def __init__(self, redis=None, allow=True):
        self.allow = allow

    async def hit(self, key, limit):
        return self.allow


@pytest.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def client(session, monkeypatch):
    import smarter_dev.web.api.routers.handlers as h

    submitted = []

    async def _submit(payload, **kwargs):
        submitted.append((payload, kwargs))

    monkeypatch.setattr(h, "worker_submit", _submit)
    monkeypatch.setattr(h, "get_redis_client", lambda: None)
    monkeypatch.setattr(h, "WindowedLimiter", lambda redis: _StubLimiter(allow=True))

    async def _verify():
        return object()

    async def _session():
        yield session

    api.dependency_overrides[verify_api_key] = _verify
    api.dependency_overrides[get_skrift_db_session] = _session
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        c.submitted = submitted  # type: ignore[attr-defined]
        yield c
    api.dependency_overrides.pop(verify_api_key, None)
    api.dependency_overrides.pop(get_skrift_db_session, None)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def _activity_rows(session) -> list[MemberActivity]:
    return list((await session.execute(select(MemberActivity))).scalars().all())


# --- batch ingestion ----------------------------------------------------------


async def test_batch_creates_new_rows(client, session):
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/activity/batch",
        json={
            "events": [
                {"guild_id": "G1", "user_id": "U1", "message_at": _iso(now)},
                {"guild_id": "G1", "user_id": "U2", "message_at": _iso(now)},
            ]
        },
    )
    assert resp.status_code == 200
    rows = await _activity_rows(session)
    assert len(rows) == 2
    assert all(r.first_message_at is not None for r in rows)


async def test_batch_advances_last_but_keeps_first(client, session):
    early = datetime.now(timezone.utc) - timedelta(days=10)
    late = datetime.now(timezone.utc)
    await client.post(
        "/activity/batch",
        json={"events": [{"guild_id": "G1", "user_id": "U1", "message_at": _iso(early)}]},
    )
    await client.post(
        "/activity/batch",
        json={"events": [{"guild_id": "G1", "user_id": "U1", "message_at": _iso(late)}]},
    )
    row = (await _activity_rows(session))[0]
    assert row.first_message_at.replace(tzinfo=timezone.utc) <= early + timedelta(seconds=1)
    assert row.last_message_at.replace(tzinfo=timezone.utc) >= late - timedelta(seconds=1)


async def test_batch_ignores_stale_timestamps(client, session):
    late = datetime.now(timezone.utc)
    early = late - timedelta(days=3)
    await client.post(
        "/activity/batch",
        json={"events": [{"guild_id": "G1", "user_id": "U1", "message_at": _iso(late)}]},
    )
    await client.post(
        "/activity/batch",
        json={"events": [{"guild_id": "G1", "user_id": "U1", "message_at": _iso(early)}]},
    )
    row = (await _activity_rows(session))[0]
    assert row.last_message_at.replace(tzinfo=timezone.utc) >= late - timedelta(seconds=1)


# --- dispatch enrichment ------------------------------------------------------


def _handler(channel_id="C1"):
    return ChannelHandler(
        guild_id="G1",
        channel_id=channel_id,
        name="watcher",
        trigger_type="message",
        settings={},
        description="d",
        script="pass\n",
        created_by="U1",
    )


def _dispatch_body(author_id="U7"):
    return {
        "guild_id": "G1",
        "channel_id": "C1",
        "trigger_type": "message",
        "trigger_context": {
            "trigger_type": "message",
            "message_content": "hello",
            "author_id": author_id,
        },
    }


async def test_dispatch_marks_first_message(client, session):
    session.add(_handler())
    await session.commit()
    resp = await client.post("/handlers/dispatch", json=_dispatch_body())
    assert resp.json()["dispatched"] is True
    payload, _ = client.submitted[0]  # type: ignore[attr-defined]
    ctx = payload.trigger_context
    assert ctx["author_is_first_message"] is True
    assert ctx["author_days_since_last_message"] is None
    assert ctx["author_last_message_at"] is None
    # The dispatch itself records the activity.
    rows = await _activity_rows(session)
    assert len(rows) == 1 and rows[0].user_id == "U7"


async def test_dispatch_reports_days_since_last_message(client, session):
    session.add(_handler())
    session.add(
        MemberActivity(
            guild_id="G1",
            user_id="U7",
            first_message_at=datetime.now(timezone.utc) - timedelta(days=100),
            last_message_at=datetime.now(timezone.utc) - timedelta(days=71),
        )
    )
    await session.commit()
    await client.post("/handlers/dispatch", json=_dispatch_body())
    payload, _ = client.submitted[0]  # type: ignore[attr-defined]
    ctx = payload.trigger_context
    assert ctx["author_is_first_message"] is False
    assert ctx["author_days_since_last_message"] == 71
    assert ctx["author_last_message_at"] is not None
    # Row advanced to now.
    row = (await _activity_rows(session))[0]
    assert (datetime.now(timezone.utc) - row.last_message_at.replace(tzinfo=timezone.utc)).total_seconds() < 60


async def test_dispatch_without_author_id_adds_no_facts(client, session):
    session.add(
        ChannelHandler(
            guild_id="G1", channel_id="C1", name="rx", trigger_type="reaction",
            settings={}, description="d", script="pass\n", created_by="U1",
        )
    )
    await session.commit()
    resp = await client.post(
        "/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "reaction",
            "trigger_context": {"trigger_type": "reaction", "reaction_user_id": "U9"},
        },
    )
    assert resp.json()["dispatched"] is True
    payload, _ = client.submitted[0]  # type: ignore[attr-defined]
    assert "author_is_first_message" not in payload.trigger_context
