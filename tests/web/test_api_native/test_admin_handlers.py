"""Parity tests for the native (Litestar) admin-handlers controller.

Port of the FastAPI suite ``tests/web/test_api/test_admin_handlers.py`` against
``smarter_dev.web.api_native.admin_handlers`` — same in-memory SQLite database,
same stubbed worker seams, same status codes and JSON bodies, with the final
``/api/admin/handlers`` paths the bot client sends. (The legacy suite's
chatbot-tool isolation test stays with the bot suite — it exercises bot code,
not this API.)
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
from smarter_dev.web.api_native import admin_handlers as admin_handlers_module
from smarter_dev.web.api_native.admin_handlers import AdminHandlerController


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
    """Capture ``worker_submit`` calls and stub the job-handle seam."""
    captured: list[tuple] = []

    async def _submit(payload, **kwargs):
        captured.append((payload, kwargs))

    _StubJobHandle.cancelled = []
    monkeypatch.setattr(admin_handlers_module, "worker_submit", _submit)
    monkeypatch.setattr(admin_handlers_module, "get_handle", _StubJobHandle)
    return captured


@pytest.fixture
def client(db_session, submitted) -> Iterator[TestClient]:
    """Litestar client serving the admin-handlers controller, guards bypassed.

    The routes share the ``admin_handlers.BOT_API_GUARDS`` list by reference,
    so emptying it before the app is built removes the guards for these tests
    only. Auth is covered separately by ``test_auth.py``.
    """
    original_guards = list(admin_handlers_module.BOT_API_GUARDS)
    admin_handlers_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[AdminHandlerController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: db_session, sync_to_thread=False)
            },
        ) as test_client:
            test_client.submitted = submitted  # type: ignore[attr-defined]
            yield test_client
    finally:
        admin_handlers_module.BOT_API_GUARDS[:] = original_guards


def _body(**over):
    body = {
        "guild_id": "G1",
        "name": "scam-banner",
        "trigger_type": "message",
        "settings": {},
        "channel_ids": [],
        "description": "ban scammers",
        "script": 'await ban_user(context["author_id"])\n',
        "created_by_admin": "A1",
    }
    body.update(over)
    return body


def test_create_admin_handler_rejects_include_bot_messages_on_non_message(client):
    # The Disboard-confirmation opt-in only means anything on a message trigger.
    rejected = client.post(
        "/api/admin/handlers",
        json=_body(
            name="stat-counter",
            trigger_type="schedule",
            settings={"include_bot_messages": True, "interval_seconds": 600},
            script="pass\n",
        ),
    )
    assert rejected.status_code == 422
    # A message-trigger admin handler accepts it (this is the Disboard tracker).
    accepted = client.post(
        "/api/admin/handlers",
        json=_body(
            name="disboard-tracker",
            trigger_type="message",
            settings={"include_bot_messages": True},
        ),
    )
    assert accepted.status_code == 201


def test_update_admin_handler_rejects_include_bot_messages_on_non_message(client):
    created = client.post(
        "/api/admin/handlers",
        json=_body(name="stat-counter", trigger_type="schedule",
                   settings={"interval_seconds": 600}, script="pass\n"),
    )
    handler_id = created.json()["handler_id"]
    resp = client.put(
        f"/api/admin/handlers/{handler_id}",
        json={
            "description": "d",
            "script": "pass\n",
            "settings": {"include_bot_messages": True, "interval_seconds": 600},
            "channel_ids": [],
        },
    )
    assert resp.status_code == 422


def test_create_list_delete_admin_handler(client):
    created = client.post("/api/admin/handlers", json=_body())
    assert created.status_code == 201
    data = created.json()
    assert data["trigger_type"] == "message"
    assert data["channel_ids"] == []
    assert data["name"] == "scam-banner"
    handler_id = data["handler_id"]

    listed = client.get("/api/admin/handlers", params={"guild_id": "G1"})
    assert len(listed.json()) == 1
    assert "script" not in listed.json()[0]

    deleted = client.delete(f"/api/admin/handlers/{handler_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": handler_id}
    assert client.get("/api/admin/handlers", params={"guild_id": "G1"}).json() == []


def test_multiple_admin_handlers_per_trigger_coexist(client):
    first = client.post("/api/admin/handlers", json=_body(name="scam-banner"))
    second = client.post("/api/admin/handlers", json=_body(name="spam-sweeper"))
    assert first.status_code == 201 and second.status_code == 201
    listed = client.get("/api/admin/handlers", params={"guild_id": "G1"})
    assert {r["name"] for r in listed.json()} == {"scam-banner", "spam-sweeper"}


def test_duplicate_admin_name_in_guild_is_conflict(client):
    client.post("/api/admin/handlers", json=_body(name="scam-banner"))
    dupe = client.post(
        "/api/admin/handlers", json=_body(name="scam-banner", trigger_type="reaction")
    )
    assert dupe.status_code == 409
    other_guild = client.post(
        "/api/admin/handlers", json=_body(name="scam-banner", guild_id="G2")
    )
    assert other_guild.status_code == 201


def test_blank_admin_name_is_rejected(client):
    resp = client.post("/api/admin/handlers", json=_body(name="   "))
    assert resp.status_code == 422
    assert resp.json() == {"detail": "name is required"}


def test_list_admin_handlers_with_scripts(client):
    client.post("/api/admin/handlers", json=_body())
    listed = client.get(
        "/api/admin/handlers", params={"guild_id": "G1", "include_scripts": "true"}
    )
    assert listed.json()[0]["script"].startswith("await ban_user")


def test_edit_admin_handler(client):
    created = client.post("/api/admin/handlers", json=_body())
    handler_id = created.json()["handler_id"]
    resp = client.put(
        f"/api/admin/handlers/{handler_id}",
        json={
            "description": "ban scammers politely",
            "script": 'await ban_user(context["author_id"], "scam")\n',
            "settings": {},
            "channel_ids": ["MODCHAT"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["channel_ids"] == ["MODCHAT"]
    assert resp.json()["description"] == "ban scammers politely"


def test_edit_admin_rename_collision_is_conflict(client):
    client.post("/api/admin/handlers", json=_body(name="scam-banner"))
    created = client.post("/api/admin/handlers", json=_body(name="spam-sweeper"))
    handler_id = created.json()["handler_id"]
    collision = client.put(
        f"/api/admin/handlers/{handler_id}",
        json={
            "description": "d",
            "script": "pass\n",
            "settings": {},
            "channel_ids": [],
            "name": "scam-banner",
        },
    )
    assert collision.status_code == 409


def test_edit_unknown_admin_handler_is_404(client):
    resp = client.put(
        "/api/admin/handlers/00000000-0000-0000-0000-000000000000",
        json={"description": "d", "script": "pass\n", "settings": {}, "channel_ids": []},
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "admin handler not found"}


def test_malformed_admin_handler_id_is_422(client):
    resp = client.delete("/api/admin/handlers/not-a-uuid")
    assert resp.status_code == 422


def test_edit_scheduled_admin_handler_reschedules(client):
    created = client.post(
        "/api/admin/handlers",
        json=_body(
            trigger_type="schedule",
            settings={"interval_seconds": 3600},
            channel_ids=["MODCHAT"],
            script='await send_message("tick", "MODCHAT")\n',
        ),
    )
    handler_id = created.json()["handler_id"]
    assert len(client.submitted) == 1  # type: ignore[attr-defined]

    resp = client.put(
        f"/api/admin/handlers/{handler_id}",
        json={
            "description": "tock",
            "script": 'await send_message("tock", "MODCHAT")\n',
            "settings": {"interval_seconds": 7200},
            "channel_ids": ["MODCHAT"],
        },
    )
    assert resp.status_code == 200
    assert len(_StubJobHandle.cancelled) == 1
    assert len(client.submitted) == 2  # type: ignore[attr-defined]


def test_create_scheduled_admin_handler_schedules_fire(client):
    body = _body(
        trigger_type="timer",
        settings={"delay_seconds": 60},
        channel_ids=["MODCHAT"],
        script='await send_message("tick", "MODCHAT")\n',
    )
    resp = client.post("/api/admin/handlers", json=body)
    assert resp.status_code == 201
    assert resp.json()["channel_ids"] == ["MODCHAT"]


@pytest.mark.parametrize(
    "trigger",
    [
        "member_join",
        "member_leave",
        "member_rules_accepted",
        "member_role_change",
        "thread_create",
    ],
)
def test_create_admin_accepts_new_event_triggers(client, trigger):
    """Admin tier admits the five member/thread triggers; they are event
    triggers, so no first fire is scheduled (unlike ``schedule``/``timer``)."""
    resp = client.post(
        "/api/admin/handlers",
        json=_body(trigger_type=trigger, script="await send_message('hi', 'LOG')\n"),
    )
    assert resp.status_code == 201
    assert resp.json()["trigger_type"] == trigger
    assert len(client.submitted) == 0  # type: ignore[attr-defined]


def test_create_mod_action_handler_allowed(client):
    """The synthetic mod_action trigger is an authorable admin trigger; it is an
    event-style trigger so no first fire is scheduled on create."""
    resp = client.post(
        "/api/admin/handlers",
        json=_body(
            trigger_type="mod_action",
            name="mod-log-formatter",
            script="await send_message('logged', 'MODLOG')\n",
        ),
    )
    assert resp.status_code == 201
    assert resp.json()["trigger_type"] == "mod_action"
    assert len(client.submitted) == 0  # type: ignore[attr-defined]
