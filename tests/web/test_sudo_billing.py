"""Unit tests for the two-role sudo billing logic (Hacker / Founder).

These cover the pure, Polar-shaped logic without touching the database or the
live Polar API: catalog parsing, subscription period-end extraction, webhook
dispatch routing, and Checkout parameter construction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from smarter_dev.web.billing import catalog, checkout, webhooks


def _price(**kwargs) -> SimpleNamespace:
    """A Polar-price-shaped fake (fixed uses price_amount, custom uses minimum)."""
    kwargs.setdefault("is_archived", False)
    return SimpleNamespace(**kwargs)


def _product(**kwargs) -> SimpleNamespace:
    kwargs.setdefault("is_recurring", False)
    kwargs.setdefault("recurring_interval", None)
    kwargs.setdefault("description", "")
    return SimpleNamespace(**kwargs)


# ── catalog parsing ────────────────────────────────────────────────


def test_price_shape_fixed():
    assert catalog._price_shape(_price(price_amount=800)) == (800, 800, False)


def test_price_shape_custom_pay_what_you_want():
    price = _price(minimum_amount=25600, preset_amount=30000)
    assert catalog._price_shape(price) == (30000, 25600, True)


def test_price_shape_custom_defaults_preset_to_minimum():
    price = _price(minimum_amount=25600, preset_amount=None)
    assert catalog._price_shape(price) == (25600, 25600, True)


def test_select_price_skips_archived_and_free():
    prices = [
        _price(id="free"),  # no amount → unpriceable
        _price(id="archived", price_amount=800, is_archived=True),
        _price(id="live", price_amount=800),
    ]
    assert catalog._select_price(prices).id == "live"


def test_features_collects_numbered_keys_in_order():
    meta = {"feature_1": "second", "feature_0": "first", "feature_10": "third"}
    assert catalog._features(meta) == ["first", "second", "third"]


def test_offering_from_product_maps_hacker():
    product = _product(
        id="prod_h",
        name="Hacker",
        description="challenges",
        is_recurring=True,
        recurring_interval="month",
        prices=[_price(id="price_h", price_amount=800)],
        metadata={
            "sudo_role": "hacker",
            "order": "1",
            "hero": "false",
            "cta_label": "./join --hacker",
            "feature_0": "All RunHacks",
            "discord_guild_id": "g",
            "discord_base_role_id": "b",
            "discord_role_ids": "",
        },
    )
    offering = catalog._offering_from_product(product)
    assert offering["role"] == "hacker"
    assert offering["product_id"] == "prod_h"
    assert offering["price_id"] == "price_h"
    assert offering["price_cents"] == 800
    assert offering["recurring"] is True
    assert offering["interval"] == "month"
    assert offering["pay_what_you_want"] is False
    assert offering["feats"] == ["All RunHacks"]
    assert offering["discord_role_ids"] == []


def test_offering_from_product_maps_founder():
    product = _product(
        id="prod_f",
        name="Founder",
        description="fund",
        is_recurring=False,
        prices=[_price(id="price_f", minimum_amount=25600, preset_amount=25600)],
        metadata={
            "sudo_role": "founder",
            "order": "2",
            "hero": "true",
            "discord_role_ids": "r1,r2",
        },
    )
    offering = catalog._offering_from_product(product)
    assert offering["role"] == "founder"
    assert offering["recurring"] is False
    assert offering["pay_what_you_want"] is True
    assert offering["min_cents"] == 25600
    assert offering["hero"] is True
    assert offering["discord_role_ids"] == ["r1", "r2"]


def test_offering_from_product_ignores_non_sudo():
    product = _product(id="x", name="Unrelated", prices=[], metadata={})
    assert catalog._offering_from_product(product) is None


# ── subscription period end ────────────────────────────────────────


def test_subscription_period_end_datetime():
    end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert webhooks._subscription_period_end({"current_period_end": end}) == end


def test_subscription_period_end_naive_datetime_is_utc():
    naive = datetime(2026, 8, 1)
    got = webhooks._subscription_period_end({"current_period_end": naive})
    assert got == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_subscription_period_end_iso_string():
    got = webhooks._subscription_period_end({"current_period_end": "2026-09-01T00:00:00Z"})
    assert got == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_subscription_period_end_missing_defaults_now():
    before = datetime.now(tz=timezone.utc)
    assert webhooks._subscription_period_end({}) >= before


# ── dispatch routing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_routes_known_event(monkeypatch):
    seen = {}

    async def fake_handler(session, data):
        seen["data"] = data

    monkeypatch.setitem(webhooks._HANDLERS, "order.paid", fake_handler)
    await webhooks.dispatch(None, {"type": "order.paid", "data": {"x": 1}})
    assert seen["data"] == {"x": 1}


@pytest.mark.asyncio
async def test_dispatch_ignores_unknown_event():
    # Should simply no-op (and not raise) for an unmapped type.
    await webhooks.dispatch(None, {"type": "customer.updated", "data": {}})


def test_event_type_from_sdk_payload_uses_TYPE_attr():
    # Polar SDK payloads expose the discriminator as ``TYPE`` (alias ``type``),
    # NOT ``.type`` — regressing this silently drops every webhook.
    payload = SimpleNamespace(TYPE="order.paid", data={"id": "o1"})
    assert webhooks.event_type(payload) == "order.paid"


def test_event_type_from_dict():
    assert webhooks.event_type({"type": "order.paid"}) == "order.paid"


@pytest.mark.asyncio
async def test_dispatch_routes_sdk_style_payload(monkeypatch):
    # An object with a ``TYPE`` attr + ``data`` attr (the real SDK shape).
    seen = {}

    async def fake_handler(session, data):
        seen["data"] = data

    monkeypatch.setitem(webhooks._HANDLERS, "order.paid", fake_handler)
    payload = SimpleNamespace(TYPE="order.paid", data={"x": 1})
    await webhooks.dispatch(None, payload)
    assert seen["data"] == {"x": 1}


# ── order.paid billing_reason routing ──────────────────────────────


@pytest.mark.asyncio
async def test_order_paid_purchase_routes_to_one_time(monkeypatch):
    seen = {}

    async def fake_one_time(session, order):
        seen["kind"] = "one_time"

    async def fake_subscription(session, order):
        seen["kind"] = "subscription"

    monkeypatch.setattr(webhooks, "_fulfil_one_time", fake_one_time)
    monkeypatch.setattr(webhooks, "_fulfil_subscription", fake_subscription)
    await webhooks.handle_order_paid(None, {"billing_reason": "purchase", "id": "o1"})
    assert seen["kind"] == "one_time"


@pytest.mark.asyncio
async def test_order_paid_subscription_cycle_routes_to_subscription(monkeypatch):
    seen = {}

    async def fake_one_time(session, order):
        seen["kind"] = "one_time"

    async def fake_subscription(session, order):
        seen["kind"] = "subscription"

    monkeypatch.setattr(webhooks, "_fulfil_one_time", fake_one_time)
    monkeypatch.setattr(webhooks, "_fulfil_subscription", fake_subscription)
    await webhooks.handle_order_paid(
        None, {"billing_reason": "subscription_cycle", "id": "o2"}
    )
    assert seen["kind"] == "subscription"


# ── checkout param construction ────────────────────────────────────


class _FakeCheckouts:
    def __init__(self, box):
        self._box = box

    async def create_async(self, *, request):
        self._box["request"] = request
        return SimpleNamespace(url="https://polar.test/checkout/x")


class _FakePolar:
    def __init__(self, box):
        self.checkouts = _FakeCheckouts(box)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _fake_offerings():
    return [
        {"role": "hacker", "product_id": "prod_h", "price_id": "price_h"},
        {"role": "founder", "product_id": "prod_f", "price_id": "price_f"},
    ]


@pytest.mark.asyncio
async def test_checkout_builds_polar_request(monkeypatch):
    box: dict = {}
    monkeypatch.setattr(checkout.catalog, "get_offerings", _fake_offerings)
    monkeypatch.setattr(checkout, "get_polar", lambda: _FakePolar(box))
    user = SimpleNamespace(id="u1", email="a@b.c")

    url = await checkout.create_checkout_session(
        None, user, role="hacker", success_url="https://s/success"
    )

    request = box["request"]
    assert url == "https://polar.test/checkout/x"
    assert request["products"] == ["prod_h"]
    assert request["external_customer_id"] == "u1"
    assert request["customer_email"] == "a@b.c"
    assert request["metadata"] == {"role": "hacker", "user_id": "u1"}
    assert request["success_url"] == "https://s/success"


@pytest.mark.asyncio
async def test_checkout_founder_uses_founder_product(monkeypatch):
    box: dict = {}
    monkeypatch.setattr(checkout.catalog, "get_offerings", _fake_offerings)
    monkeypatch.setattr(checkout, "get_polar", lambda: _FakePolar(box))
    user = SimpleNamespace(id="u2", email="f@b.c")

    await checkout.create_checkout_session(
        None, user, role="founder", success_url="https://s/success"
    )
    assert box["request"]["products"] == ["prod_f"]


@pytest.mark.asyncio
async def test_checkout_unknown_role_raises(monkeypatch):
    monkeypatch.setattr(checkout.catalog, "get_offerings", _fake_offerings)
    user = SimpleNamespace(id="u1", email="a@b.c")
    with pytest.raises(checkout.UnknownRole):
        await checkout.create_checkout_session(
            None, user, role="nope", success_url="s"
        )
