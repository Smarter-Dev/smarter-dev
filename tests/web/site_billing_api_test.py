"""Site billing contract, launch gate, redirects and principal isolation."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
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
from skrift.db.models.user import User
from skrift.db.services import api_key_service

import smarter_dev.web.roles  # noqa: F401
from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.web.api_native import site_billing
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native.site_billing import SiteBillingController
from smarter_dev.web.models import SudoMembership

ROOT = "/api/site/billing"
OFFERING = {"id": "hacker", "role": "hacker", "price_id": "price-hacker"}
BODY = {
    "plan_id": "hacker",
    "success_url": "https://site.test/sudo/confirm",
    "cancel_url": "https://site.test/sudo/cancel",
}


@pytest.fixture
async def client(test_engine, monkeypatch):
    async with test_engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def session():
        async with maker() as db:
            yield db

    monkeypatch.setattr(site_guards, "bot_api_auth_guard", AsyncMock())
    key = SimpleNamespace(
        principal_type="service",
        service_name="site",
        scoped_permission_list=["site-billing"],
    )
    monkeypatch.setattr(
        site_guards, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    monkeypatch.setattr(
        site_billing,
        "get_settings",
        lambda: SimpleNamespace(site_base_url="https://site.test"),
    )
    monkeypatch.setattr(
        site_billing.feature_flags, "get_mode", AsyncMock(return_value="enabled")
    )
    monkeypatch.setattr(
        site_billing,
        "build_pricing_context",
        AsyncMock(return_value={"offerings": [OFFERING]}),
    )
    monkeypatch.setattr(
        site_billing,
        "create_checkout_session",
        AsyncMock(return_value="https://polar.sh/checkout/test"),
    )
    monkeypatch.setattr(
        site_billing,
        "create_portal_session",
        AsyncMock(return_value="https://polar.sh/portal/test"),
    )
    with create_test_client(
        route_handlers=[SiteBillingController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(session)},
    ) as tc:
        yield tc, key


@pytest.fixture
async def user(db_session):
    user = User(email="billing@example.test", name="Member", is_active=True)
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.mark.parametrize("mode", ["disabled", "admin_only"])
def test_public_catalog_and_checkout_do_not_inherit_service_owner_admin(client, mode):
    tc, _ = client
    site_billing.feature_flags.get_mode.return_value = mode
    assert tc.get(ROOT + "/catalog").json()["launched"] is False
    site_billing.build_pricing_context.assert_not_awaited()


def test_catalog_reuses_pricing_context(client):
    tc, _ = client
    response = tc.get(ROOT + "/catalog")
    assert response.json() == {"launched": True, "offerings": [OFFERING]}
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize(
    "principal,service,scopes",
    [
        ("user", None, ["site-billing"]),
        ("service", "bot", ["site-billing"]),
        ("service", "site", ["site-memory", "site-privacy"]),
        ("service", "site", []),
    ],
)
def test_all_endpoints_require_explicit_site_billing_scope(
    client, principal, service, scopes
):
    tc, key = client
    key.principal_type, key.service_name, key.scoped_permission_list = (
        principal,
        service,
        scopes,
    )
    path = f"{ROOT}/users/{uuid4()}"
    for response in (
        tc.get(ROOT + "/catalog"),
        tc.get(path + "/membership"),
        tc.post(path + "/checkout", json=BODY),
        tc.post(path + "/portal"),
    ):
        assert response.status_code == 401


async def test_no_membership_and_no_portal(client, user):
    tc, _ = client
    path = f"{ROOT}/users/{user.id}"
    assert tc.get(path + "/membership").json() == {
        "status": "none",
        "plan": None,
        "expires_at": None,
        "will_renew": None,
        "portal_available": False,
    }
    assert tc.post(path + "/portal").status_code == 404
    site_billing.create_portal_session.assert_not_awaited()


@pytest.mark.parametrize(
    "days,source,revoked,expected",
    [
        (30, "subscription", None, "active"),
        (-1, "subscription", None, "expired"),
        (30, "comp", None, "active"),
        (30, "subscription", "refund", "none"),
    ],
)
async def test_membership_and_portal(
    client, user, db_session, days, source, revoked, expected
):
    tc, _ = client
    db_session.add(
        SudoMembership(
            user_id=user.id,
            role="hacker",
            source=source,
            customer_id="customer-one",
            checkout_id=str(uuid4()),
            price_id="price-hacker",
            amount_paid_cents=800,
            expires_at=datetime.now(UTC) + timedelta(days=days),
            will_renew=True,
            revoked_reason=revoked,
        )
    )
    await db_session.commit()
    path = f"{ROOT}/users/{user.id}"
    state = tc.get(path + "/membership").json()
    assert state["status"] == expected
    available = source != "comp" and revoked is None
    assert state["portal_available"] is available
    response = tc.post(path + "/portal")
    assert response.status_code == (200 if available else 404)
    if available:
        site_billing.create_portal_session.assert_awaited_once_with("customer-one")


@pytest.mark.parametrize("plan", ["hacker", "price-hacker"])
async def test_checkout_uses_catalog_and_stored_user(client, user, plan):
    tc, _ = client
    response = tc.post(
        f"{ROOT}/users/{user.id}/checkout", json={**BODY, "plan_id": plan}
    )
    assert response.status_code == 200
    assert response.json()["url"] == "https://polar.sh/checkout/test"
    args, kwargs = site_billing.create_checkout_session.await_args
    assert args[1].id == user.id
    assert kwargs == {
        "role": "hacker",
        "success_url": BODY["success_url"],
        "cancel_url": BODY["cancel_url"],
    }


@pytest.mark.parametrize("field", ["success_url", "cancel_url"])
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/x",
        "//site.test/x",
        "http://site.test/x",
        "https://site.test:444/x",
        "https://site.test.evil.test/x",
        "https://site.test@evil.test/x",
        "https://user@site.test/x",
        "https://site.test\\@evil.test/x",
        "https://site.test/\nx",
        "https://site.test:bad/x",
    ],
)
async def test_checkout_rejects_unsafe_redirects(client, user, field, url):
    tc, _ = client
    assert (
        tc.post(
            f"{ROOT}/users/{user.id}/checkout", json={**BODY, field: url}
        ).status_code
        == 400
    )
    site_billing.create_checkout_session.assert_not_awaited()


async def test_closed_flag_unknown_plan_and_inactive_user(client, user, db_session):
    tc, _ = client
    path = f"{ROOT}/users/{user.id}/checkout"
    site_billing.feature_flags.get_mode.return_value = "disabled"
    assert tc.post(path, json=BODY).status_code == 404
    site_billing.feature_flags.get_mode.return_value = "enabled"
    assert tc.post(path, json={**BODY, "plan_id": "bogus"}).status_code == 400
    user.is_active = False
    await db_session.commit()
    assert tc.post(path, json=BODY).status_code == 404
    site_billing.create_checkout_session.assert_not_awaited()


async def test_provider_failure_is_generic(client, user):
    tc, _ = client
    site_billing.create_checkout_session.side_effect = RuntimeError(
        "secret provider body"
    )
    response = tc.post(f"{ROOT}/users/{user.id}/checkout", json=BODY)
    assert response.status_code == 503
    assert response.json() == {"detail": "Billing unavailable"}
    site_billing.build_pricing_context.side_effect = RuntimeError("secret")
    assert tc.get(ROOT + "/catalog").status_code == 503


@pytest.mark.parametrize("uid,status", [("invalid", 400), (str(uuid4()), 404)])
def test_missing_or_invalid_user(client, uid, status):
    tc, _ = client
    path = f"{ROOT}/users/{uid}"
    assert tc.get(path + "/membership").status_code == status
    assert tc.post(path + "/checkout", json=BODY).status_code == status
    assert tc.post(path + "/portal").status_code == status


async def test_real_skrift_key_requires_billing_permission(test_engine, monkeypatch):
    async with test_engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        owner = User(email="service@example.test", name="Site", is_active=True)
        db.add(owner)
        await db.commit()
        await sync_roles_to_database(db)
        assert await assign_role_to_user(db, owner.id, "site-service")
        await db.commit()
        keys = []
        for scopes in (["site-billing"], ["site-memory", "site-privacy"]):
            _, raw, _ = await api_key_service.create_api_key(
                db,
                owner.id,
                "site",
                principal_type="service",
                service_name="site",
                scoped_permissions=scopes,
            )
            keys.append(raw)

    async def session():
        async with maker() as db:
            yield db

    monkeypatch.setattr(
        site_billing.feature_flags, "get_mode", AsyncMock(return_value="disabled")
    )
    invalidate_user_permissions_cache()
    with patch(
        "smarter_dev.web.api_native.auth.get_db_session_context",
        side_effect=lambda: maker(),
    ):
        with create_test_client(
            route_handlers=[SiteBillingController],
            plugins=[PydanticPlugin()],
            dependencies={"db_session": Provide(session)},
        ) as tc:
            tc.app.state.session_maker_class = maker
            for raw, status in zip(keys, (200, 401)):
                assert (
                    tc.get(
                        ROOT + "/catalog", headers={"Authorization": f"Bearer {raw}"}
                    ).status_code
                    == status
                )
            assert tc.get(ROOT + "/catalog").status_code == 401
    invalidate_user_permissions_cache()


@pytest.mark.parametrize("cancel_url", [None, "https://site.test/sudo/cancel"])
async def test_shared_checkout_preserves_legacy_payload_and_supports_back_button(
    monkeypatch, cancel_url
):
    from smarter_dev.web.billing import checkout

    create = AsyncMock(return_value=SimpleNamespace(url="https://polar.sh/test"))
    polar = SimpleNamespace(checkouts=SimpleNamespace(create_async=create))
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def client():
        yield polar

    monkeypatch.setattr(checkout, "get_polar", client)
    monkeypatch.setattr(
        checkout.catalog,
        "get_offerings",
        AsyncMock(return_value=[{"role": "hacker", "product_id": "product"}]),
    )
    await checkout.create_checkout_session(
        None,
        SimpleNamespace(id="user", email="member@example.test"),
        role="hacker",
        success_url="https://site.test/sudo/confirm",
        cancel_url=cancel_url,
    )
    payload = create.await_args.kwargs["request"]
    assert payload["products"] == ["product"]
    if cancel_url is None:
        assert "return_url" not in payload
    else:
        assert payload["return_url"] == cancel_url


@pytest.mark.parametrize("status", [401, 400, 404, 422, 503, 500])
def test_billing_error_responses_are_private_no_store(client, status):
    tc, key = client
    if status == 401:
        key.scoped_permission_list = ["site-memory"]
        response = tc.get(ROOT + "/catalog")
    elif status == 400:
        response = tc.get(ROOT + "/users/invalid/membership")
    elif status == 404:
        response = tc.get(f"{ROOT}/users/{uuid4()}/membership")
    elif status == 422:
        response = tc.post(f"{ROOT}/users/{uuid4()}/checkout", json={})
    elif status == 503:
        site_billing.build_pricing_context.side_effect = RuntimeError(
            "private provider diagnostic"
        )
        response = tc.get(ROOT + "/catalog")
        assert response.json() == {"detail": "Billing unavailable"}
    else:
        site_billing.feature_flags.get_mode.side_effect = RuntimeError(
            "private database diagnostic"
        )
        response = tc.get(ROOT + "/catalog")
        assert response.json() == {
            "status_code": 500,
            "detail": "Internal Server Error",
        }
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "private, no-store"
    if status == 401:
        assert response.json() == {
            "status_code": 401,
            "detail": "Site service key required",
        }
    elif status == 400:
        assert response.json() == {"detail": "Invalid account ID"}
    elif status == 404:
        assert response.json() == {"detail": "Account not found"}
    elif status == 422:
        assert response.json()["detail"] == "Request validation failed"
        assert response.json()["type"] == "validation_error"
        assert response.json()["errors"]
    assert "diagnostic" not in response.text
