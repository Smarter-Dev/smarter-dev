"""Tests for the admin-handlers API + isolation from the chatbot path."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base, get_skrift_db_session
from smarter_dev.web.api.app import api
from smarter_dev.web.api.dependencies import verify_api_key


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

    async def _submit(payload, **kwargs):
        return None

    monkeypatch.setattr(ah, "worker_submit", _submit)

    async def _verify():
        return object()

    async def _session():
        yield session

    api.dependency_overrides[verify_api_key] = _verify
    api.dependency_overrides[get_skrift_db_session] = _session
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    api.dependency_overrides.pop(verify_api_key, None)
    api.dependency_overrides.pop(get_skrift_db_session, None)


def _body(**over):
    body = {
        "guild_id": "G1",
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
    hid = data["handler_id"]

    listed = await client.get("/admin/handlers", params={"guild_id": "G1"})
    assert len(listed.json()) == 1

    deleted = await client.delete(f"/admin/handlers/{hid}")
    assert deleted.status_code == 200
    assert (await client.get("/admin/handlers", params={"guild_id": "G1"})).json() == []


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
