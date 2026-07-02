"""Tests for the handlers API router (FastAPI, in-memory SQLite)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base, get_skrift_db_session
from smarter_dev.web.api.app import api
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.handler_caps import MAX_HANDLERS_PER_CHANNEL


class _StubLimiter:
    def __init__(self, redis=None, allow=True):
        self.allow = allow

    async def hit(self, key, limit):
        return self.allow


class _StubJobHandle:
    cancelled: list[str] = []

    def __init__(self, job_id):
        self.job_id = job_id

    async def cancel(self):
        _StubJobHandle.cancelled.append(self.job_id)


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

    _StubJobHandle.cancelled = []
    monkeypatch.setattr(h, "worker_submit", _submit)
    monkeypatch.setattr(h, "get_handle", _StubJobHandle)
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
        "name": "huzzah-reactor",
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
    assert resp.json()["name"] == "huzzah-reactor"


async def test_multiple_handlers_per_trigger_coexist(client):
    first = await client.post("/handlers", json=_event_body(name="greeter"))
    second = await client.post("/handlers", json=_event_body(name="mood-tracker"))
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["handler_id"] != second.json()["handler_id"]
    listed = await client.get("/handlers", params={"channel_id": "C1"})
    assert {r["name"] for r in listed.json()} == {"greeter", "mood-tracker"}


async def test_duplicate_name_in_channel_is_conflict(client):
    await client.post("/handlers", json=_event_body(name="greeter"))
    dupe = await client.post(
        "/handlers", json=_event_body(name="greeter", trigger_type="reaction")
    )
    assert dupe.status_code == 409
    # Same name in a DIFFERENT channel is fine.
    other = await client.post(
        "/handlers", json=_event_body(name="greeter", channel_id="C2")
    )
    assert other.status_code == 201


async def test_blank_name_is_rejected(client):
    resp = await client.post("/handlers", json=_event_body(name="   "))
    assert resp.status_code == 422


async def test_handler_count_cap_per_channel(client):
    for n in range(MAX_HANDLERS_PER_CHANNEL):
        resp = await client.post("/handlers", json=_event_body(name=f"h{n}"))
        assert resp.status_code == 201
    over = await client.post("/handlers", json=_event_body(name="one-too-many"))
    assert over.status_code == 422


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
    assert "script" not in listed.json()[0]
    handler_id = listed.json()[0]["handler_id"]
    deleted = await client.delete(f"/handlers/{handler_id}")
    assert deleted.status_code == 200
    again = await client.get("/handlers", params={"channel_id": "C1"})
    assert again.json() == []


async def test_list_with_scripts(client):
    await client.post("/handlers", json=_event_body())
    listed = await client.get(
        "/handlers", params={"channel_id": "C1", "include_scripts": "true"}
    )
    assert listed.json()[0]["script"].startswith("await add_reaction")


async def test_edit_handler_updates_script_and_description(client):
    created = await client.post("/handlers", json=_event_body())
    handler_id = created.json()["handler_id"]
    resp = await client.put(
        f"/handlers/{handler_id}",
        json={
            "description": "react on hooray too",
            "script": 'await add_reaction(context["message_id"], "🎊")\n',
            "settings": {},
        },
    )
    assert resp.status_code == 200
    detail = await client.get(f"/handlers/{handler_id}")
    assert detail.json()["description"] == "react on hooray too"
    assert "🎊" in detail.json()["script"]
    assert detail.json()["name"] == "huzzah-reactor"  # unchanged without rename


async def test_edit_can_rename_but_not_to_taken_name(client):
    await client.post("/handlers", json=_event_body(name="greeter"))
    created = await client.post("/handlers", json=_event_body(name="mood"))
    handler_id = created.json()["handler_id"]
    renamed = await client.put(
        f"/handlers/{handler_id}",
        json={"description": "d", "script": "pass\n", "settings": {}, "name": "vibes"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "vibes"
    collision = await client.put(
        f"/handlers/{handler_id}",
        json={"description": "d", "script": "pass\n", "settings": {}, "name": "greeter"},
    )
    assert collision.status_code == 409


async def test_edit_time_handler_cancels_and_reschedules(client):
    body = _event_body(
        trigger_type="schedule",
        settings={"interval_seconds": 3600},
        script='await send_message("hourly")\n',
    )
    created = await client.post("/handlers", json=body)
    handler_id = created.json()["handler_id"]
    assert len(client.submitted) == 1  # type: ignore[attr-defined]

    resp = await client.put(
        f"/handlers/{handler_id}",
        json={
            "description": "every two hours",
            "script": 'await send_message("bihourly")\n',
            "settings": {"interval_seconds": 7200},
        },
    )
    assert resp.status_code == 200
    assert len(_StubJobHandle.cancelled) == 1
    assert len(client.submitted) == 2  # type: ignore[attr-defined]


async def test_edit_unknown_handler_is_404(client):
    resp = await client.put(
        "/handlers/00000000-0000-0000-0000-000000000000",
        json={"description": "d", "script": "pass\n", "settings": {}},
    )
    assert resp.status_code == 404


async def test_dispatch_no_handler(client):
    resp = await client.post(
        "/handlers/dispatch",
        json={"guild_id": "G1", "channel_id": "C1", "trigger_type": "message", "trigger_context": {}},
    )
    assert resp.json()["dispatched"] is False


async def test_dispatch_fires_all_standard_handlers_for_trigger(client):
    await client.post("/handlers", json=_event_body(name="greeter"))
    await client.post("/handlers", json=_event_body(name="mood-tracker"))
    resp = await client.post(
        "/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {"trigger_type": "message", "message_content": "huzzah"},
        },
    )
    assert resp.json()["dispatched"] is True
    assert len(resp.json()["handler_ids"]) == 2
    assert len(client.submitted) == 2  # type: ignore[attr-defined]


async def test_dispatch_rate_limited(client, monkeypatch):
    import smarter_dev.web.api.routers.handlers as h

    monkeypatch.setattr(h, "WindowedLimiter", lambda redis: _StubLimiter(allow=False))
    await client.post("/handlers", json=_event_body())
    resp = await client.post(
        "/handlers/dispatch",
        json={"guild_id": "G1", "channel_id": "C1", "trigger_type": "message", "trigger_context": {}},
    )
    assert resp.json()["dispatched"] is False


async def test_dispatch_fans_out_to_standard_and_admin(client, session):
    from smarter_dev.web.models import AdminHandler, ChannelHandler

    # one standard handler in C1, one all-channel admin handler, one admin handler
    # scoped to a different channel (should NOT fire for C1).
    session.add(ChannelHandler(
        guild_id="G1", channel_id="C1", name="std", trigger_type="message",
        settings={}, description="std", script="await send_message('x')\n",
        created_by="U1",
    ))
    session.add(AdminHandler(
        guild_id="G1", name="all-chan", trigger_type="message", settings={},
        channel_ids=[], description="all-chan admin",
        script="await send_message('y')\n", created_by_admin="A1",
    ))
    session.add(AdminHandler(
        guild_id="G1", name="scoped", trigger_type="message", settings={},
        channel_ids=["OTHER"], description="scoped admin",
        script="await send_message('z')\n", created_by_admin="A1",
    ))
    await session.commit()

    resp = await client.post(
        "/handlers/dispatch",
        json={"guild_id": "G1", "channel_id": "C1", "trigger_type": "message", "trigger_context": {}},
    )
    body = resp.json()
    assert body["dispatched"] is True
    # standard + all-channel admin = 2 fires; the OTHER-scoped admin is skipped.
    assert len(body["handler_ids"]) == 2
    assert len(client.submitted) == 2  # type: ignore[attr-defined]


async def test_active_channels(client):
    await client.post("/handlers", json=_event_body())
    await client.post("/handlers", json=_event_body(name="rx", trigger_type="reaction"))
    resp = await client.get("/handlers/active-channels")
    channels = resp.json()["channels"]
    assert ["C1", "message"] in channels
    assert ["C1", "reaction"] in channels
