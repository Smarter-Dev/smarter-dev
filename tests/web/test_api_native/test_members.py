"""Parity tests for the native (Litestar) member-management API (unit U3).

Assert the wire contract of the ported ``routers/members.py`` directly: the
``DELETE /api/guilds/{guild_id}/members/{user_id}`` happy path (200 +
``SuccessResponse`` with the exact message), that it removes the user's bytes
balance and squad membership while preserving transaction history, the idempotent
success on a user with no data, the plain ``{"detail": "Invalid guild ID"}`` 400
the legacy ``verify_guild_access`` produced, and the plain 500 the legacy handler
returned via ``security_utils.create_database_error`` on a
``DatabaseOperationError``. The path carries the final ``/api`` prefix and mirrors
exactly what the bot sends from ``smarter_dev/bot/client.py``.

These run against a real in-memory SQLite session injected into a Litestar app
via ``httpx.ASGITransport`` because the handler exercises the real
``GuildOperations.remove_user_data`` deletes. Auth guards are cleared for the app
build — auth parity is covered separately by ``test_auth.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base
from smarter_dev.web.api_native import members as members_module
from smarter_dev.web.api_native.members import MemberController
from smarter_dev.web.crud import DatabaseOperationError
from smarter_dev.web.models import (
    BytesBalance,
    BytesTransaction,
    Squad,
    SquadMembership,
)

_GUILD = "123456789012345678"
_USER = "987654321098765432"


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
    """Litestar app serving the member controller with guards cleared."""
    original_guards = list(members_module.BOT_API_GUARDS)
    members_module.BOT_API_GUARDS.clear()
    try:
        app = Litestar(
            route_handlers=[MemberController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: session, sync_to_thread=False)
            },
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as http_client:
            yield http_client
    finally:
        members_module.BOT_API_GUARDS[:] = original_guards


def _url(guild_id: str = _GUILD, user_id: str = _USER) -> str:
    return f"/api/guilds/{guild_id}/members/{user_id}"


async def test_delete_returns_success_response(client: AsyncClient):
    response = await client.request("DELETE", _url())

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["message"] == f"Cleaned up user {_USER} data in guild {_GUILD}"
    assert "timestamp" in body


async def test_delete_removes_balance_and_membership_keeps_history(
    client: AsyncClient, session
):
    squad_id = uuid4()
    session.add(Squad(id=squad_id, guild_id=_GUILD, role_id="777", name="S"))
    session.add(BytesBalance(guild_id=_GUILD, user_id=_USER, balance=100))
    session.add(SquadMembership(squad_id=squad_id, guild_id=_GUILD, user_id=_USER))
    session.add(
        BytesTransaction(
            guild_id=_GUILD,
            giver_id=_USER,
            giver_username="giver",
            receiver_id="111",
            receiver_username="receiver",
            amount=5,
        )
    )
    await session.commit()

    response = await client.request("DELETE", _url())
    assert response.status_code == 200

    balances = (
        await session.execute(
            select(BytesBalance).where(
                BytesBalance.guild_id == _GUILD, BytesBalance.user_id == _USER
            )
        )
    ).scalars().all()
    memberships = (
        await session.execute(
            select(SquadMembership).where(
                SquadMembership.guild_id == _GUILD,
                SquadMembership.user_id == _USER,
            )
        )
    ).scalars().all()
    transactions = (
        (await session.execute(select(BytesTransaction))).scalars().all()
    )
    assert balances == []
    assert memberships == []
    # Transaction history is preserved for audit integrity.
    assert len(transactions) == 1


async def test_delete_is_idempotent_for_unknown_user(client: AsyncClient):
    response = await client.request("DELETE", _url(user_id="222333444555666777"))
    assert response.status_code == 200
    assert response.json()["success"] is True


async def test_invalid_guild_id_is_400(client: AsyncClient):
    response = await client.request("DELETE", _url(guild_id="not-a-snowflake"))
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid guild ID"}


async def test_negative_guild_id_is_400(client: AsyncClient):
    response = await client.request("DELETE", _url(guild_id="-5"))
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid guild ID"}


async def test_database_error_is_plain_500(client: AsyncClient):
    failing_operations = Mock()
    failing_operations.remove_user_data = AsyncMock(
        side_effect=DatabaseOperationError("boom")
    )
    with patch(
        "smarter_dev.web.api_native.members.GuildOperations",
        return_value=failing_operations,
    ):
        response = await client.request("DELETE", _url())

    assert response.status_code == 500
    # Plain body — ``secure_database_error`` mirrors the legacy
    # ``security_utils.create_database_error`` verbose gate exactly: the detailed
    # string is exposed here because the test settings enable verbose errors in
    # development (a real prod deploy would answer the generic "Internal server
    # error"). Either way the body shape is a bare ``{"detail": ...}``.
    assert response.json() == {"detail": "Database error: boom"}
