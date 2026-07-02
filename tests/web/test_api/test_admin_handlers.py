"""Tests for the admin-handlers API + isolation from the chatbot path."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base, get_skrift_db_session
from smarter_dev.web.api.app import api
from smarter_dev.web.api.dependencies import verify_api_key


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
    import smarter_dev.web.api.routers.admin_handlers as ah

    submitted = []

    async def _submit(payload, **kwargs):
        submitted.append((payload, kwargs))

    _StubJobHandle.cancelled = []
    monkeypatch.setattr(ah, "worker_submit", _submit)
    monkeypatch.setattr(ah, "get_handle", _StubJobHandle)

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


async def test_create_list_delete_admin_handler(client):
    created = await client.post("/admin/handlers", json=_body())
    assert created.status_code == 201
    data = created.json()
    assert data["trigger_type"] == "message"
    assert data["channel_ids"] == []
    assert data["name"] == "scam-banner"
    hid = data["handler_id"]

    listed = await client.get("/admin/handlers", params={"guild_id": "G1"})
    assert len(listed.json()) == 1
    assert "script" not in listed.json()[0]

    deleted = await client.delete(f"/admin/handlers/{hid}")
    assert deleted.status_code == 200
    assert (await client.get("/admin/handlers", params={"guild_id": "G1"})).json() == []


async def test_multiple_admin_handlers_per_trigger_coexist(client):
    first = await client.post("/admin/handlers", json=_body(name="scam-banner"))
    second = await client.post("/admin/handlers", json=_body(name="spam-sweeper"))
    assert first.status_code == 201 and second.status_code == 201
    listed = await client.get("/admin/handlers", params={"guild_id": "G1"})
    assert {r["name"] for r in listed.json()} == {"scam-banner", "spam-sweeper"}


async def test_duplicate_admin_name_in_guild_is_conflict(client):
    await client.post("/admin/handlers", json=_body(name="scam-banner"))
    dupe = await client.post(
        "/admin/handlers", json=_body(name="scam-banner", trigger_type="reaction")
    )
    assert dupe.status_code == 409
    other_guild = await client.post(
        "/admin/handlers", json=_body(name="scam-banner", guild_id="G2")
    )
    assert other_guild.status_code == 201


async def test_list_admin_handlers_with_scripts(client):
    await client.post("/admin/handlers", json=_body())
    listed = await client.get(
        "/admin/handlers", params={"guild_id": "G1", "include_scripts": "true"}
    )
    assert listed.json()[0]["script"].startswith("await ban_user")


async def test_edit_admin_handler(client):
    created = await client.post("/admin/handlers", json=_body())
    hid = created.json()["handler_id"]
    resp = await client.put(
        f"/admin/handlers/{hid}",
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


async def test_edit_admin_rename_collision_is_conflict(client):
    await client.post("/admin/handlers", json=_body(name="scam-banner"))
    created = await client.post("/admin/handlers", json=_body(name="spam-sweeper"))
    hid = created.json()["handler_id"]
    collision = await client.put(
        f"/admin/handlers/{hid}",
        json={
            "description": "d",
            "script": "pass\n",
            "settings": {},
            "channel_ids": [],
            "name": "scam-banner",
        },
    )
    assert collision.status_code == 409


async def test_edit_scheduled_admin_handler_reschedules(client):
    created = await client.post(
        "/admin/handlers",
        json=_body(
            trigger_type="schedule",
            settings={"interval_seconds": 3600},
            channel_ids=["MODCHAT"],
            script='await send_message("tick", "MODCHAT")\n',
        ),
    )
    hid = created.json()["handler_id"]
    assert len(client.submitted) == 1  # type: ignore[attr-defined]

    resp = await client.put(
        f"/admin/handlers/{hid}",
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


async def test_create_scheduled_admin_handler_schedules_fire(client):
    body = _body(
        trigger_type="timer",
        settings={"delay_seconds": 60},
        channel_ids=["MODCHAT"],
        script='await send_message("tick", "MODCHAT")\n',
    )
    resp = await client.post("/admin/handlers", json=body)
    assert resp.status_code == 201
    assert resp.json()["channel_ids"] == ["MODCHAT"]


def test_chatbot_tools_cannot_create_admin_handlers():
    # The member chatbot tools have no admin-handler tool — isolation by design.
    from smarter_dev.bot.agents.handler_tools import handler_tool_functions

    names = {f.__name__ for f in handler_tool_functions()}
    assert names == {"register_handler", "list_handlers", "delete_handler"}
    assert not any("admin" in n for n in names)
