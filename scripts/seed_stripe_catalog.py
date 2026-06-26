"""Seed/refresh the sudo offering catalog on Stripe.

Stripe is the source of truth for the sudo catalog. There are exactly two
offerings:

* **Hacker** — $8/month recurring subscription. Every RunHacks challenge.
* **Founder** — one-time, pay-what-you-want with a $256 minimum. Funds the
  build and grants the inside seat.

Each is a Stripe Product tagged with ``metadata.sudo_role`` (``hacker`` /
``founder``), carrying its perks in ``marketing_features`` and its price in a
single active Price. Discord projection role IDs live on the Product metadata
so ``converge`` can map an entitlement to guild roles.

This script is idempotent: it matches products by ``metadata.sudo_role``,
updates their metadata + perks, ensures the right Price exists (creating a new
one only when the amount/shape changed — Stripe Prices are immutable), and
archives any leftover legacy tier products (``metadata.sudo_tier`` set).

Usage:
    STRIPE_SECRET_KEY=sk_... uv run python scripts/seed_stripe_catalog.py
"""

from __future__ import annotations

import json
import sys

import stripe

from smarter_dev.shared.config import get_settings

# ── Discord projection (carried over from the original launch config) ──
# base role = "members-only" access granted to any active sudo member;
# the Founder role + its dedicated-channel role are layered on for founders.
_GUILD_ID = "644299523686006834"
_BASE_ROLE_ID = "1513308785806938122"
_FOUNDER_ROLE_ID = "1513308170519580823"
_FOUNDER_CHANNEL_ROLE_ID = "1513308208582889674"

# slug → full offering definition. ``price`` describes the single active Price
# the offering should have; we create it if no active price matches.
_OFFERINGS: dict[str, dict] = {
    "hacker": {
        "name": "Hacker",
        "description": "Every RunHacks challenge, the day it drops.",
        "price": {"unit_amount": 800, "recurring": {"interval": "month"}},
        "metadata": {
            "sudo_role": "hacker",
            "order": "1",
            "hero": "false",
            "cta_label": "./join --hacker",
            "discord_guild_id": _GUILD_ID,
            "discord_base_role_id": _BASE_ROLE_ID,
            "discord_role_ids": "",
        },
        "features": [
            "All RunHacks challenges, new ones on a schedule",
            "Members-only Discord",
        ],
    },
    "founder": {
        "name": "Founder",
        "description": (
            "Fund the build and take the inside seat. One-time, pay what you "
            "want above the $256 minimum."
        ),
        # Pay-what-you-want one-time price: a $256 floor with a $256 preset.
        "price": {
            "currency": "usd",
            "custom_unit_amount": {
                "enabled": True,
                "minimum": 25600,
                "preset": 25600,
            },
        },
        "metadata": {
            "sudo_role": "founder",
            "order": "2",
            "hero": "true",
            "min_amount": "25600",
            "cta_label": "./fund --founder",
            "discord_guild_id": _GUILD_ID,
            "discord_base_role_id": _BASE_ROLE_ID,
            "discord_role_ids": f"{_FOUNDER_ROLE_ID},{_FOUNDER_CHANNEL_ROLE_ID}",
        },
        "features": [
            "Everything in Hacker",
            "An early look at what's being built",
            "A voice while it's young",
            "Founder role + dedicated channel",
        ],
    },
}


def _active_price_matches(price: dict, want: dict) -> bool:
    """True if an existing active Price already matches the desired shape."""
    if "custom_unit_amount" in want:
        cua = price.get("custom_unit_amount")
        if not cua:
            return False
        return (
            int(cua.get("minimum") or 0) == int(want["custom_unit_amount"]["minimum"])
            and price.get("type") == "one_time"
        )
    # Fixed recurring price.
    if price.get("unit_amount") != want["unit_amount"]:
        return False
    rec = price.get("recurring") or {}
    return rec.get("interval") == want["recurring"]["interval"]


def _ensure_price(product_id: str, want: dict) -> str:
    """Return the id of an active Price matching ``want``, creating it if needed.

    Stripe Prices are immutable, so any change means a new Price; we leave the
    old ones in place (archiving them would break historical references) but
    only ever surface the matching one.
    """
    existing = json.loads(str(stripe.Price.list(product=product_id, active=True, limit=100)))
    for price in existing["data"]:
        if _active_price_matches(price, want):
            return price["id"]

    params: dict = {"product": product_id, "currency": want.get("currency", "usd")}
    if "custom_unit_amount" in want:
        params["custom_unit_amount"] = want["custom_unit_amount"]
    else:
        params["unit_amount"] = want["unit_amount"]
        params["recurring"] = want["recurring"]
    created = json.loads(str(stripe.Price.create(**params)))
    return created["id"]


def _find_product_by_role(role: str) -> dict | None:
    for product in stripe.Product.list(limit=100, active=True).auto_paging_iter():
        data = json.loads(str(product))
        if (data.get("metadata") or {}).get("sudo_role") == role:
            return data
    return None


def _archive_legacy_products() -> list[str]:
    """Deactivate any leftover legacy tier products (``metadata.sudo_tier``)."""
    archived: list[str] = []
    for product in stripe.Product.list(limit=100, active=True).auto_paging_iter():
        data = json.loads(str(product))
        meta = data.get("metadata") or {}
        if meta.get("sudo_tier") and not meta.get("sudo_role"):
            stripe.Product.modify(data["id"], active=False)
            archived.append(f"{data['id']} ({data.get('name')})")
    return archived


def main() -> int:
    settings = get_settings()
    if not settings.stripe_secret_key:
        print("STRIPE_SECRET_KEY is not configured.", file=sys.stderr)
        return 1
    stripe.api_key = settings.stripe_secret_key

    archived = _archive_legacy_products()
    for line in archived:
        print(f"archived legacy product: {line}")

    for role, spec in _OFFERINGS.items():
        product = _find_product_by_role(role)
        if product is None:
            product = json.loads(
                str(
                    stripe.Product.create(
                        name=spec["name"],
                        description=spec["description"],
                        metadata=spec["metadata"],
                        marketing_features=[{"name": f} for f in spec["features"]],
                    )
                )
            )
            print(f"created product {product['id']} ({spec['name']})")
        else:
            stripe.Product.modify(
                product["id"],
                name=spec["name"],
                description=spec["description"],
                metadata=spec["metadata"],
                marketing_features=[{"name": f} for f in spec["features"]],
            )
            print(f"updated product {product['id']} ({spec['name']})")

        price_id = _ensure_price(product["id"], spec["price"])
        print(f"  price: {price_id}")

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
