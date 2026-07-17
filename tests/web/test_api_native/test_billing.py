"""Parity tests for the native billing controllers (unit U10 — money path).

Covers the two billing-sensitive endpoints ported from the legacy FastAPI
routers ``polar_webhooks.py`` and ``sudo_converge.py``:

- ``POST /api/polar-webhooks/events`` — standard-webhooks signature auth (NO
  API key), idempotent dispatch keyed on the ``webhook-id`` delivery id.
- ``POST /api/sudo/converge`` — API-key authenticated bot trigger for sudo
  role re-projection.

Signature verification is exercised with REAL crypto (``standardwebhooks``
signing + ``polar_sdk.webhooks.validate_event``) for the rejection paths and
for a correctly-signed delivery, pinning the exact
``(payload_bytes, lowercased_headers, secret)`` plumbing the legacy router
used. Post-verification behavior (dedupe, dispatch, commit ordering) is tested
with ``validate_event`` patched, mirroring how the billing dispatch suite
(``tests/web/test_sudo_billing.py``) drives dict-shaped events.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient, create_test_client
from sqlalchemy.exc import IntegrityError
from standardwebhooks.webhooks import Webhook

from smarter_dev.web.api_native import billing as billing_module
from smarter_dev.web.api_native.billing import (
    PolarWebhookController,
    SudoConvergeController,
)
from smarter_dev.web.models import WebhookEventProcessed

_WEBHOOK_SECRET = "whsec-test-secret"
_EVENTS_PATH = "/api/polar-webhooks/events"
_CONVERGE_PATH = "/api/sudo/converge"


def _signed_headers(payload: str, secret: str, message_id: str = "msg_1") -> dict[str, str]:
    """Produce valid standard-webhooks headers for ``payload`` under ``secret``.

    Mirrors Polar's delivery signing: ``polar_sdk.webhooks.validate_event``
    base64-encodes the raw secret before handing it to ``standardwebhooks``.
    """
    signer = Webhook(base64.b64encode(secret.encode()).decode())
    timestamp = datetime.now(timezone.utc)
    signature = signer.sign(message_id, timestamp, payload)
    return {
        "webhook-id": message_id,
        "webhook-timestamp": str(int(timestamp.timestamp())),
        "webhook-signature": signature,
    }


@pytest.fixture
def polar_settings() -> Iterator[Mock]:
    """Patch module settings with a configured webhook secret."""
    with patch("smarter_dev.web.api_native.billing.get_settings") as get_settings_mock:
        settings = Mock()
        settings.polar_webhook_secret = _WEBHOOK_SECRET
        get_settings_mock.return_value = settings
        yield settings


@pytest.fixture
def polar_client(session_mock: AsyncMock) -> Iterator[TestClient]:
    """Client serving the polar-webhook controller (no guards on the route).

    ``raise_server_exceptions=False`` so unhandled dispatch/validation errors
    surface as the 500 response Polar would see (its retry trigger) instead of
    being re-raised into the test.
    """
    with create_test_client(
        route_handlers=[PolarWebhookController],
        plugins=[PydanticPlugin()],
        dependencies={
            "db_session": Provide(lambda: session_mock, sync_to_thread=False)
        },
        raise_server_exceptions=False,
    ) as client:
        yield client


@pytest.fixture
def validate_event_mock() -> Iterator[Mock]:
    """Patch signature verification to hand back a dict-shaped Polar event."""
    with patch("smarter_dev.web.api_native.billing.validate_event") as verifier:
        verifier.return_value = {"type": "order.paid", "data": {"id": "order_1"}}
        yield verifier


@pytest.fixture
def dispatch_mock() -> Iterator[AsyncMock]:
    """Patch the billing dispatch side-effect handler."""
    with patch(
        "smarter_dev.web.api_native.billing.billing_webhooks.dispatch",
        new=AsyncMock(),
    ) as dispatcher:
        yield dispatcher


# --------------------------------------------------------------------------- #
# Polar webhook — configuration + signature verification (real crypto)
# --------------------------------------------------------------------------- #


def test_polar_unconfigured_secret_returns_503(
    polar_client: TestClient, polar_settings: Mock
):
    polar_settings.polar_webhook_secret = None

    response = polar_client.post(_EVENTS_PATH, content=b"{}")

    assert response.status_code == 503
    assert response.json() == {"detail": "Polar webhooks are not configured."}


def test_polar_missing_signature_headers_returns_403(
    polar_client: TestClient, polar_settings: Mock, session_mock: AsyncMock
):
    """No Authorization header is required — rejection is the SIGNATURE 403.

    Also proves the route carries no API-key guard: an unauthenticated request
    reaches signature verification instead of a 401.
    """
    response = polar_client.post(
        _EVENTS_PATH, content=json.dumps({"type": "order.paid"})
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid signature."}
    session_mock.add.assert_not_called()


def test_polar_wrong_secret_signature_returns_403(
    polar_client: TestClient, polar_settings: Mock
):
    payload = json.dumps({"type": "order.paid", "data": {}})
    forged_headers = _signed_headers(payload, "whsec-attacker-secret")

    response = polar_client.post(_EVENTS_PATH, content=payload, headers=forged_headers)

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid signature."}


def test_polar_tampered_payload_returns_403(
    polar_client: TestClient, polar_settings: Mock
):
    signed_headers = _signed_headers(
        json.dumps({"type": "order.paid", "data": {}}), _WEBHOOK_SECRET
    )
    tampered_payload = json.dumps({"type": "order.paid", "data": {"amount": 0}})

    response = polar_client.post(
        _EVENTS_PATH, content=tampered_payload, headers=signed_headers
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid signature."}


def test_polar_correctly_signed_unknown_type_bubbles_500(
    polar_client: TestClient, polar_settings: Mock, session_mock: AsyncMock
):
    """A valid signature over an unknown event type propagates as a 500.

    ``WebhookUnknownTypeError`` is NOT a ``WebhookVerificationError`` subclass,
    so the legacy router let it bubble (500 → Polar retries). Reaching this
    error proves real signature verification SUCCEEDED through the ported
    payload/header plumbing — a bad signature would have produced the 403.
    """
    payload = json.dumps({"type": "totally.unknown", "data": {}})
    signed_headers = _signed_headers(payload, _WEBHOOK_SECRET)

    response = polar_client.post(_EVENTS_PATH, content=payload, headers=signed_headers)

    assert response.status_code == 500
    session_mock.add.assert_not_called()
    session_mock.commit.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Polar webhook — dedupe + dispatch (verification patched)
# --------------------------------------------------------------------------- #


def test_polar_valid_event_dispatches_and_commits(
    polar_client: TestClient,
    polar_settings: Mock,
    session_mock: AsyncMock,
    validate_event_mock: Mock,
    dispatch_mock: AsyncMock,
):
    payload = json.dumps({"type": "order.paid", "data": {"id": "order_1"}})

    response = polar_client.post(
        _EVENTS_PATH, content=payload, headers={"webhook-id": "msg_1"}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    # Verification received the exact raw payload, lowercased headers, and
    # the configured secret — the legacy router's precise contract.
    verify_payload, verify_headers, verify_secret = validate_event_mock.call_args.args
    assert verify_payload == payload.encode()
    assert verify_headers["webhook-id"] == "msg_1"
    assert all(key == key.lower() for key in verify_headers)
    assert verify_secret == _WEBHOOK_SECRET

    # Dedupe row flushed before dispatch, then the request-level commit.
    recorded = session_mock.add.call_args.args[0]
    assert isinstance(recorded, WebhookEventProcessed)
    assert recorded.event_id == "msg_1"
    assert recorded.type == "order.paid"
    session_mock.flush.assert_awaited_once()
    dispatch_mock.assert_awaited_once_with(
        session_mock, validate_event_mock.return_value
    )
    session_mock.commit.assert_awaited_once()


def test_polar_duplicate_delivery_acknowledged_without_dispatch(
    polar_client: TestClient,
    polar_settings: Mock,
    session_mock: AsyncMock,
    validate_event_mock: Mock,
    dispatch_mock: AsyncMock,
):
    """At-least-once delivery: a replayed webhook-id NEVER re-runs the handler."""
    session_mock.flush.side_effect = IntegrityError(
        "duplicate key", None, Exception("unique violation")
    )

    response = polar_client.post(
        _EVENTS_PATH, content=b"{}", headers={"webhook-id": "msg_dup"}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "duplicate": "true"}
    session_mock.rollback.assert_awaited_once()
    dispatch_mock.assert_not_awaited()
    session_mock.commit.assert_not_awaited()


def test_polar_missing_webhook_id_skips_dedupe_but_dispatches(
    polar_client: TestClient,
    polar_settings: Mock,
    session_mock: AsyncMock,
    validate_event_mock: Mock,
    dispatch_mock: AsyncMock,
):
    response = polar_client.post(_EVENTS_PATH, content=b"{}")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    session_mock.add.assert_not_called()
    dispatch_mock.assert_awaited_once()
    session_mock.commit.assert_awaited_once()


def test_polar_dispatch_failure_returns_500_without_commit(
    polar_client: TestClient,
    polar_settings: Mock,
    session_mock: AsyncMock,
    validate_event_mock: Mock,
    dispatch_mock: AsyncMock,
):
    """A dispatch crash bubbles as 500 (Polar retries) and never commits the
    dedupe row — the retry must be allowed to attempt the handler again."""
    dispatch_mock.side_effect = RuntimeError("handler exploded")

    response = polar_client.post(
        _EVENTS_PATH, content=b"{}", headers={"webhook-id": "msg_boom"}
    )

    assert response.status_code == 500
    session_mock.commit.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Sudo converge — endpoint behavior (guards bypassed)
# --------------------------------------------------------------------------- #


@pytest.fixture
def converge_client(session_mock: AsyncMock) -> Iterator[TestClient]:
    """Client serving the sudo-converge controller with auth guards bypassed.

    The route shares the ``billing.BOT_API_GUARDS`` list by reference, so
    emptying it before the app is built removes the guards for these tests
    only. Auth is covered by the guarded tests below.
    """
    original_guards = list(billing_module.BOT_API_GUARDS)
    billing_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[SudoConvergeController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: session_mock, sync_to_thread=False)
            },
        ) as client:
            yield client
    finally:
        billing_module.BOT_API_GUARDS[:] = original_guards


@pytest.fixture
def converge_mock() -> Iterator[AsyncMock]:
    """Patch the billing ``converge`` projection in the controller module."""
    with patch(
        "smarter_dev.web.api_native.billing.converge", new=AsyncMock()
    ) as converge_stub:
        converge_stub.return_value = {"added": [], "removed": []}
        yield converge_stub


def test_converge_unlinked_user_returns_linked_false(
    converge_client: TestClient,
    session_mock: AsyncMock,
    converge_mock: AsyncMock,
    user_id: str,
):
    session_mock.execute.return_value = Mock(first=Mock(return_value=None))

    response = converge_client.post(
        _CONVERGE_PATH, json={"discord_user_id": user_id}
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": None,
        "added": [],
        "removed": [],
        "linked": False,
    }
    converge_mock.assert_not_awaited()
    lookup_params = session_mock.execute.call_args.args[1]
    assert lookup_params == {"did": user_id}


def test_converge_linked_user_returns_outcome_and_commits(
    converge_client: TestClient,
    session_mock: AsyncMock,
    converge_mock: AsyncMock,
    user_id: str,
):
    site_user_id = uuid4()
    session_mock.execute.return_value = Mock(
        first=Mock(return_value=(str(site_user_id),))
    )
    converge_mock.return_value = {"added": ["111"], "removed": ["222"]}

    response = converge_client.post(
        _CONVERGE_PATH, json={"discord_user_id": user_id}
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(site_user_id),
        "added": ["111"],
        "removed": ["222"],
        "linked": True,
    }
    converge_mock.assert_awaited_once_with(session_mock, site_user_id)
    session_mock.commit.assert_awaited_once()


def test_converge_failure_returns_500_converge_failed(
    converge_client: TestClient,
    session_mock: AsyncMock,
    converge_mock: AsyncMock,
    user_id: str,
):
    session_mock.execute.return_value = Mock(first=Mock(return_value=(str(uuid4()),)))
    converge_mock.side_effect = RuntimeError("entitlement lookup exploded")

    response = converge_client.post(
        _CONVERGE_PATH, json={"discord_user_id": user_id}
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "converge failed"}
    session_mock.commit.assert_not_awaited()


def test_converge_missing_discord_user_id_returns_422(
    converge_client: TestClient, session_mock: AsyncMock
):
    response = converge_client.post(_CONVERGE_PATH, json={})

    assert response.status_code == 422
    session_mock.execute.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Sudo converge — auth guards (real guards, parity with test_auth.py)
# --------------------------------------------------------------------------- #


@pytest.fixture
def guarded_converge_client() -> Iterator[TestClient]:
    """Client serving the sudo-converge controller with its real auth guards."""
    with create_test_client(
        route_handlers=[SudoConvergeController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def test_converge_missing_authorization_header_rejected(
    guarded_converge_client: TestClient, user_id: str
):
    response = guarded_converge_client.post(
        _CONVERGE_PATH, json={"discord_user_id": user_id}
    )
    assert response.status_code == 401


def test_converge_non_sk_bearer_rejected(
    guarded_converge_client: TestClient, user_id: str
):
    response = guarded_converge_client.post(
        _CONVERGE_PATH,
        json={"discord_user_id": user_id},
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401
