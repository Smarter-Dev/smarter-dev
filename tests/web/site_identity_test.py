"""Identity ownership, provenance, revision and deletion ordering contracts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import create_test_client
from skrift.app_factory import EXCEPTION_HANDLERS
from skrift.db.base import Base as SkriftBase
from skrift.db.models.oauth_account import OAuthAccount
from skrift.db.models.user import User
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from smarter_dev.web import site_identity as service
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native import site_identity
from smarter_dev.web.api_native import site_privacy
from smarter_dev.web.api_native.errors import BotApiException
from smarter_dev.web.site_identity_models import SiteIdentityFence
from smarter_dev.web.site_identity_models import SiteIdentityState

KEY = "test-only-identity-key-not-a-real-secret"


def snapshot(revision=1, identities=None):
    return service.IdentitySnapshot(
        revision=revision,
        identities=identities
        if identities is not None
        else [
            {
                "provider": "discord",
                "subject": "123456789012345678",
                "email": "member@example.test",
                "email_verified": True,
            },
        ],
        display_name="Member",
    )


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(
        service, "get_settings", lambda: SimpleNamespace(site_identity_fence_key=KEY)
    )


@pytest.fixture
async def client(test_engine, monkeypatch):
    async with test_engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def session():
        async with maker() as db:
            yield db

    key = SimpleNamespace(
        principal_type="service",
        service_name="site",
        scoped_permission_list=["site-identity", "site-privacy"],
    )
    monkeypatch.setattr(site_guards, "bot_api_auth_guard", AsyncMock())
    monkeypatch.setattr(
        site_guards, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    monkeypatch.setattr(site_identity, "strip_discord_roles", AsyncMock())
    monkeypatch.setattr(site_privacy, "dispatch_one", AsyncMock())
    with create_test_client(
        route_handlers=[
            site_identity.SiteIdentityController,
            site_privacy.SitePrivacyController,
        ],
        plugins=[PydanticPlugin()],
        exception_handlers=EXCEPTION_HANDLERS,
        dependencies={"db_session": Provide(session)},
    ) as tc:
        yield tc, key


def put(client, uid, body=None):
    return client[0].put(
        f"/api/site/identities/{uid}", json=body or snapshot().model_dump()
    )


async def test_new_account_same_uuid_tokenless_and_repeat(client, db_session):
    uid = uuid4()
    first = put(client, uid)
    assert first.status_code == 201
    assert first.json() == {"user_id": str(uid), "revision": 1, "state": "ready"}
    assert first.headers["Cache-Control"] == "private, no-store"
    assert put(client, uid).status_code == 200
    user = await db_session.get(User, uid)
    assert user.email == "member@example.test" and user.is_active
    account = await db_session.scalar(
        select(OAuthAccount).where(OAuthAccount.user_id == uid)
    )
    assert account.access_token is None and account.refresh_token is None
    assert len((await db_session.scalars(select(User))).all()) == 1
    assert user.roles == []


async def test_unverified_email_not_promoted(client, db_session):
    body = snapshot().model_dump()
    body["identities"][0]["email_verified"] = False
    uid = uuid4()
    assert put(client, uid, body).status_code == 201
    assert (await db_session.get(User, uid)).email is None


@pytest.mark.parametrize("conflict", ["provider", "email", "uuid"])
async def test_conflicts_are_atomic(client, db_session, conflict):
    owner = User(
        email="member@example.test" if conflict == "email" else "other@example.test",
        is_active=True,
    )
    db_session.add(owner)
    await db_session.flush()
    if conflict == "provider":
        db_session.add(
            OAuthAccount(
                user_id=owner.id,
                provider="discord",
                provider_account_id="123456789012345678",
            )
        )
    await db_session.commit()
    uid = owner.id if conflict == "uuid" else uuid4()
    assert put(client, uid).status_code == 409
    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(User)) == 1
    assert await db_session.get(SiteIdentityState, uid) is None


async def test_imported_binding_preserved_and_only_owned_links_removed(
    client, db_session
):
    uid = uuid4()
    user = User(id=uid, email="old@example.test", is_active=True)
    db_session.add(user)
    await db_session.flush()
    imported = OAuthAccount(
        user_id=uid,
        provider="github",
        provider_account_id="88",
        access_token="leave-existing-token",
    )
    db_session.add(imported)
    await db_session.commit()
    identities = [
        {"provider": "github", "subject": "88"},
        *snapshot().model_dump()["identities"],
    ]
    assert put(client, uid, snapshot(1, identities).model_dump()).status_code == 200
    assert put(client, uid, snapshot(2, []).model_dump()).status_code == 200
    db_session.expire_all()
    remaining = list(
        (
            await db_session.scalars(
                select(OAuthAccount).where(OAuthAccount.user_id == uid)
            )
        ).all()
    )
    assert len(remaining) == 1 and remaining[0].access_token == "leave-existing-token"
    assert (await db_session.get(User, uid)).email == "old@example.test"
    site_identity.strip_discord_roles.assert_awaited_once_with(
        "123456789012345678", strict=True
    )


async def test_cookie_only_existing_user_cannot_bootstrap_provider_ownership(
    client, db_session
):
    uid = uuid4()
    db_session.add(User(id=uid, is_active=True))
    await db_session.commit()
    assert put(client, uid, snapshot(1, []).model_dump()).status_code == 200
    assert put(client, uid).status_code == 409
    assert put(client, uuid4(), snapshot(1, []).model_dump()).status_code == 409


async def test_revisions_and_changed_replay_fail_closed(client):
    uid = uuid4()
    assert put(client, uid).status_code == 201
    assert put(client, uid, snapshot(3).model_dump()).status_code == 200
    assert put(client, uid, snapshot(2).model_dump()).status_code == 409
    body = snapshot(3).model_dump()
    body["display_name"] = "Changed"
    assert put(client, uid, body).status_code == 409


async def test_inactive_user_never_reactivated(client, db_session):
    uid = uuid4()
    db_session.add(User(id=uid, is_active=False))
    await db_session.commit()
    assert put(client, uid).status_code == 410
    assert not (await db_session.get(User, uid)).is_active


@pytest.mark.parametrize("create_first", [False, True])
async def test_deletion_before_or_after_creation_blocks_all_replays(
    client, db_session, create_first
):
    uid = uuid4()
    if create_first:
        assert put(client, uid).status_code == 201
    result = client[0].post(f"/api/site/privacy/delete-account/{uid}")
    assert result.status_code == (202 if create_first else 404)
    # Simulate the final purge and anonymized receipt: fence must outlive User.
    await db_session.execute(delete(OAuthAccount).where(OAuthAccount.user_id == uid))
    await db_session.execute(delete(User).where(User.id == uid))
    await db_session.commit()
    assert put(client, uid, snapshot(99).model_dump()).status_code == 410
    fence = await db_session.get(SiteIdentityFence, service.fence_digest(uid))
    assert fence.deleted
    assert await db_session.get(SiteIdentityState, uid) is None
    assert set(SiteIdentityFence.__table__.columns.keys()) == {
        "digest",
        "deleted",
        "created_at",
        "updated_at",
    }
    assert str(uid) not in fence.digest


async def test_native_deletion_fence_helper_blocks_retry(client, db_session):
    uid = uuid4()
    assert put(client, uid).status_code == 201
    await service.fence_account_deletion(db_session, uid)
    await db_session.commit()
    assert put(client, uid).status_code == 410


@pytest.mark.parametrize(
    "scopes,principal,name",
    [
        ([], "service", "site"),
        (["site-billing"], "service", "site"),
        (["site-identity"], "user", None),
        (["site-identity"], "service", "bot"),
    ],
)
def test_service_scope_is_explicit(client, scopes, principal, name):
    key = client[1]
    key.scoped_permission_list, key.principal_type, key.service_name = (
        scopes,
        principal,
        name,
    )
    response = put(client, uuid4())
    assert response.status_code == 401
    assert response.json()["status_code"] == 401
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize(
    "body",
    [
        {"revision": 0, "identities": []},
        {"revision": True, "identities": []},
        {"revision": 1, "identities": [], "roles": ["admin"]},
        {"revision": 1, "identities": [{"provider": "evil", "subject": "1"}]},
        {"revision": 1, "identities": [{"provider": "discord", "subject": "../1"}]},
    ],
)
def test_strict_contract(client, body):
    result = put(client, uuid4(), body)
    assert result.status_code == 422
    assert result.headers["Cache-Control"] == "private, no-store"


def test_missing_key_fails_closed(client, monkeypatch):
    monkeypatch.setattr(
        service, "get_settings", lambda: SimpleNamespace(site_identity_fence_key=None)
    )
    assert put(client, uuid4()).status_code == 503


async def test_concurrent_same_uuid_and_deletion_serialize(test_engine, client):
    # Independent DB sessions exercise absent-row insertion arbitration (SQLite
    # here; the same INSERT + FOR UPDATE is used in PostgreSQL).
    engine = create_async_engine(test_engine.url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid4()

    async def ensure():
        async with maker() as db:
            try:
                await service.provision_identity(db, uid, snapshot())
                await db.commit()
            except BotApiException as error:
                assert error.status_code == 410

    async def remove():
        async with maker() as db:
            await service.fence_account_deletion(db, uid)
            await db.commit()

    await asyncio.gather(ensure(), ensure(), remove())
    async with maker() as db:
        with pytest.raises(BotApiException) as error:
            await service.provision_identity(db, uid, snapshot(2))
        assert error.value.status_code == 410

    await engine.dispose()


async def test_mismatched_key_cannot_reprovision_or_delete(client, monkeypatch):
    uid = uuid4()
    assert put(client, uid).status_code == 201
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(
            site_identity_fence_key="other-test-key-with-at-least-32-bytes"
        ),
    )
    assert put(client, uid).status_code == 503
    assert (
        client[0].post(f"/api/site/privacy/delete-account/{uuid4()}").status_code == 503
    )


def test_missing_key_cannot_delete_unknown_uuid(client, monkeypatch):
    monkeypatch.setattr(
        service, "get_settings", lambda: SimpleNamespace(site_identity_fence_key=None)
    )
    assert (
        client[0].post(f"/api/site/privacy/delete-account/{uuid4()}").status_code == 503
    )


async def test_unlink_failure_preserves_binding_and_revision_for_retry(
    client, db_session
):
    uid = uuid4()
    assert put(client, uid).status_code == 201
    site_identity.strip_discord_roles.side_effect = RuntimeError(
        "private provider error"
    )
    assert put(client, uid, snapshot(2, []).model_dump()).status_code == 503
    state = await db_session.get(SiteIdentityState, uid)
    assert state.revision == 1
    assert (
        await db_session.scalar(
            select(OAuthAccount.id).where(OAuthAccount.user_id == uid)
        )
        is not None
    )
    await db_session.commit()
    site_identity.strip_discord_roles.side_effect = None
    assert put(client, uid, snapshot(2, []).model_dump()).status_code == 200
    assert put(client, uid, snapshot(2, []).model_dump()).status_code == 200


async def test_exact_retry_detects_binding_removed_outside_handoff(client, db_session):
    uid = uuid4()
    assert put(client, uid).status_code == 201
    await db_session.execute(delete(OAuthAccount).where(OAuthAccount.user_id == uid))
    await db_session.commit()
    assert put(client, uid).status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(OAuthAccount)) == 0


async def test_native_account_delete_commits_fence(client, db_session, monkeypatch):
    from smarter_dev.web import account_controller

    uid = uuid4()
    assert put(client, uid).status_code == 201
    user = await db_session.get(User, uid)
    monkeypatch.setattr(
        account_controller, "_current_user", AsyncMock(return_value=user)
    )
    monkeypatch.setattr(account_controller, "verify_csrf", AsyncMock(return_value=True))
    monkeypatch.setattr(account_controller, "dispatch_one", AsyncMock())
    request = SimpleNamespace(
        form=AsyncMock(return_value={"confirmation": "DELETE"}),
        session={"user_id": str(uid)},
    )
    await account_controller.AccountController.delete_account.fn(
        object.__new__(account_controller.AccountController), request, db_session
    )
    assert request.session == {}
    assert put(client, uid).status_code == 410
    db_session.expire_all()
    assert not (await db_session.get(User, uid)).is_active


async def test_operator_can_pin_key_without_identity_marker(db_session):
    await service.verify_identity_key(db_session)
    await db_session.commit()
    assert (
        await db_session.scalar(select(func.count()).select_from(SiteIdentityFence))
        == 0
    )


@pytest.mark.parametrize("failure", ["config", "token", "fetch", "remove"])
async def test_strict_unlink_projection_failure_is_reported(monkeypatch, failure):
    import httpx

    from smarter_dev.web.billing import converge as projection

    monkeypatch.setattr(
        projection.catalog,
        "get_discord_config",
        AsyncMock(
            return_value=None
            if failure == "config"
            else {"guild_id": "1", "base_role_id": "2"}
        ),
    )
    monkeypatch.setattr(
        projection,
        "_bot_token",
        lambda: None if failure == "token" else "test-only-token",
    )
    fetch = AsyncMock(return_value={"2"})
    remove = AsyncMock()
    if failure == "fetch":
        fetch.side_effect = httpx.ConnectError("unavailable")
    if failure == "remove":
        remove.side_effect = httpx.ConnectError("unavailable")
    monkeypatch.setattr(projection, "_fetch_member_roles", fetch)
    monkeypatch.setattr(projection, "_remove_role", remove)
    with pytest.raises((RuntimeError, httpx.HTTPError)):
        await projection.strip_discord_roles("123456789012345678", strict=True)
    # Existing non-strict callers keep their best-effort behavior.
    assert await projection.strip_discord_roles("123456789012345678") == {"removed": []}


async def test_missing_catalog_unlink_keeps_owned_binding_and_retry_state(
    client, db_session, monkeypatch
):
    from smarter_dev.web.billing import converge as projection

    uid = uuid4()
    assert put(client, uid).status_code == 201
    state = await db_session.get(SiteIdentityState, uid)
    owned = list(state.owned_accounts)
    fingerprint = state.fingerprint
    await db_session.commit()
    monkeypatch.setattr(
        site_identity, "strip_discord_roles", projection.strip_discord_roles
    )
    config = AsyncMock(return_value=None)
    monkeypatch.setattr(projection.catalog, "get_discord_config", config)
    monkeypatch.setattr(projection, "_bot_token", lambda: "test-only-token")
    monkeypatch.setattr(projection, "_fetch_member_roles", AsyncMock(return_value=None))
    result = put(client, uid, snapshot(2, []).model_dump())
    assert result.status_code == 503
    db_session.expire_all()
    state = await db_session.get(SiteIdentityState, uid)
    assert state.revision == 1
    assert state.fingerprint == fingerprint
    assert state.owned_accounts == owned
    assert (
        str(
            await db_session.scalar(
                select(OAuthAccount.id).where(OAuthAccount.user_id == uid)
            )
        )
        == owned[0]
    )
    await db_session.commit()
    config.return_value = {"guild_id": "1", "base_role_id": "2"}
    assert put(client, uid, snapshot(2, []).model_dump()).status_code == 200
    db_session.expire_all()
    state = await db_session.get(SiteIdentityState, uid)
    assert state.revision == 2 and state.owned_accounts == []
    assert (
        await db_session.scalar(
            select(OAuthAccount.id).where(OAuthAccount.user_id == uid)
        )
        is None
    )


def test_malformed_json_uses_private_api_error_under_skrift_parent_handlers(client):
    response = client[0].put(
        f"/api/site/identities/{uuid4()}",
        content="{broken",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json()["status_code"] == 400
