"""Unit tests for the two-role sudo billing logic (Hacker / Founder).

These cover the pure, Stripe-shaped logic without touching the database or the
live Stripe API: catalog parsing, subscription period-end extraction, webhook
dispatch routing, and Checkout Session parameter construction.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from smarter_dev.web.billing import catalog, checkout, webhooks


class _Obj:
    """Mimics a Stripe SDK object whose ``str()`` is its JSON body."""

    def __init__(self, data: dict):
        self._data = data

    def __str__(self) -> str:
        return json.dumps(self._data)


# ── catalog parsing ────────────────────────────────────────────────


def test_select_price_prefers_recurring():
    prices = [
        {"id": "p_once", "type": "one_time"},
        {"id": "p_sub", "type": "recurring", "recurring": {"interval": "month"}},
    ]
    assert catalog._select_price(prices)["id"] == "p_sub"


def test_select_price_falls_back_to_one_time():
    prices = [{"id": "p_once", "type": "one_time"}]
    assert catalog._select_price(prices)["id"] == "p_once"


def test_select_price_none_when_empty():
    assert catalog._select_price([]) is None


def test_fetch_catalog_maps_hacker_and_founder(monkeypatch):
    products = [
        _Obj({
            "id": "prod_h", "name": "Hacker", "description": "challenges",
            "marketing_features": [{"name": "All RunHacks"}],
            "metadata": {
                "sudo_role": "hacker", "order": "1", "hero": "false",
                "cta_label": "./join --hacker",
                "discord_guild_id": "g", "discord_base_role_id": "b",
                "discord_role_ids": "",
            },
        }),
        _Obj({
            "id": "prod_f", "name": "Founder", "description": "fund",
            "marketing_features": [{"name": "Everything in Hacker"}],
            "metadata": {
                "sudo_role": "founder", "order": "2", "hero": "true",
                "cta_label": "./fund --founder",
                "discord_guild_id": "g", "discord_base_role_id": "b",
                "discord_role_ids": "r1,r2",
            },
        }),
        _Obj({"id": "prod_x", "name": "Unrelated", "metadata": {}}),
    ]
    prices = {
        "prod_h": _Obj({"data": [{
            "id": "price_h", "type": "recurring", "unit_amount": 800,
            "recurring": {"interval": "month"},
        }]}),
        "prod_f": _Obj({"data": [{
            "id": "price_f", "type": "one_time",
            "custom_unit_amount": {"minimum": 25600, "preset": 25600},
        }]}),
    }

    class FakeProductList:
        def auto_paging_iter(self):
            return iter(products)

    class FakeStripe:
        class Product:
            @staticmethod
            def list(**kw):
                return FakeProductList()

        class Price:
            @staticmethod
            def list(product, **kw):
                return prices[product]

    monkeypatch.setattr(catalog, "get_stripe", lambda: FakeStripe)
    offerings = catalog._fetch_catalog_sync()

    assert [o["role"] for o in offerings] == ["hacker", "founder"]
    hacker, founder = offerings
    assert hacker["price_cents"] == 800
    assert hacker["recurring"] is True
    assert hacker["interval"] == "month"
    assert hacker["pay_what_you_want"] is False
    assert founder["recurring"] is False
    assert founder["pay_what_you_want"] is True
    assert founder["min_cents"] == 25600
    assert founder["hero"] is True
    assert founder["discord_role_ids"] == ["r1", "r2"]


# ── subscription period end ────────────────────────────────────────


def test_subscription_period_end_top_level():
    ts = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
    got = webhooks._subscription_period_end({"current_period_end": ts})
    assert got == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_subscription_period_end_item_fallback():
    ts = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    sub = {"items": {"data": [{"current_period_end": ts}]}}
    assert webhooks._subscription_period_end(sub) == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_subscription_period_end_missing_defaults_now():
    before = datetime.now(tz=timezone.utc)
    got = webhooks._subscription_period_end({})
    assert got >= before


# ── dispatch routing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_routes_known_event(monkeypatch):
    seen = {}

    async def fake_handler(session, data):
        seen["data"] = data

    monkeypatch.setitem(webhooks._HANDLERS, "checkout.session.completed", fake_handler)
    await webhooks.dispatch(None, {"type": "checkout.session.completed", "data": {"x": 1}})
    assert seen["data"] == {"x": 1}


@pytest.mark.asyncio
async def test_dispatch_ignores_unknown_event():
    # Should simply no-op (and not raise) for an unmapped type.
    await webhooks.dispatch(None, {"type": "invoice.upcoming", "data": {}})


# ── checkout param construction ────────────────────────────────────


class _CapturingStripe:
    last_params: dict = {}

    class checkout:
        class Session:
            @staticmethod
            def create(**params):
                _CapturingStripe.last_params = params
                return type("S", (), {"url": "https://checkout.stripe.test/x"})()


@pytest.mark.asyncio
async def test_checkout_hacker_is_subscription_mode(monkeypatch):
    monkeypatch.setattr(checkout.catalog, "get_offerings", _fake_offerings)
    monkeypatch.setattr(checkout, "get_stripe", lambda: _CapturingStripe)
    user = type("U", (), {"id": "u1", "email": "a@b.c"})()
    url = await checkout.create_checkout_session(
        None, user, role="hacker", success_url="s", cancel_url="c"
    )
    p = _CapturingStripe.last_params
    assert url.startswith("https://checkout.stripe.test")
    assert p["mode"] == "subscription"
    assert p["line_items"][0]["price"] == "price_h"
    assert "subscription_data" in p
    assert "customer_creation" not in p  # invalid in subscription mode


@pytest.mark.asyncio
async def test_checkout_founder_is_payment_mode(monkeypatch):
    monkeypatch.setattr(checkout.catalog, "get_offerings", _fake_offerings)
    monkeypatch.setattr(checkout, "get_stripe", lambda: _CapturingStripe)
    user = type("U", (), {"id": "u1", "email": "a@b.c"})()
    await checkout.create_checkout_session(
        None, user, role="founder", success_url="s", cancel_url="c"
    )
    p = _CapturingStripe.last_params
    assert p["mode"] == "payment"
    assert p["customer_creation"] == "always"
    assert "payment_intent_data" in p


@pytest.mark.asyncio
async def test_checkout_unknown_role_raises(monkeypatch):
    monkeypatch.setattr(checkout.catalog, "get_offerings", _fake_offerings)
    user = type("U", (), {"id": "u1", "email": "a@b.c"})()
    with pytest.raises(checkout.UnknownRole):
        await checkout.create_checkout_session(
            None, user, role="nope", success_url="s", cancel_url="c"
        )


async def _fake_offerings():
    return [
        {"role": "hacker", "price_id": "price_h", "recurring": True},
        {"role": "founder", "price_id": "price_f", "recurring": False},
    ]
