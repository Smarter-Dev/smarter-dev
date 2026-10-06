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
from datetime import UTC
from datetime import datetime

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import AsyncTestClient
from litestar.testing import TestClient
from litestar.testing import create_async_test_client
from litestar.testing import create_test_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base
from smarter_dev.web import handler_recurrence
from smarter_dev.web.api_native import admin_handlers as admin_handlers_module
from smarter_dev.web.api_native.admin_handlers import AdminHandlerController
from smarter_dev.web.models import AdminHandler


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
    monkeypatch.setattr(handler_recurrence, "worker_submit", _submit)
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


@pytest.fixture
def no_guards():
    """Guards bypassed, as for ``client``, for a test that builds its own app."""
    original_guards = list(admin_handlers_module.BOT_API_GUARDS)
    admin_handlers_module.BOT_API_GUARDS.clear()
    yield
    admin_handlers_module.BOT_API_GUARDS[:] = original_guards


def _async_client(db_session) -> AsyncTestClient:
    """The same app as ``client``, for a test that also drives the session."""
    return create_async_test_client(
        route_handlers=[AdminHandlerController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: db_session, sync_to_thread=False)},
    )


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
        json=_body(
            name="stat-counter",
            trigger_type="schedule",
            settings={"interval_seconds": 600},
            script="pass\n",
        ),
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


def _script_of(client, handler_id: str) -> dict:
    rows = client.get(
        "/api/admin/handlers", params={"guild_id": "G1", "include_scripts": "true"}
    ).json()
    return next(r for r in rows if r["handler_id"] == handler_id)


async def test_replace_script_changes_the_script_and_nothing_else(
    db_session, submitted, no_guards
):
    # A handler somebody disabled, with settings and a scope. The script-only
    # write must change the script and leave every one of those as it was —
    # the full update would switch the handler back on.
    record = AdminHandler(
        guild_id="G1",
        name="scam-banner",
        trigger_type="message",
        settings={"include_bot_messages": True},
        channel_ids=["C1"],
        description="ban scammers",
        script="old = 1\n",
        created_by_admin="A1",
        enabled=False,
    )
    db_session.add(record)
    await db_session.commit()
    handler_id = str(record.id)

    async with _async_client(db_session) as client:
        resp = await client.put(
            f"/api/admin/handlers/{handler_id}/script",
            json={"script": "new = 2\n", "expected_script": "old = 1\n"},
        )
        assert resp.status_code == 200
        rows = (
            await client.get(
                "/api/admin/handlers",
                params={"guild_id": "G1", "include_scripts": "true"},
            )
        ).json()

    after = next(r for r in rows if r["handler_id"] == handler_id)
    assert after["script"] == "new = 2\n"
    assert after["enabled"] is False
    assert after["description"] == "ban scammers"
    assert after["settings"] == {"include_bot_messages": True}
    assert after["channel_ids"] == ["C1"]
    assert after["name"] == "scam-banner"


def test_replace_script_refuses_when_the_script_moved(client):
    created = client.post("/api/admin/handlers", json=_body()).json()
    handler_id = created["handler_id"]
    client.put(
        f"/api/admin/handlers/{handler_id}/script",
        json={"script": "first = 1\n", "expected_script": _body()["script"]},
    )

    resp = client.put(
        f"/api/admin/handlers/{handler_id}/script",
        json={"script": "second = 2\n", "expected_script": _body()["script"]},
    )

    assert resp.status_code == 409
    assert "changed since it was read" in resp.text
    assert _script_of(client, handler_id)["script"] == "first = 1\n"


async def test_replace_script_lets_only_one_of_two_stale_readers_through(
    db_session, submitted, no_guards
):
    # Two editors read the same script. Each session holds its own copy of
    # the row, so a check against the loaded record would pass for both and
    # the second write would quietly replace the first. The check has to be
    # the database's: the write lands only where the script is still what
    # was read.
    record = AdminHandler(
        guild_id="G1",
        name="scam-banner",
        trigger_type="message",
        settings={},
        channel_ids=[],
        description="ban scammers",
        script="old = 1\n",
        created_by_admin="A1",
    )
    db_session.add(record)
    await db_session.commit()
    handler_id = str(record.id)
    other_session = async_sessionmaker(db_session.bind, expire_on_commit=False)()
    # Both sessions now hold the old script.
    assert (await other_session.get(AdminHandler, record.id)).script == "old = 1\n"
    assert (await db_session.get(AdminHandler, record.id)).script == "old = 1\n"

    async with _async_client(other_session) as first:
        first_write = await first.put(
            f"/api/admin/handlers/{handler_id}/script",
            json={"script": "first = 1\n", "expected_script": "old = 1\n"},
        )
    async with _async_client(db_session) as second:
        second_write = await second.put(
            f"/api/admin/handlers/{handler_id}/script",
            json={"script": "second = 2\n", "expected_script": "old = 1\n"},
        )
    await other_session.close()
    # The column itself: a loaded record would only show its session's copy.
    async with async_sessionmaker(db_session.bind)() as fresh:
        stored = await fresh.scalar(
            select(AdminHandler.script).where(AdminHandler.id == record.id)
        )

    assert first_write.status_code == 200
    assert second_write.status_code == 409
    assert stored == "first = 1\n"


def test_replace_script_on_unknown_handler_is_404(client):
    resp = client.put(
        "/api/admin/handlers/00000000-0000-0000-0000-000000000000/script",
        json={"script": "x = 1\n", "expected_script": ""},
    )
    assert resp.status_code == 404


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
        json={
            "description": "d",
            "script": "pass\n",
            "settings": {},
            "channel_ids": [],
        },
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


def test_admin_schedule_start_at_is_persisted_and_used_for_first_fire(client):
    start_at = "2099-08-01T14:30:00+00:00"
    body = _body(
        trigger_type="schedule",
        settings={"interval_seconds": 3600, "start_at": start_at},
        channel_ids=["MODCHAT"],
        script='await send_message("tick", "MODCHAT")\n',
    )
    resp = client.post("/api/admin/handlers", json=body)
    assert resp.status_code == 201
    assert resp.json()["settings"]["start_at"] == start_at
    _, kwargs = client.submitted[0]  # type: ignore[attr-defined]
    assert kwargs["scheduled_for"] == datetime(2099, 8, 1, 14, 30, tzinfo=UTC)


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
