"""Seed/refresh the sudo founder tier metadata + perks on Stripe Products.

Stripe is the source of truth for the sudo pricing catalog. The dashboard owns
each Product's name, description, price, and perks (marketing_features); this
script writes the short, structured site-side fields (metadata) and the perks
list via the API so they don't have to be hand-entered.

Idempotent: re-running overwrites the same fields. Run against whichever key is
in the environment (test or live).

Usage:
    STRIPE_SECRET_KEY=sk_... python scripts/seed_stripe_catalog.py
"""

from __future__ import annotations

import os
import sys

import stripe

# Match products by the founder price (unit_amount, cents) → tier slug.
_AMOUNT_TO_SLUG = {12800: "r", 25600: "rw", 51200: "rwx"}

# slug → (metadata, perks). `tag` may contain `{seats}`, filled at render time.
_TIERS: dict[str, dict] = {
    "r": {
        "description": (
            "The starting line. Your year up front funds the build, and you "
            "ship with us as every piece of sudo lands."
        ),
        "metadata": {
            "sudo_tier": "r",
            "perm": "r--",
            "tier_label": "READ",
            "role": "read",
            "order": "1",
            "hero": "false",
            "tag": "FOUNDER · 1 YEAR",
            "cta_label": "./reserve --r--",
        },
        "features": [
            "Scan — answers from our curated library",
            "RunHacks — new challenges on a schedule",
            "Gym when it launches",
            "Founder role + members-only Discord",
        ],
    },
    "rw": {
        "description": (
            "For the developers who'll lean on it. Extended limits across every "
            "tool, and the support that pays for the headroom."
        ),
        "metadata": {
            "sudo_tier": "rw",
            "perm": "rw-",
            "tier_label": "WRITE",
            "role": "write",
            "order": "2",
            "hero": "false",
            "tag": "FOUNDER · 1 YEAR",
            "cta_label": "./reserve --rw-",
        },
        "features": [
            "Everything in r--",
            "Extended limits across every tool",
            "Labs when it launches",
            "Priority support from the team",
        ],
    },
    "rwx": {
        "description": (
            "The first 16. Early preview access to Gym and Labs while we "
            "shape them, because we need users in the loop to ship them right."
        ),
        "metadata": {
            "sudo_tier": "rwx",
            "perm": "rwx",
            "tier_label": "EXECUTE",
            "role": "execute",
            "order": "3",
            "hero": "true",
            "tag": "0DAY · {seats} SEATS",
            "cta_label": "./reserve --rwx --0day",
        },
        "features": [
            "Everything in rw-",
            "Gym preview access while we shape it",
            "Labs preview access while we shape it",
            "0day Founder role + dedicated channel",
        ],
    },
}


def main() -> int:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        print("STRIPE_SECRET_KEY not set", file=sys.stderr)
        return 1
    stripe.api_key = key

    products = stripe.Product.list(limit=100, active=True).data
    seeded = 0
    for product in products:
        prices = stripe.Price.list(product=product.id, active=True).data
        amount = next((p.unit_amount for p in prices if p.type == "one_time"), None)
        slug = _AMOUNT_TO_SLUG.get(amount)
        if slug is None:
            continue
        spec = _TIERS[slug]
        stripe.Product.modify(
            product.id,
            description=spec["description"],
            metadata=spec["metadata"],
            marketing_features=[{"name": f} for f in spec["features"]],
        )
        print(f"seeded {slug:>3} → {product.id} ({amount/100:.0f} USD)")
        seeded += 1

    print(f"done — {seeded}/3 tiers seeded")
    return 0 if seeded == 3 else 2


if __name__ == "__main__":
    raise SystemExit(main())
