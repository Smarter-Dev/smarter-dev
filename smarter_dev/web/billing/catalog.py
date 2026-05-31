"""Sudo founder tier catalog, sourced from Stripe.

Stripe is the single source of truth for the pricing ladder: each tier is a
Product tagged with ``metadata.sudo_tier``, carrying its perks in
``marketing_features`` and its founder price in a one-time Price. The public
comparison prices are *computed* from the founder price, not stored:

    base   = founder * 1.5          # the public "full" rate; founder is 33% off it
    monthly = base / 12             # public monthly
    annual  = monthly * 10          # public annual, SaaS-standard 2 months free

Reads are served from an in-process cache with a stale-while-revalidate policy:
a request always gets the current cached catalog immediately; if the cache is
older than ``_TTL_SECONDS`` it triggers a single background refresh (prices
change rarely, so serving slightly stale data beats blocking on Stripe). Force
a refresh by restarting the pods.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import anyio

from smarter_dev.web.billing.client import get_stripe

_TTL_SECONDS = 15 * 60

_cache: list[dict[str, Any]] | None = None
_fetched_at: float = 0.0
_refreshing = False


def _compute_public_prices(founder: int) -> tuple[int, int, int]:
    """Return (base, monthly, annual) derived from the founder price."""
    base = round(founder * 1.5)
    monthly = round(base / 12)
    annual = monthly * 10
    return base, monthly, annual


def _fetch_catalog_sync() -> list[dict[str, Any]]:
    """Pull tiers from Stripe. Runs the blocking SDK calls off the event loop."""
    stripe = get_stripe()
    tiers: list[dict[str, Any]] = []
    for product in stripe.Product.list(limit=100, active=True).auto_paging_iter():
        # StripeObject's attribute access is hostile to dict ops; the JSON repr
        # is the reliable way to get a plain nested dict.
        data = json.loads(str(product))
        meta = data.get("metadata") or {}
        slug = meta.get("sudo_tier")
        if not slug:
            continue

        prices = json.loads(str(stripe.Price.list(product=data["id"], active=True)))
        price = next(
            (p for p in prices["data"] if p["type"] == "one_time"), None
        )
        if not price:
            continue

        founder = price["unit_amount"] // 100
        base, monthly, annual = _compute_public_prices(founder)
        tiers.append(
            {
                "id": slug,
                "perm": meta.get("perm", ""),
                "tier": meta.get("tier_label", ""),
                "tag": meta.get("tag", ""),
                "role": meta.get("role", ""),
                "hero": meta.get("hero") == "true",
                "order": int(meta.get("order", "0") or 0),
                "cta_label": meta.get("cta_label", ""),
                "desc": data.get("description") or "",
                "feats": [f["name"] for f in (data.get("marketing_features") or [])],
                "annual": founder,
                "base": base,
                "public_monthly": monthly,
                "public_annual": annual,
                "price_id": price["id"],
            }
        )

    tiers.sort(key=lambda t: t["order"])
    return tiers


async def _refresh() -> None:
    global _cache, _fetched_at, _refreshing
    try:
        tiers = await anyio.to_thread.run_sync(_fetch_catalog_sync)
        _cache = tiers
        _fetched_at = time.monotonic()
    finally:
        _refreshing = False


async def get_tiers() -> list[dict[str, Any]]:
    """Return the founder tiers, newest-cached-first with background refresh.

    On a cold cache this blocks on Stripe once. Afterwards it always returns
    the cached catalog immediately and refreshes in the background past TTL.
    """
    global _refreshing

    if _cache is None:
        await _refresh()
        return _cache or []

    if time.monotonic() - _fetched_at > _TTL_SECONDS and not _refreshing:
        _refreshing = True
        asyncio.create_task(_refresh())

    return _cache


def get_tier(tiers: list[dict[str, Any]], slug: str) -> dict[str, Any] | None:
    """Find a tier by slug within an already-fetched catalog."""
    return next((t for t in tiers if t["id"] == slug), None)
