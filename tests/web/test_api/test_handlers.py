"""Tests for the handlers API router (FastAPI, in-memory SQLite)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base, get_skrift_db_session
from smarter_dev.web.api.app import api
from smarter_dev.web.api.dependencies import verify_api_key


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


def _event_body(**over):
    body = {
        "guild_id": "G1",
        "channel_id": "C1",
        "trigger_type": "message",
        "settings": {},
        "description": "react on huzzah",
        "script": 'await add_reaction(context["message_id"], "🎉")\n',
        "created_by": "U1",
    }
    body.update(over)
    return body


async def test_create_event_handler(client):
    resp = await client.post("/handlers", json=_event_body())
    assert resp.status_code == 201
    assert resp.json()["trigger_type"] == "message"


async def test_event_handler_is_single_listener_replace(client):
    first = await client.post("/handlers", json=_event_body(description="v1"))
    second = await client.post("/handlers", json=_event_body(description="v2"))
    assert first.json()["handler_id"] == second.json()["handler_id"]
    assert second.json()["description"] == "v2"


async def test_create_timer_schedules_first_fire(client):
    body = _event_body(
        trigger_type="timer",
        settings={"delay_seconds": 3600},
        script='await send_message("reminder")\n',
        description="remind in an hour",
    )
    resp = await client.post("/handlers", json=body)
    assert resp.status_code == 201
    assert len(client.submitted) == 1  # type: ignore[attr-defined]
    _, kwargs = client.submitted[0]  # type: ignore[attr-defined]
    assert "scheduled_for" in kwargs and "job_id" in kwargs


async def test_schedule_below_floor_is_rejected(client):
    body = _event_body(
        trigger_type="schedule",
        settings={"interval_seconds": 5},
        script='await send_message("spam")\n',
    )
    resp = await client.post("/handlers", json=body)
    assert resp.status_code == 422


async def test_list_and_delete(client):
    await client.post("/handlers", json=_event_body())
    listed = await client.get("/handlers", params={"channel_id": "C1"})
    assert len(listed.json()) == 1
    handler_id = listed.json()[0]["handler_id"]
    deleted = await client.delete(f"/handlers/{handler_id}")
    assert deleted.status_code == 200
    again = await client.get("/handlers", params={"channel_id": "C1"})
    assert again.json() == []


async def test_dispatch_no_handler(client):
    resp = await client.post(
        "/handlers/dispatch",
        json={"channel_id": "C1", "trigger_type": "message", "trigger_context": {}},
    )
    assert resp.json()["dispatched"] is False


async def test_dispatch_enqueues_when_handler_present(client):
    await client.post("/handlers", json=_event_body())
    resp = await client.post(
        "/handlers/dispatch",
        json={
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {"trigger_type": "message", "message_content": "huzzah"},
        },
    )
    assert resp.json()["dispatched"] is True
    assert len(client.submitted) == 1  # type: ignore[attr-defined]


async def test_dispatch_rate_limited(client, monkeypatch):
    import smarter_dev.web.api.routers.handlers as h

    monkeypatch.setattr(h, "WindowedLimiter", lambda redis: _StubLimiter(allow=False))
    await client.post("/handlers", json=_event_body())
    resp = await client.post(
        "/handlers/dispatch",
        json={"channel_id": "C1", "trigger_type": "message", "trigger_context": {}},
    )
    assert resp.json() == {"dispatched": False, "reason": "rate_limited"}


async def test_active_channels(client):
    await client.post("/handlers", json=_event_body())
    await client.post("/handlers", json=_event_body(trigger_type="reaction"))
    resp = await client.get("/handlers/active-channels")
    channels = resp.json()["channels"]
    assert ["C1", "message"] in channels
    assert ["C1", "reaction"] in channels
