"""Seed/refresh the sudo offering catalog on Polar.

Polar is the source of truth for the sudo catalog. There are exactly two
offerings:

* **Hacker** — $8/month recurring subscription. Every RunHacks challenge.
* **Founder** — one-time, pay-what-you-want with a $256 minimum. Funds the
  build and grants the inside seat.

Each is a Polar Product tagged with ``metadata.sudo_role`` (``hacker`` /
``founder``), carrying its perks in numbered ``metadata.feature_<n>`` keys
(Polar has no ``marketing_features`` field) and its price as a single active
Price. Discord projection role IDs live on the Product metadata so ``converge``
can map an entitlement to guild roles.

This script is idempotent: it matches products by ``metadata.sudo_role`` and
creates them (with their price) if missing, otherwise updates their metadata,
name, and description. Polar prices are immutable, so changing an amount means
archiving the product and re-seeding — the script prints a warning if the live
price no longer matches the desired amount rather than silently diverging.

Usage:
    POLAR_ACCESS_TOKEN=polar_oat_... POLAR_SERVER=sandbox \\
        uv run python scripts/seed_polar_catalog.py
"""

from __future__ import annotations

import asyncio
import sys

from polar_sdk import Polar
from polar_sdk.models import (
    ProductPriceCustomCreate,
    ProductPriceFixedCreate,
    SubscriptionRecurringInterval,
)

from smarter_dev.shared.config import get_settings

# ── Discord projection (carried over from the original launch config) ──
# base role = "members-only" access granted to any active sudo member;
# the Founder role is layered on for founders. Dedicated-channel access hangs
# off the Founder role itself (via Discord channel permissions), so no separate
# channel role is granted.
_GUILD_ID = "644299523686006834"
_BASE_ROLE_ID = "1513308785806938122"
_FOUNDER_ROLE_ID = "1513308170519580823"

_HACKER_PRICE_CENTS = 800
_FOUNDER_MIN_CENTS = 25600

# slug → full offering definition.
_OFFERINGS: dict[str, dict] = {
    "hacker": {
        "name": "Hacker",
        "description": "Every RunHacks challenge, the day it drops.",
        "recurring_interval": SubscriptionRecurringInterval.MONTH,
        "price": ProductPriceFixedCreate(price_amount=_HACKER_PRICE_CENTS),
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
        "recurring_interval": None,  # one-time
        "price": ProductPriceCustomCreate(
            minimum_amount=_FOUNDER_MIN_CENTS,
            preset_amount=_FOUNDER_MIN_CENTS,
        ),
        "metadata": {
            "sudo_role": "founder",
            "order": "2",
            "hero": "true",
            "min_amount": str(_FOUNDER_MIN_CENTS),
            "cta_label": "./fund --founder",
            "discord_guild_id": _GUILD_ID,
            "discord_base_role_id": _BASE_ROLE_ID,
            "discord_role_ids": _FOUNDER_ROLE_ID,
        },
        "features": [
            "Everything in Hacker",
            "An early look at what's being built",
            "A voice while it's young",
            "Founder role + dedicated channel",
        ],
    },
}


def _build_metadata(spec: dict) -> dict[str, str]:
    """Flatten the spec metadata + numbered feature keys into one metadata dict.

    Polar rejects empty-string metadata values, so blank keys (e.g. Hacker's
    empty ``discord_role_ids``) are dropped — the catalog treats a missing key
    the same as an empty one.
    """
    metadata = dict(spec["metadata"])
    for index, feature in enumerate(spec["features"]):
        metadata[f"feature_{index}"] = feature
    return {key: value for key, value in metadata.items() if value != ""}


async def _find_product_by_role(polar: Polar, role: str):
    """Return the live Polar product tagged with ``sudo_role == role``, or None."""
    page = 1
    while True:
        response = await polar.products.list_async(
            is_archived=False, limit=100, page=page
        )
        if response is None:
            return None
        items = list(response.result.items)
        for product in items:
            if (product.metadata or {}).get("sudo_role") == role:
                return product
        if len(items) < 100:
            return None
        page += 1


def _live_price_cents(product) -> int | None:
    """Best-effort live amount for a product's active price (for drift warnings)."""
    for price in product.prices or []:
        if getattr(price, "is_archived", False):
            continue
        if getattr(price, "price_amount", None) is not None:
            return int(price.price_amount)
        if getattr(price, "minimum_amount", None) is not None:
            return int(price.minimum_amount)
    return None


async def _seed(polar: Polar, organization_id: str | None) -> None:
    for role, spec in _OFFERINGS.items():
        metadata = _build_metadata(spec)
        product = await _find_product_by_role(polar, role)

        if product is None:
            request: dict = {
                "name": spec["name"],
                "description": spec["description"],
                "prices": [spec["price"]],
                "metadata": metadata,
            }
            if spec["recurring_interval"] is not None:
                request["recurring_interval"] = spec["recurring_interval"]
            if organization_id:
                request["organization_id"] = organization_id
            created = await polar.products.create_async(request=request)
            print(f"created product {created.id} ({spec['name']})")
            continue

        await polar.products.update_async(
            id=product.id,
            product_update={
                "name": spec["name"],
                "description": spec["description"],
                "metadata": metadata,
            },
        )
        print(f"updated product {product.id} ({spec['name']})")

        want_cents = (
            _FOUNDER_MIN_CENTS if role == "founder" else _HACKER_PRICE_CENTS
        )
        live_cents = _live_price_cents(product)
        if live_cents != want_cents:
            print(
                f"  WARNING: live price {live_cents} != desired {want_cents}; "
                "Polar prices are immutable — archive the product and re-seed "
                "to change the amount.",
                file=sys.stderr,
            )
        else:
            print(f"  price: {live_cents} cents (unchanged)")


async def main() -> int:
    settings = get_settings()
    if not settings.polar_access_token:
        print("POLAR_ACCESS_TOKEN is not configured.", file=sys.stderr)
        return 1

    async with Polar(
        access_token=settings.polar_access_token,
        server=settings.polar_server,
    ) as polar:
        await _seed(polar, settings.polar_organization_id)

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
