"""Tests for the privileged-routines admin API and its isolation."""

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
    import smarter_dev.web.api.routers.privileged_routines as pr

    async def _submit(payload, **kwargs):
        return None

    monkeypatch.setattr(pr, "worker_submit", _submit)

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


def _routine_body(**over):
    body = {
        "guild_id": "G1",
        "channel_id": "C1",
        "trigger_type": "timer",
        "settings": {"delay_seconds": 60},
        "action": {"kind": "ban", "target_user_id": "U1"},
        "created_by_admin": "ADMIN1",
    }
    body.update(over)
    return body


async def test_create_list_delete_routine(client):
    created = await client.post("/admin/routines", json=_routine_body())
    assert created.status_code == 201
    rid = created.json()["routine_id"]

    listed = await client.get("/admin/routines", params={"guild_id": "G1"})
    assert len(listed.json()) == 1

    deleted = await client.delete(f"/admin/routines/{rid}")
    assert deleted.status_code == 200
    assert (await client.get("/admin/routines", params={"guild_id": "G1"})).json() == []


async def test_malformed_action_rejected(client):
    resp = await client.post(
        "/admin/routines", json=_routine_body(action={"kind": "timeout"})
    )
    assert resp.status_code == 422


def test_chatbot_tools_cannot_touch_privileged_tier():
    # The chatbot's tool surface has no routine/privileged tool — isolation by
    # construction. The only path to privileged routines is the admin command.
    from smarter_dev.bot.agents.handler_tools import handler_tool_functions

    names = {f.__name__ for f in handler_tool_functions()}
    assert names == {"register_handler", "list_handlers", "delete_handler"}
    assert not any("routine" in n or "privileg" in n for n in names)
