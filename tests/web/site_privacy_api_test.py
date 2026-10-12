"""Site service opt-out contract and principal boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import create_test_client
from skrift.auth.services import assign_role_to_user
from skrift.auth.services import invalidate_user_permissions_cache
from skrift.auth.services import sync_roles_to_database
from skrift.db.base import Base as SkriftBase
from skrift.db.models.user import User as SkriftUser
from skrift.db.services import api_key_service
from sqlalchemy import select

import smarter_dev.web.roles  # noqa: F401 — registers the site-service role
from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native.site_privacy import SitePrivacyController
from smarter_dev.web.chat_bot_opt_out import read_opt_out
from smarter_dev.web.chat_bot_purge import STATUS_QUEUED
from smarter_dev.web.models import ChatBotBlockedUser
from smarter_dev.web.models import ChatBotPurgeRequest

_USER = "111111111111111111"
_PATH = f"/api/site/privacy/opt-out/{_USER}"


@pytest.fixture
def client(test_engine, monkeypatch):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def session():
        async with maker() as db:
            yield db

    monkeypatch.setattr(site_guards, "bot_api_auth_guard", AsyncMock())
    key = SimpleNamespace(
        principal_type="service",
        service_name="site",
        scoped_permission_list=["site-privacy"],
    )
    monkeypatch.setattr(
        site_guards, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    with create_test_client(
        route_handlers=[SitePrivacyController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(session)},
    ) as test_client:
        yield test_client, key


def test_site_can_read_opt_out_and_opt_back_in(client):
    test_client, _ = client
    initial = test_client.get(_PATH)
    assert initial.status_code == 200
    assert initial.json() == {"opted_out": False, "since": None, "source": None}

    enabled = test_client.post(_PATH)
    assert enabled.status_code == 200
    assert enabled.json()["opted_out"] is True
    assert enabled.json()["source"] == "opt_out"
    assert enabled.json()["since"] is not None
    assert test_client.get(_PATH).json() == enabled.json()

    disabled = test_client.delete(_PATH)
    assert disabled.status_code == 200
    assert disabled.json() == initial.json()


async def test_writes_bump_revision_only_when_membership_changes(client, db_session):
    test_client, _ = client
    assert (await read_opt_out(db_session, _USER)).revision == 0
    test_client.post(_PATH)
    assert (await read_opt_out(db_session, _USER)).revision == 1
    test_client.post(_PATH)
    assert (await read_opt_out(db_session, _USER)).revision == 1
    test_client.delete(_PATH)
    assert (await read_opt_out(db_session, _USER)).revision == 2
    test_client.delete(_PATH)
    assert (await read_opt_out(db_session, _USER)).revision == 2


@pytest.mark.parametrize(
    ("principal_type", "service_name", "scopes"),
    [
        ("user", None, ["site-privacy"]),
        ("service", "bot", ["site-privacy"]),
        ("service", "site", ["site-memory"]),
    ],
    ids=["user-key", "bot-key", "wrong-scope"],
)
def test_only_explicitly_scoped_site_service_key_is_accepted(
    client, principal_type, service_name, scopes
):
    test_client, key = client
    key.principal_type = principal_type
    key.service_name = service_name
    key.scoped_permission_list = scopes
    for method in ("get", "post", "delete"):
        assert getattr(test_client, method)(_PATH).status_code == 401


async def test_writes_refused_while_purge_is_in_flight(client, db_session):
    test_client, _ = client
    db_session.add(
        ChatBotPurgeRequest(
            id=uuid4(),
            discord_user_id=_USER,
            names=[],
            status=STATUS_QUEUED,
            steps={},
        )
    )
    await db_session.commit()

    assert test_client.post(_PATH).status_code == 409
    assert test_client.delete(_PATH).status_code == 409
    assert test_client.get(_PATH).json() == {
        "opted_out": False,
        "since": None,
        "source": None,
    }
    assert (await read_opt_out(db_session, _USER)).revision == 0


@pytest.mark.parametrize("method", ["get", "post", "delete"])
@pytest.mark.parametrize(
    "bad_id",
    ["not-a-snowflake", "1234567890123456", "１２３４５６７８９０１２３４５６７"],
)
async def test_invalid_path_id_is_rejected_without_mutation(
    client, db_session, method, bad_id
):
    test_client, _ = client
    response = getattr(test_client, method)(f"/api/site/privacy/opt-out/{bad_id}")
    assert response.status_code == 400
    assert (await db_session.scalars(select(ChatBotBlockedUser))).all() == []
    assert (await read_opt_out(db_session, _USER)).revision == 0


async def test_real_skrift_site_key_and_principal_boundary(test_engine):
    """Exercise role, key verification, scope, and the production guard chain."""
    async with test_engine.begin() as connection:
        await connection.run_sync(SkriftBase.metadata.create_all)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as session:
        owner = SkriftUser(
            email="site-opt-out@smarter.dev", name="site", is_active=True
        )
        session.add(owner)
        await session.commit()
        await sync_roles_to_database(session)
        assert await assign_role_to_user(session, owner.id, "site-service")
        await session.commit()

        _, site_key, _ = await api_key_service.create_api_key(
            session,
            owner.id,
            "site",
            principal_type="service",
            service_name="site",
            scoped_permissions=["site-privacy"],
        )
        _, bot_key, _ = await api_key_service.create_api_key(
            session,
            owner.id,
            "bot",
            principal_type="service",
            service_name="discord-bot",
            scoped_permissions=["site-privacy"],
        )
        _, unscoped_key, _ = await api_key_service.create_api_key(
            session,
            owner.id,
            "unscoped-site",
            principal_type="service",
            service_name="site",
        )

    async def provide_db_session():
        async with maker() as session:
            yield session

    invalidate_user_permissions_cache()
    with patch(
        "smarter_dev.web.api_native.auth.get_db_session_context",
        side_effect=lambda: maker(),
    ):
        with create_test_client(
            route_handlers=[SitePrivacyController],
            plugins=[PydanticPlugin()],
            dependencies={"db_session": Provide(provide_db_session)},
        ) as test_client:
            test_client.app.state.session_maker_class = maker

            def auth(key: str) -> dict[str, str]:
                return {"Authorization": f"Bearer {key}"}

            assert test_client.get(_PATH, headers=auth(site_key)).status_code == 200
            assert test_client.post(_PATH, headers=auth(site_key)).status_code == 200
            assert test_client.delete(_PATH, headers=auth(site_key)).status_code == 200
            assert test_client.get(_PATH, headers=auth(bot_key)).status_code == 401
            assert test_client.get(_PATH, headers=auth(unscoped_key)).status_code == 401
    invalidate_user_permissions_cache()
