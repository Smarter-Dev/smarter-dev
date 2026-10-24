"""The in-place scope grant for the site key (docs/site-code.md)."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from skrift.auth.services import UserPermissions
from skrift.db.base import Base as SkriftBase
from skrift.db.models.api_key import APIKey
from skrift.db.models.user import User
from skrift.db.services.api_key_service import create_api_key
from skrift.db.services.api_key_service import verify_api_key
from sqlalchemy import select

from scripts import grant_site_key_scope as grant_script

SCOPES = ["site-billing", "site-identity", "site-memory", "site-privacy", "site-search"]


@pytest.fixture
async def session(test_engine, db_session, monkeypatch):
    async with test_engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
    owner_permissions = {"site-code", *SCOPES}
    monkeypatch.setattr(
        grant_script,
        "get_user_permissions",
        AsyncMock(
            side_effect=lambda _session, user_id: UserPermissions(
                user_id=user_id, permissions=set(owner_permissions)
            )
        ),
    )
    db_session.owner_permissions = owner_permissions
    return db_session


async def _site_key(session, scopes=SCOPES, *, service="site", active=True):
    owner = User(email=f"{uuid4()}@example.test", name="site", is_active=True)
    session.add(owner)
    await session.commit()
    key, raw, _ = await create_api_key(
        session,
        owner.id,
        "site",
        principal_type="service",
        service_name=service,
        scoped_permissions=list(scopes),
    )
    key.is_active = active
    await session.commit()
    return key, raw


async def test_adds_the_scope_to_the_same_key(session):
    key, raw = await _site_key(session)
    said = await grant_script.grant(session, "site-code")
    await session.refresh(key)
    assert key.scoped_permission_list == sorted([*SCOPES, "site-code"])
    assert said == f"{key.key_prefix}: {sorted([*SCOPES, 'site-code'])}"
    assert raw not in said
    # Not re-minted: the Worker's key still verifies, as the same key.
    assert (await verify_api_key(session, raw)).id == key.id


async def test_a_second_run_changes_nothing(session):
    key, _ = await _site_key(session, [*SCOPES, "site-code"])
    said = await grant_script.grant(session, "site-code")
    assert said == f"{key.key_prefix}: site-code already granted"
    await session.refresh(key)
    assert key.scoped_permission_list == [*SCOPES, "site-code"]


async def test_refuses_before_the_owner_holds_the_permission(session):
    key, _ = await _site_key(session)
    session.owner_permissions.discard("site-code")
    with pytest.raises(RuntimeError, match="deploy first"):
        await grant_script.grant(session, "site-code")
    await session.refresh(key)
    assert "site-code" not in key.scoped_permission_list


@pytest.mark.parametrize("keys", [0, 2])
async def test_refuses_unless_exactly_one_active_site_key(session, keys):
    for _ in range(keys):
        await _site_key(session)
    # Neither a bot key nor a revoked site key counts.
    await _site_key(session, service="bot")
    await _site_key(session, active=False)
    with pytest.raises(RuntimeError, match=f"found {keys}"):
        await grant_script.grant(session, "site-code")
    rows = (await session.scalars(select(APIKey))).all()
    assert all("site-code" not in (row.scoped_permissions or "") for row in rows)
