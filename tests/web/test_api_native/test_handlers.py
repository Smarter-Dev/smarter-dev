"""Parity tests for the native (Litestar) channel-handlers controller.

Port of the FastAPI suite ``tests/web/test_api/test_handlers.py`` against
``smarter_dev.web.api_native.handlers`` — same in-memory SQLite database, same
stubbed worker/limiter seams, same status codes and JSON bodies, with the
final ``/api/handlers`` paths the bot client sends.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient, create_test_client
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base
from smarter_dev.web.api_native import handlers as handlers_module
from smarter_dev.web.api_native.handlers import HandlerController
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
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def submitted(monkeypatch) -> list[tuple]:
    """Capture ``worker_submit`` calls and stub the scheduling/limiter seams."""
    captured: list[tuple] = []

    async def _submit(payload, **kwargs):
        captured.append((payload, kwargs))

    _StubJobHandle.cancelled = []
    monkeypatch.setattr(handlers_module, "worker_submit", _submit)
    monkeypatch.setattr(handlers_module, "get_handle", _StubJobHandle)
    monkeypatch.setattr(handlers_module, "get_redis_client", lambda: None)
    monkeypatch.setattr(
        handlers_module, "WindowedLimiter", lambda redis: _StubLimiter(allow=True)
    )
    return captured


@pytest.fixture
def client(db_session, submitted) -> Iterator[TestClient]:
    """Litestar client serving the handlers controller with guards bypassed.

    The routes share the ``handlers.BOT_API_GUARDS`` list by reference, so
    emptying it before the app is built removes the guards for these tests
    only. Auth is covered separately by ``test_auth.py``.
    """
    original_guards = list(handlers_module.BOT_API_GUARDS)
    handlers_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[HandlerController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: db_session, sync_to_thread=False)
            },
        ) as test_client:
            test_client.submitted = submitted  # type: ignore[attr-defined]
            yield test_client
    finally:
        handlers_module.BOT_API_GUARDS[:] = original_guards


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


def test_create_event_handler(client):
    resp = client.post("/api/handlers", json=_event_body())
    assert resp.status_code == 201
    assert resp.json()["trigger_type"] == "message"
    assert resp.json()["name"] == "huzzah-reactor"


def test_multiple_handlers_per_trigger_coexist(client):
    first = client.post("/api/handlers", json=_event_body(name="greeter"))
    second = client.post("/api/handlers", json=_event_body(name="mood-tracker"))
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["handler_id"] != second.json()["handler_id"]
    listed = client.get("/api/handlers", params={"channel_id": "C1"})
    assert {r["name"] for r in listed.json()} == {"greeter", "mood-tracker"}


def test_duplicate_name_in_channel_is_conflict(client):
    client.post("/api/handlers", json=_event_body(name="greeter"))
    dupe = client.post(
        "/api/handlers", json=_event_body(name="greeter", trigger_type="reaction")
    )
    assert dupe.status_code == 409
    # Same name in a DIFFERENT channel is fine.
    other = client.post(
        "/api/handlers", json=_event_body(name="greeter", channel_id="C2")
    )
    assert other.status_code == 201


def test_blank_name_is_rejected(client):
    resp = client.post("/api/handlers", json=_event_body(name="   "))
    assert resp.status_code == 422
    assert resp.json() == {"detail": "name is required"}


def test_unknown_trigger_type_is_rejected(client):
    resp = client.post("/api/handlers", json=_event_body(trigger_type="telepathy"))
    assert resp.status_code == 422
    assert resp.json() == {"detail": "unknown trigger_type"}


def test_handler_count_cap_per_channel(client):
    for n in range(MAX_HANDLERS_PER_CHANNEL):
        resp = client.post("/api/handlers", json=_event_body(name=f"h{n}"))
        assert resp.status_code == 201
    over = client.post("/api/handlers", json=_event_body(name="one-too-many"))
    assert over.status_code == 422


def test_create_timer_schedules_first_fire(client):
    body = _event_body(
        trigger_type="timer",
        settings={"delay_seconds": 3600},
        script='await send_message("reminder")\n',
        description="remind in an hour",
    )
    resp = client.post("/api/handlers", json=body)
    assert resp.status_code == 201
    assert len(client.submitted) == 1  # type: ignore[attr-defined]
    _, kwargs = client.submitted[0]  # type: ignore[attr-defined]
    assert "scheduled_for" in kwargs and "job_id" in kwargs


def test_schedule_below_floor_is_rejected(client):
    body = _event_body(
        trigger_type="schedule",
        settings={"interval_seconds": 5},
        script='await send_message("spam")\n',
    )
    resp = client.post("/api/handlers", json=body)
    assert resp.status_code == 422


def test_list_and_delete(client):
    client.post("/api/handlers", json=_event_body())
    listed = client.get("/api/handlers", params={"channel_id": "C1"})
    assert len(listed.json()) == 1
    assert "script" not in listed.json()[0]
    handler_id = listed.json()[0]["handler_id"]
    deleted = client.delete(f"/api/handlers/{handler_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": handler_id}
    again = client.get("/api/handlers", params={"channel_id": "C1"})
    assert again.json() == []


def test_list_requires_channel_id(client):
    resp = client.get("/api/handlers")
    assert resp.status_code == 422


def test_list_with_scripts(client):
    client.post("/api/handlers", json=_event_body())
    listed = client.get(
        "/api/handlers", params={"channel_id": "C1", "include_scripts": "true"}
    )
    assert listed.json()[0]["script"].startswith("await add_reaction")


def test_edit_handler_updates_script_and_description(client):
    created = client.post("/api/handlers", json=_event_body())
    handler_id = created.json()["handler_id"]
    resp = client.put(
        f"/api/handlers/{handler_id}",
        json={
            "description": "react on hooray too",
            "script": 'await add_reaction(context["message_id"], "🎊")\n',
            "settings": {},
        },
    )
    assert resp.status_code == 200
    detail = client.get(f"/api/handlers/{handler_id}")
    assert detail.json()["description"] == "react on hooray too"
    assert "🎊" in detail.json()["script"]
    assert detail.json()["name"] == "huzzah-reactor"  # unchanged without rename


def test_edit_can_rename_but_not_to_taken_name(client):
    client.post("/api/handlers", json=_event_body(name="greeter"))
    created = client.post("/api/handlers", json=_event_body(name="mood"))
    handler_id = created.json()["handler_id"]
    renamed = client.put(
        f"/api/handlers/{handler_id}",
        json={"description": "d", "script": "pass\n", "settings": {}, "name": "vibes"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "vibes"
    collision = client.put(
        f"/api/handlers/{handler_id}",
        json={"description": "d", "script": "pass\n", "settings": {}, "name": "greeter"},
    )
    assert collision.status_code == 409


def test_edit_time_handler_cancels_and_reschedules(client):
    body = _event_body(
        trigger_type="schedule",
        settings={"interval_seconds": 3600},
        script='await send_message("hourly")\n',
    )
    created = client.post("/api/handlers", json=body)
    handler_id = created.json()["handler_id"]
    assert len(client.submitted) == 1  # type: ignore[attr-defined]

    resp = client.put(
        f"/api/handlers/{handler_id}",
        json={
            "description": "every two hours",
            "script": 'await send_message("bihourly")\n',
            "settings": {"interval_seconds": 7200},
        },
    )
    assert resp.status_code == 200
    assert len(_StubJobHandle.cancelled) == 1
    assert len(client.submitted) == 2  # type: ignore[attr-defined]


def test_edit_unknown_handler_is_404(client):
    resp = client.put(
        "/api/handlers/00000000-0000-0000-0000-000000000000",
        json={"description": "d", "script": "pass\n", "settings": {}},
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "handler not found"}


def test_malformed_handler_id_is_422(client):
    resp = client.put(
        "/api/handlers/not-a-uuid",
        json={"description": "d", "script": "pass\n", "settings": {}},
    )
    assert resp.status_code == 422


def test_dispatch_no_handler(client):
    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {},
        },
    )
    assert resp.json()["dispatched"] is False


def test_dispatch_fires_all_standard_handlers_for_trigger(client):
    client.post("/api/handlers", json=_event_body(name="greeter"))
    client.post("/api/handlers", json=_event_body(name="mood-tracker"))
    resp = client.post(
        "/api/handlers/dispatch",
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


def test_dispatch_rate_limited(client, monkeypatch):
    monkeypatch.setattr(
        handlers_module, "WindowedLimiter", lambda redis: _StubLimiter(allow=False)
    )
    client.post("/api/handlers", json=_event_body())
    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {},
        },
    )
    assert resp.json()["dispatched"] is False


async def test_dispatch_fans_out_to_standard_and_admin(client, db_session):
    from smarter_dev.web.models import AdminHandler, ChannelHandler

    # one standard handler in C1, one all-channel admin handler, one admin
    # handler scoped to a different channel (should NOT fire for C1).
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="C1", name="std", trigger_type="message",
        settings={}, description="std", script="await send_message('x')\n",
        created_by="U1",
    ))
    db_session.add(AdminHandler(
        guild_id="G1", name="all-chan", trigger_type="message", settings={},
        channel_ids=[], description="all-chan admin",
        script="await send_message('y')\n", created_by_admin="A1",
    ))
    db_session.add(AdminHandler(
        guild_id="G1", name="scoped", trigger_type="message", settings={},
        channel_ids=["OTHER"], description="scoped admin",
        script="await send_message('z')\n", created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {},
        },
    )
    body = resp.json()
    assert body["dispatched"] is True
    # standard + all-channel admin = 2 fires; the OTHER-scoped admin is skipped.
    assert len(body["handler_ids"]) == 2
    assert len(client.submitted) == 2  # type: ignore[attr-defined]


def test_active_channels(client):
    client.post("/api/handlers", json=_event_body())
    client.post("/api/handlers", json=_event_body(name="rx", trigger_type="reaction"))
    resp = client.get("/api/handlers/active-channels")
    channels = resp.json()["channels"]
    assert ["C1", "message"] in channels
    assert ["C1", "reaction"] in channels
